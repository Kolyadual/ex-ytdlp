#!/usr/bin/env python3
import os
import tempfile
import json
import re
from pathlib import Path
from typing import Optional

import yt_dlp
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # разрешаем запросы с GitHub Pages

# ---------- Конфигурации (как в вашем CLI) ----------
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3

PLATFORM_CONFIGS = {
    "rutube": {
        "extractor_args": {"rutube": {"player_client": "web"}},
        "format": "best[height<=1080]",
    },
    "vk": {
        "extractor_args": {"vk": {"player_client": "web"}},
        "format": "best[height<=1080]",
    },
    "ok": {
        "extractor_args": {"ok": {}},
        "format": "best[height<=1080]",
    },
    "youtube": {
        "extractor_args": {"youtube": {"player_client": "default,-android_sdkless"}},
        "format": "best[height<=1080]",
    },
    "tiktok": {
        "extractor_args": {"tiktok": {"webapp": "true"}},
        "format": "best",
    },
}

def detect_platform(url: str) -> str:
    if "rutube.ru" in url:
        return "rutube"
    elif "vk.com/video" in url or "vk.ru/video" in url or "vkvideo.ru" in url or "vkvideo.com" in url:
        return "vk"
    elif "ok.ru" in url:
        return "ok"
    elif "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    elif "tiktok.com" in url or "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        return "tiktok"
    else:
        return "unknown"

def get_ydl_opts(platform: str, cookies_path: Optional[str] = None, for_download: bool = False) -> dict:
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
    return opts

def get_video_info(url: str, platform: str, cookies_path: Optional[str] = None) -> Optional[dict]:
    """Возвращает {url, title, thumbnail, duration, uploader, platform}"""
    ydl_opts = get_ydl_opts(platform, cookies_path, for_download=False)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None
            # Берём прямую ссылку на видео (best)
            video_url = info.get("url")
            if not video_url and info.get("formats"):
                # сортируем по разрешению, берём самый высокий
                formats = sorted(info["formats"], key=lambda f: f.get("height") or 0, reverse=True)
                video_url = formats[0]["url"] if formats else None
            return {
                "url": video_url,
                "title": info.get("title", "Unknown"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
                "platform": platform,
            }
    except Exception as e:
        print(f"Error extracting info: {e}")
        return None

def sanitize_filename(title: str, max_len: int = 100) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", title)
    return cleaned[:max_len].strip()

# ---------- Эндпоинты ----------
@app.route('/get_stream_url', methods=['POST'])
def get_stream_url():
    """
    Получить прямую ссылку на видео (без скачивания).
    Тело запроса: {"url": "https://..."}
    Ответ: {"stream_url": "...", "title": "...", "thumbnail": "...", ...}
    """
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({'error': 'Missing url'}), 400
    
    url = data['url']
    platform = detect_platform(url)
    if platform == 'unknown':
        return jsonify({'error': 'Unsupported platform'}), 400
    
    cookies = data.get('cookies_path')  # можно позже реализовать загрузку cookies
    info = get_video_info(url, platform, cookies)
    if not info or not info['url']:
        return jsonify({'error': 'Failed to extract video URL'}), 500
    
    return jsonify({
        'stream_url': info['url'],
        'title': info['title'],
        'thumbnail': info['thumbnail'],
        'duration': info['duration'],
        'uploader': info['uploader'],
        'platform': platform
    })

@app.route('/download', methods=['POST'])
def download_video():
    """
    Скачать видео и отдать файл.
    Тело запроса: {"url": "https://..."}
    Ответ: video/mp4 attachment
    """
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({'error': 'Missing url'}), 400
    
    url = data['url']
    platform = detect_platform(url)
    if platform == 'unknown':
        return jsonify({'error': 'Unsupported platform'}), 400
    
    cookies_path = data.get('cookies_path')  # опционально
    
    # Создаём временную директорию
    temp_dir = tempfile.mkdtemp()
    filename = None
    
    ydl_opts = get_ydl_opts(platform, cookies_path, for_download=True)
    ydl_opts.update({
        'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
        'merge_output_format': 'mp4',
    })
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # Если расширение не mp4, а, например, webm, переименовывать не будем,
            # но можно попробовать конвертировать через postprocessor.
            # Отправляем как есть, браузер всё равно поймёт.
            return send_file(
                filename,
                as_attachment=True,
                download_name=f"{sanitize_filename(info['title'])}.mp4",
                mimetype='video/mp4'
            )
    except Exception as e:
        return jsonify({'error': f'Download failed: {str(e)}'}), 500
    finally:
        # Удаляем временный файл и папку после отправки
        if filename and os.path.exists(filename):
            os.remove(filename)
        if os.path.exists(temp_dir):
            os.rmdir(temp_dir)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    # Платформа сама подставит порт через переменную окружения
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
