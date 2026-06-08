#!/usr/bin/env python3
import os
import tempfile
import re
from urllib.parse import urlparse
import requests
from flask import Flask, request, jsonify, send_file, Response, redirect
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

# ---------- Конфигурация ----------
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3

PLATFORM_CONFIGS = {
    "rutube": {"extractor_args": {"rutube": {"player_client": "web"}}, "format": "best[height<=1080]"},
    "vk": {"extractor_args": {"vk": {"player_client": "web"}}, "format": "best[height<=1080]"},
    "ok": {"extractor_args": {"ok": {}}, "format": "best[height<=1080]"},
    "youtube": {"extractor_args": {"youtube": {"player_client": "default,-android_sdkless"}}, "format": "best[height<=1080]"},
    "tiktok": {"extractor_args": {"tiktok": {"webapp": "true"}}, "format": "best"},
}

def detect_platform(url: str) -> str:
    if "rutube.ru" in url: return "rutube"
    if "vk.com/video" in url or "vk.ru/video" in url or "vkvideo.ru" in url or "vkvideo.com" in url: return "vk"
    if "ok.ru" in url: return "ok"
    if "youtube.com" in url or "youtu.be" in url: return "youtube"
    if "tiktok.com" in url or "vm.tiktok.com" in url or "vt.tiktok.com" in url: return "tiktok"
    return "unknown"

def get_direct_url(video_url: str, cookies_path: str = None) -> str | None:
    """Извлекает прямую ссылку на видео (лучшее качество) с помощью yt-dlp"""
    platform = detect_platform(video_url)
    if platform == "unknown":
        return None
    config = PLATFORM_CONFIGS.get(platform, PLATFORM_CONFIGS["youtube"])
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": DEFAULT_TIMEOUT,
        "retries": DEFAULT_RETRIES,
        "user_agent": DEFAULT_USER_AGENT,
        "extractor_args": config["extractor_args"],
        "format": config["format"],
        "no_playlist": True,
    }
    if cookies_path and os.path.exists(cookies_path):
        opts["cookiefile"] = cookies_path
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            if not info:
                return None
            # Прямая ссылка может быть в info['url'] или в formats
            url = info.get("url")
            if not url and info.get("formats"):
                # берём самый высокий по разрешению
                formats = sorted(info["formats"], key=lambda f: f.get("height") or 0, reverse=True)
                url = formats[0].get("url") if formats else None
            return url
    except Exception as e:
        print(f"Error extracting direct URL: {e}")
        return None

# ---------- Новый эндпоинт для стриминга с Range ----------
@app.route('/stream')
def stream_video():
    """Проксирует видео с поддержкой Range (частичная загрузка)"""
    video_url = request.args.get('url')
    if not video_url:
        return jsonify({'error': 'Missing url parameter'}), 400

    # Получаем прямую ссылку на видео (через yt-dlp)
    direct_url = get_direct_url(video_url)
    if not direct_url:
        return jsonify({'error': 'Could not extract video URL'}), 500

    # Передаём запрос к реальному видео
    try:
        # Заголовки, которые нужно передать дальше (например, Range от клиента)
        headers = {'User-Agent': DEFAULT_USER_AGENT}
        range_header = request.headers.get('Range')
        if range_header:
            headers['Range'] = range_header

        # Делаем запрос к источнику
        resp = requests.get(direct_url, headers=headers, stream=True, timeout=30)

        # Определяем MIME-тип (обычно video/mp4 или application/octet-stream)
        content_type = resp.headers.get('Content-Type', 'video/mp4')
        
        # Создаём ответ Flask с потоковым режимом
        def generate():
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        response = Response(generate(), status=resp.status_code, content_type=content_type)
        
        # Проксируем важные заголовки
        if 'Content-Range' in resp.headers:
            response.headers['Content-Range'] = resp.headers['Content-Range']
        if 'Content-Length' in resp.headers:
            response.headers['Content-Length'] = resp.headers['Content-Length']
        response.headers['Accept-Ranges'] = 'bytes'
        
        return response
    except Exception as e:
        return jsonify({'error': f'Proxy error: {str(e)}'}), 500

# ---------- Корневой URL с параметром url (редирект на /stream для красоты) ----------
@app.route('/')
def index():
    url = request.args.get('url')
    if url:
        # Редирект на /stream с тем же параметром
        return redirect(f'/stream?url={url}')
    # Если без параметра — просто приветствие
    return jsonify({
        'message': 'YouTube/Rutube/VK proxy. Use ?url=VIDEO_URL to stream, or POST /download with JSON {"url": ...}'
    })

# ---------- Старые эндпоинты (сохранены для совместимости) ----------
@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

@app.route('/get_stream_url', methods=['POST'])
def get_stream_url():
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({'error': 'Missing url'}), 400
    url = data['url']
    platform = detect_platform(url)
    if platform == 'unknown':
        return jsonify({'error': 'Unsupported platform'}), 400
    cookies = data.get('cookies_path')
    direct = get_direct_url(url, cookies)
    if not direct:
        return jsonify({'error': 'Failed to extract direct URL'}), 500
    return jsonify({'stream_url': direct, 'platform': platform})

@app.route('/download', methods=['POST'])
def download_video():
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({'error': 'Missing url'}), 400
    url = data['url']
    platform = detect_platform(url)
    if platform == 'unknown':
        return jsonify({'error': 'Unsupported platform'}), 400
    cookies_path = data.get('cookies_path')
    temp_dir = tempfile.mkdtemp()
    filename = None
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": DEFAULT_TIMEOUT,
        "retries": DEFAULT_RETRIES,
        "user_agent": DEFAULT_USER_AGENT,
        "extractor_args": PLATFORM_CONFIGS.get(platform, {}).get("extractor_args", {}),
        "format": PLATFORM_CONFIGS.get(platform, {}).get("format", "best"),
        "no_playlist": True,
        "outtmpl": os.path.join(temp_dir, '%(title)s.%(ext)s'),
        "merge_output_format": "mp4",
    }
    if cookies_path and os.path.exists(cookies_path):
        opts["cookiefile"] = cookies_path
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # Отправляем файл
            return send_file(
                filename,
                as_attachment=True,
                download_name=f"{info.get('title', 'video')}.mp4",
                mimetype='video/mp4'
            )
    except Exception as e:
        return jsonify({'error': f'Download failed: {str(e)}'}), 500
    finally:
        if filename and os.path.exists(filename):
            os.remove(filename)
        if os.path.exists(temp_dir):
            os.rmdir(temp_dir)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
