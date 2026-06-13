"""下载封装 - B站用 API 直接下载，YouTube 用 yt-dlp"""

import re
import os
import json
import logging
import subprocess
import urllib.request
from pathlib import Path
from dataclasses import dataclass

import yt_dlp

from config import DOWNLOAD_DIR, AUDIO_FORMAT, BILIBILI_COOKIES, YTDL_PROXY

logger = logging.getLogger(__name__)

# 支持的 URL 模式
URL_PATTERNS = [
    re.compile(r'bilibili\.com/video/'),
    re.compile(r'b23\.tv/'),
    re.compile(r'(www\.)?youtube\.com/watch'),
    re.compile(r'youtu\.be/'),
    re.compile(r'(www\.)?youtube\.com/shorts/'),
]


@dataclass
class DownloadResult:
    success: bool
    file_path: Path | None = None
    cover_path: Path | None = None
    title: str = ""
    uploader: str = ""
    duration: int = 0
    format_name: str = ""
    file_size: int = 0
    error: str = ""


def is_supported_url(url: str) -> bool:
    return any(p.search(url) for p in URL_PATTERNS)


def _sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()


def _is_bilibili(url: str) -> bool:
    return bool(re.search(r'bilibili\.com|b23\.tv', url))


# ──────────────────────────────────────────────
#  B站：通过官方 API 直接下载音频（绕过 yt-dlp 反爬问题）
# ──────────────────────────────────────────────

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Referer': 'https://www.bilibili.com',
}


def _extract_bvid(url: str) -> str | None:
    """从 B站 URL 提取 BV 号，支持 b23.tv 短链"""
    # 先尝试直接匹配
    m = re.search(r'(BV[a-zA-Z0-9]+)', url)
    if m:
        return m.group(1)

    # b23.tv 短链需要解析跳转
    if 'b23.tv' in url:
        try:
            req = urllib.request.Request(url, headers=_HEADERS, method='HEAD')
            req.add_header('Accept', '*/*')
            # 不跟随重定向，手动获取 Location
            import http.client
            from urllib.parse import urlparse
            parsed = urlparse(url)
            conn = http.client.HTTPSConnection(parsed.hostname, timeout=10)
            conn.request('GET', parsed.path, headers=_HEADERS)
            resp = conn.getresponse()
            location = resp.getheader('Location', '')
            conn.close()
            logger.info("b23.tv 重定向到: %s", location)
            m = re.search(r'(BV[a-zA-Z0-9]+)', location)
            if m:
                return m.group(1)
            # 如果 Location 中没有，再尝试用 urllib 跟随重定向
            req2 = urllib.request.Request(url, headers=_HEADERS)
            resp2 = urllib.request.urlopen(req2, timeout=10)
            final_url = resp2.url
            logger.info("b23.tv 最终 URL: %s", final_url)
            m = re.search(r'(BV[a-zA-Z0-9]+)', final_url)
            if m:
                return m.group(1)
        except Exception as e:
            logger.error("解析 b23.tv 短链失败: %s", e)

    return None


def _bili_api_get(url: str) -> dict:
    """调用 B站 API"""
    req = urllib.request.Request(url, headers=_HEADERS)
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read())


def _download_bilibili(url: str, progress_hook=None) -> DownloadResult:
    """通过 B站 API 下载最佳音频"""
    bvid = _extract_bvid(url)
    if not bvid:
        return DownloadResult(success=False, error=f"无法从 URL 提取 BV 号: {url}")

    try:
        # 1. 获取视频信息
        info_url = f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}'
        info = _bili_api_get(info_url)
        if info.get('code') != 0:
            return DownloadResult(success=False, error=f"B站 API 错误: {info.get('message')}")

        data = info['data']
        title = _sanitize_filename(data.get('title', 'unknown'))
        uploader = _sanitize_filename(data.get('owner', {}).get('name', 'unknown'))
        cid = data.get('cid') or data['pages'][0]['cid']
        duration = data.get('duration', 0)

        logger.info("B站视频: %s - %s (cid=%s, duration=%ds)", title, uploader, cid, duration)

        # 2. 获取音频流 URL（fnval=4048 = DASH+Hi-Res+Dolby, fourk=1）
        play_url = (
            f'https://api.bilibili.com/x/player/playurl'
            f'?bvid={bvid}&cid={cid}&fnval=4048&qn=0&fourk=1'
        )
        play_data = _bili_api_get(play_url)
        if play_data.get('code') != 0:
            return DownloadResult(success=False, error=f"播放信息获取失败: {play_data.get('message')}")

        dash = play_data.get('data', {}).get('dash')
        if not dash:
            return DownloadResult(success=False, error="未获取到 DASH 流信息")

        # 3. 选择最佳音频流（优先 Hi-Res FLAC > Dolby > 普通音频）
        audio_base_url = None
        audio_codec = ''
        audio_ext = 'm4a'

        # 优先级 1: Hi-Res FLAC
        flac_info = dash.get('flac', {})
        if flac_info and flac_info.get('audio'):
            audio_base_url = flac_info['audio'].get('baseUrl') or flac_info['audio'].get('base_url')
            audio_codec = 'flac'
            audio_ext = 'flac'
            logger.info("使用 Hi-Res FLAC 流, bandwidth=%s", flac_info['audio'].get('bandwidth'))

        # 优先级 2: Dolby Audio
        if not audio_base_url:
            dolby_info = dash.get('dolby', {})
            if dolby_info and dolby_info.get('audio'):
                dolby_streams = dolby_info['audio']
                if isinstance(dolby_streams, list) and dolby_streams:
                    best_dolby = max(dolby_streams, key=lambda s: s.get('bandwidth', 0))
                    audio_base_url = best_dolby.get('baseUrl') or best_dolby.get('base_url')
                    audio_codec = best_dolby.get('codecs', 'ec-3')
                    audio_ext = 'eac3'
                    logger.info("使用 Dolby Audio 流, bandwidth=%s", best_dolby.get('bandwidth'))

        # 优先级 3: 普通音频流（按 id 降序选最高质量）
        if not audio_base_url:
            audio_streams = dash.get('audio', [])
            if not audio_streams:
                return DownloadResult(success=False, error="没有可用的音频流")
            best_audio = max(audio_streams, key=lambda s: (s.get('id', 0), s.get('bandwidth', 0)))
            audio_base_url = best_audio.get('baseUrl') or best_audio.get('base_url')
            audio_codec = best_audio.get('codecs', 'mp4a')
            logger.info("使用普通音频流, id=%s, bandwidth=%s", best_audio.get('id'), best_audio.get('bandwidth'))

        # 4. 下载音频文件（带重试）
        temp_path = DOWNLOAD_DIR / f"{title} - {uploader}_temp.{audio_ext}"

        import time as _time

        def _do_download(url: str, dest: Path, progress_hook=None) -> int:
            """下载文件，带 3 次重试"""
            for attempt in range(3):
                try:
                    req = urllib.request.Request(url, headers={
                        **_HEADERS,
                        'Range': f'bytes={dest.stat().st_size if dest.exists() else 0}-',
                    })
                    resp = urllib.request.urlopen(req, timeout=60)
                    total = int(resp.headers.get('Content-Length', 0)) + (dest.stat().st_size if dest.exists() else 0)
                    downloaded = dest.stat().st_size if dest.exists() else 0
                    dl_start = _time.time()
                    mode = 'ab' if dest.exists() and resp.status == 206 else 'wb'
                    if mode == 'wb':
                        downloaded = 0
                    with open(dest, mode) as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if progress_hook and total > 0:
                                elapsed = _time.time() - dl_start
                                speed = downloaded / elapsed if elapsed > 0 else 0
                                remaining = (total - downloaded) / speed if speed > 0 else 0
                                progress_hook({
                                    'percent': downloaded * 100 / total,
                                    'downloaded_bytes': downloaded,
                                    'total_bytes': total,
                                    'speed': speed,
                                    'eta': int(remaining),
                                })
                    return downloaded
                except Exception as e:
                    logger.warning("下载失败 (尝试 %d/3): %s", attempt + 1, e)
                    if attempt == 2:
                        raise
                    _time.sleep(2)
            return 0

        downloaded = _do_download(audio_base_url, temp_path, progress_hook)

        logger.info("原始音频下载完成: %d MB", downloaded // 1024 // 1024)

        # 4.5 验证下载完整性（检查时长）
        try:
            probe = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', str(temp_path)],
                capture_output=True, text=True, timeout=30
            )
            actual_duration = float(probe.stdout.strip()) if probe.stdout.strip() else 0
            logger.info("实际时长: %.1fs, 预期时长: %ds", actual_duration, duration)
            if duration > 0 and actual_duration < duration * 0.9:
                logger.warning("音频不完整! 实际 %.1fs < 预期 %ds 的 90%%", actual_duration, duration)
                temp_path.unlink(missing_ok=True)
                return DownloadResult(
                    success=False, title=title, uploader=uploader,
                    error=f"音频不完整 (实际 {actual_duration:.0f}s, 预期 {duration}s)"
                )
        except Exception as e:
            logger.warning("时长检查失败: %s", e)

        # 4.6 下载视频封面 + 缩放到 320x320（Telegram 缩略图限制）
        cover_path = DOWNLOAD_DIR / f"{title} - {uploader}_cover.jpg"
        cover_url = data.get('pic', '')
        if cover_url:
            try:
                if cover_url.startswith('//'):
                    cover_url = 'https:' + cover_url
                # 下载原始封面
                raw_cover = DOWNLOAD_DIR / f"{title} - {uploader}_cover_raw.jpg"
                req = urllib.request.Request(cover_url, headers=_HEADERS)
                resp = urllib.request.urlopen(req, timeout=15)
                with open(raw_cover, 'wb') as f:
                    f.write(resp.read())
                # 缩放到 320x320 以内（保持比例）
                subprocess.run(
                    ['ffmpeg', '-y', '-i', str(raw_cover),
                     '-vf', 'scale=320:320:force_original_aspect_ratio=decrease',
                     '-q:v', '5', str(cover_path)],
                    capture_output=True, timeout=30
                )
                raw_cover.unlink(missing_ok=True)
                if cover_path.exists():
                    logger.info("封面下载完成: %d KB", cover_path.stat().st_size // 1024)
                else:
                    cover_path = None
            except Exception as e:
                logger.warning("封面下载失败: %s", e)
                cover_path = None
        else:
            cover_path = None

        # 5. 转换格式 + 嵌入封面
        target_ext = AUDIO_FORMAT if AUDIO_FORMAT != 'best' else audio_ext
        target_path = DOWNLOAD_DIR / f"{title} - {uploader}.{target_ext}"

        if target_ext != audio_ext:
            # 用 ffmpeg 转码 + 嵌入封面
            cmd = ['ffmpeg', '-y', '-i', str(temp_path)]
            if cover_path and cover_path.exists():
                cmd += ['-i', str(cover_path)]
            if target_ext == 'flac':
                cmd += ['-c:a', 'flac']
            elif target_ext == 'aac':
                cmd += ['-c:a', 'aac', '-b:a', '320k']
            elif target_ext == 'mp3':
                cmd += ['-c:a', 'libmp3lame', '-b:a', '320k']
            if cover_path and cover_path.exists():
                cmd += ['-c:v', 'mjpeg', '-map', '0:a', '-map', '1:v',
                        '-disposition:v:0', 'attached_pic',
                        '-metadata:s:v', 'title=Album cover',
                        '-metadata:s:v', 'comment=Cover (front)']
            cmd += ['-metadata', f'title={title}', '-metadata', f'artist={uploader}']
            cmd.append(str(target_path))

            logger.info("转码: %s → %s (含封面)", audio_ext, target_ext)
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode != 0:
                logger.error("ffmpeg 转码失败: %s", result.stderr.decode()[:200])
                temp_path.rename(target_path)
            else:
                temp_path.unlink(missing_ok=True)
        else:
            temp_path.rename(target_path)

        # 保留封面文件供 Telegram 缩略图使用
        # （下载完成后由 bot 层清理）

        file_size = target_path.stat().st_size
        logger.info("B站下载完成: %s (%s, %d MB)", target_path.name, target_ext, file_size // 1024 // 1024)

        return DownloadResult(
            success=True,
            file_path=target_path,
            cover_path=cover_path if cover_path and cover_path.exists() else None,
            title=title,
            uploader=uploader,
            duration=duration,
            format_name=target_ext,
            file_size=file_size,
        )

    except Exception as e:
        logger.exception("B站下载失败: %s", url)
        return DownloadResult(success=False, error=str(e))


# ──────────────────────────────────────────────
#  YouTube 等：使用 yt-dlp
# ──────────────────────────────────────────────

def _get_ydl_opts(progress_hook=None) -> dict:
    format_map = {"flac": "flac", "aac": "aac", "best": "best"}
    audio_fmt = format_map.get(AUDIO_FORMAT, "flac")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(DOWNLOAD_DIR / "%(title)s - %(uploader)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_fmt if audio_fmt != "best" else None,
            "preferredquality": "0",
        }],
        "writethumbnail": True,
        "concurrent_fragment_downloads": 4,
        "retries": 3,
        "fragment_retries": 3,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    if YTDL_PROXY:
        opts["proxy"] = YTDL_PROXY
        logger.info("yt-dlp 使用代理: %s", YTDL_PROXY)

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    return opts


def _download_ytdlp(url: str, progress_hook=None) -> DownloadResult:
    """用 yt-dlp 下载（YouTube 等平台）"""
    opts = _get_ydl_opts(progress_hook)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = _sanitize_filename(info.get("title", "unknown"))
            uploader = _sanitize_filename(info.get("uploader", "unknown"))
            duration = info.get("duration", 0)

            ydl.download([url])

            audio_ext = AUDIO_FORMAT if AUDIO_FORMAT != "best" else "m4a"
            file_path = None
            for ext in [audio_ext, "flac", "aac", "m4a", "opus", "mp3", "wav"]:
                candidate = DOWNLOAD_DIR / f"{title} - {uploader}.{ext}"
                if candidate.exists():
                    file_path = candidate
                    break

            if file_path is None:
                matches = list(DOWNLOAD_DIR.glob(f"{title} - {uploader}.*"))
                matches = [m for m in matches if m.suffix.lower() not in ('.jpg', '.png', '.webp')]
                if matches:
                    file_path = matches[0]

            if file_path is None:
                return DownloadResult(success=False, title=title, uploader=uploader,
                                      error="下载完成但未找到输出文件")

            file_size = file_path.stat().st_size
            return DownloadResult(
                success=True, file_path=file_path, title=title, uploader=uploader,
                duration=duration, format_name=file_path.suffix.lstrip('.'),
                file_size=file_size,
            )
    except Exception as e:
        logger.exception("yt-dlp 下载失败: %s", url)
        return DownloadResult(success=False, error=str(e))


# ──────────────────────────────────────────────
#  统一入口
# ──────────────────────────────────────────────

def download_audio(url: str, progress_hook=None) -> DownloadResult:
    """自动选择引擎下载音频"""
    if _is_bilibili(url):
        logger.info("使用 B站 API 引擎: %s", url)
        return _download_bilibili(url, progress_hook)
    else:
        logger.info("使用 yt-dlp 引擎: %s", url)
        return _download_ytdlp(url, progress_hook)
