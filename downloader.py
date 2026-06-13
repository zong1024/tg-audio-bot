"""下载封装 - B站用 API 直接下载，YouTube 用 yt-dlp

修复的 Bug 清单：
- Bug 2: ffmpeg 失败不再静默重命名（返回错误）
- Bug 3: Dolby 使用标准扩展名 ac3
- Bug 4/5: 完整性校验收紧到 97%，双向检查
- Bug 6/7: 下载字节数累计追踪 + 文件大小验证
- Bug 9: 格式匹配时也嵌入封面和元数据
- Bug 10: 始终使用 -map 0:a 显式选择音频流
- Bug 12: 下载 0 字节检测
- Bug 13: 多P视频提示用户
- Bug 14: 启动时清理残留临时文件
"""

import re
import os
import json
import time as _time
import logging
import subprocess
import urllib.request
from pathlib import Path
from dataclasses import dataclass

import yt_dlp

from config import DOWNLOAD_DIR, AUDIO_FORMAT, BILIBILI_COOKIES, YTDL_PROXY

logger = logging.getLogger(__name__)

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


# ── B站 API ──────────────────────────────────────

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Referer': 'https://www.bilibili.com',
}


def _extract_bvid(url: str) -> str | None:
    m = re.search(r'(BV[a-zA-Z0-9]+)', url)
    if m:
        return m.group(1)
    if 'b23.tv' in url:
        try:
            import http.client
            from urllib.parse import urlparse
            parsed = urlparse(url)
            conn = http.client.HTTPSConnection(parsed.hostname, timeout=10)
            conn.request('GET', parsed.path, headers=_HEADERS)
            resp = conn.getresponse()
            location = resp.getheader('Location', '')
            conn.close()
            logger.info("b23.tv → %s", location[:80])
            m = re.search(r'(BV[a-zA-Z0-9]+)', location)
            if m:
                return m.group(1)
            req2 = urllib.request.Request(url, headers=_HEADERS)
            resp2 = urllib.request.urlopen(req2, timeout=10)
            m = re.search(r'(BV[a-zA-Z0-9]+)', resp2.url)
            if m:
                return m.group(1)
        except Exception as e:
            logger.error("b23.tv 解析失败: %s", e)
    return None


def _bili_api_get(url: str) -> dict:
    req = urllib.request.Request(url, headers=_HEADERS)
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def _cleanup_temp_files():
    """Bug 14: 清理残留临时文件"""
    for f in DOWNLOAD_DIR.glob("*_temp.*"):
        try:
            f.unlink()
            logger.info("清理临时文件: %s", f.name)
        except Exception:
            pass


def _download_bilibili(url: str, progress_hook=None) -> DownloadResult:
    bvid = _extract_bvid(url)
    if not bvid:
        return DownloadResult(success=False, error=f"无法提取 BV 号: {url}")

    try:
        _cleanup_temp_files()  # Bug 14

        # 1. 获取视频信息
        info = _bili_api_get(f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}')
        if info.get('code') != 0:
            return DownloadResult(success=False, error=f"B站 API: {info.get('message')}")

        data = info['data']
        title = _sanitize_filename(data.get('title', 'unknown'))
        uploader = _sanitize_filename(data.get('owner', {}).get('name', 'unknown'))
        cid = data.get('cid') or data['pages'][0]['cid']
        duration = data.get('duration', 0)
        pages = data.get('pages', [])

        # Bug 13: 多P视频提示
        if len(pages) > 1:
            logger.warning("多P视频 (共%dP)，仅下载第1P: %s", len(pages), pages[0].get('part', ''))

        logger.info("B站: %s - %s (cid=%s, %ds)", title, uploader, cid, duration)

        # 2. 获取音频流
        play_data = _bili_api_get(
            f'https://api.bilibili.com/x/player/playurl'
            f'?bvid={bvid}&cid={cid}&fnval=4048&qn=0&fourk=1'
        )
        if play_data.get('code') != 0:
            return DownloadResult(success=False, error=f"播放信息: {play_data.get('message')}")

        dash = play_data.get('data', {}).get('dash')
        if not dash:
            return DownloadResult(success=False, error="无 DASH 流")

        # 3. 选择最佳音频流
        audio_url = None
        audio_ext = 'm4a'

        # Hi-Res FLAC
        flac = dash.get('flac', {})
        if flac and flac.get('audio'):
            audio_url = flac['audio'].get('baseUrl') or flac['audio'].get('base_url')
            audio_ext = 'flac'
            logger.info("Hi-Res FLAC, bw=%s", flac['audio'].get('bandwidth'))

        # Dolby（Bug 3: 使用标准扩展名 ac3）
        if not audio_url:
            dolby = dash.get('dolby', {})
            if dolby and dolby.get('audio'):
                streams = dolby['audio']
                if isinstance(streams, list) and streams:
                    best = max(streams, key=lambda s: s.get('bandwidth', 0))
                    audio_url = best.get('baseUrl') or best.get('base_url')
                    audio_ext = 'ac3'
                    logger.info("Dolby, bw=%s", best.get('bandwidth'))

        # 普通音频
        if not audio_url:
            audio_streams = dash.get('audio', [])
            if not audio_streams:
                return DownloadResult(success=False, error="无音频流")
            best = max(audio_streams, key=lambda s: (s.get('id', 0), s.get('bandwidth', 0)))
            audio_url = best.get('baseUrl') or best.get('base_url')
            logger.info("普通音频, id=%s, bw=%s", best.get('id'), best.get('bandwidth'))

        # 4. 下载（带重试 + 累计字节 + 大小校验）
        temp_path = DOWNLOAD_DIR / f"{title} - {uploader}_temp.{audio_ext}"

        def _do_download(src_url: str, dest: Path, hook=None) -> int:
            cumulative = dest.stat().st_size if dest.exists() else 0
            expected_total = 0
            for attempt in range(3):
                try:
                    existing = dest.stat().st_size if dest.exists() else 0
                    req = urllib.request.Request(src_url, headers={
                        **_HEADERS, 'Range': f'bytes={existing}-',
                    })
                    resp = urllib.request.urlopen(req, timeout=60)
                    content_len = int(resp.headers.get('Content-Length', 0))
                    expected_total = content_len + existing
                    cumulative = existing
                    mode = 'ab' if existing and resp.status == 206 else 'wb'
                    if mode == 'wb':
                        cumulative = 0
                    dl_start = _time.time()
                    with open(dest, mode) as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            cumulative += len(chunk)
                            if hook and expected_total > 0:
                                elapsed = _time.time() - dl_start
                                speed = cumulative / elapsed if elapsed > 0 else 0
                                eta = (expected_total - cumulative) / speed if speed > 0 else 0
                                hook({
                                    'percent': cumulative * 100 / expected_total,
                                    'downloaded_bytes': cumulative,
                                    'total_bytes': expected_total,
                                    'speed': speed,
                                    'eta': int(eta),
                                })
                    # Bug 12: 检查下载是否为空
                    if cumulative == 0:
                        raise IOError("下载 0 字节")
                    return cumulative
                except Exception as e:
                    logger.warning("下载重试 %d/3: %s", attempt + 1, e)
                    if attempt == 2:
                        raise
                    _time.sleep(2)
            return cumulative

        downloaded = _do_download(audio_url, temp_path, progress_hook)
        logger.info("下载完成: %d MB", downloaded // 1024 // 1024)

        # Bug 6/7: 验证文件大小
        actual_size = temp_path.stat().st_size
        if actual_size < 1024:  # 小于 1KB 肯定有问题
            temp_path.unlink(missing_ok=True)
            return DownloadResult(success=False, title=title, uploader=uploader,
                                  error=f"文件过小 ({actual_size} bytes)，下载可能失败")

        # 4.5 完整性校验（Bug 4/5: 97% 阈值 + 双向检查）
        try:
            probe = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', str(temp_path)],
                capture_output=True, text=True, timeout=30
            )
            actual_dur = float(probe.stdout.strip()) if probe.stdout.strip() else 0
            logger.info("时长: %.1fs (预期 %ds)", actual_dur, duration)
            if duration > 0:
                ratio = actual_dur / duration
                if ratio < 0.97:
                    logger.warning("音频不完整! %.1fs < %ds 的 97%%", actual_dur, duration)
                    temp_path.unlink(missing_ok=True)
                    return DownloadResult(success=False, title=title, uploader=uploader,
                                          error=f"不完整 ({actual_dur:.0f}s / {duration}s)")
                if ratio > 1.1:
                    logger.warning("音频异常长! %.1fs > %ds 的 110%%", actual_dur, duration)
        except Exception as e:
            logger.warning("时长校验失败: %s", e)

        # 4.6 下载封面 + 缩放到 320px
        cover_path = DOWNLOAD_DIR / f"{title} - {uploader}_cover.jpg"
        cover_url = data.get('pic', '')
        if cover_url:
            try:
                if cover_url.startswith('//'):
                    cover_url = 'https:' + cover_url
                raw = DOWNLOAD_DIR / f"{title} - {uploader}_cover_raw.jpg"
                req = urllib.request.Request(cover_url, headers=_HEADERS)
                with open(raw, 'wb') as f:
                    f.write(urllib.request.urlopen(req, timeout=15).read())
                subprocess.run(
                    ['ffmpeg', '-y', '-i', str(raw),
                     '-vf', 'scale=320:320:force_original_aspect_ratio=decrease',
                     '-q:v', '5', str(cover_path)],
                    capture_output=True, timeout=30
                )
                raw.unlink(missing_ok=True)
                if cover_path.exists():
                    logger.info("封面: %d KB", cover_path.stat().st_size // 1024)
                else:
                    cover_path = None
            except Exception as e:
                logger.warning("封面失败: %s", e)
                cover_path = None
        else:
            cover_path = None

        # 5. 转换 + 嵌入封面 + 元数据（Bug 9: 始终执行，不再跳过）
        target_ext = AUDIO_FORMAT if AUDIO_FORMAT != 'best' else audio_ext
        target_path = DOWNLOAD_DIR / f"{title} - {uploader}.{target_ext}"

        cmd = ['ffmpeg', '-y', '-i', str(temp_path)]
        if cover_path and cover_path.exists():
            cmd += ['-i', str(cover_path)]

        # Bug 10: 始终显式选择音频流
        cmd += ['-map', '0:a']

        if target_ext != audio_ext:
            # 需要转码
            if target_ext == 'flac':
                cmd += ['-c:a', 'flac']
            elif target_ext == 'aac':
                cmd += ['-c:a', 'aac', '-b:a', '320k']
            elif target_ext == 'mp3':
                cmd += ['-c:a', 'libmp3lame', '-b:a', '320k']
            elif target_ext == 'ac3':
                cmd += ['-c:a', 'ac3']
        else:
            # 格式匹配，仅复制音频（不重新编码）
            cmd += ['-c:a', 'copy']

        # 嵌入封面
        if cover_path and cover_path.exists():
            cmd += ['-map', '1:v', '-c:v', 'mjpeg',
                    '-disposition:v:0', 'attached_pic',
                    '-metadata:s:v', 'title=Album cover',
                    '-metadata:s:v', 'comment=Cover (front)']

        # 元数据
        cmd += ['-metadata', f'title={title}', '-metadata', f'artist={uploader}']
        cmd.append(str(target_path))

        logger.info("ffmpeg: %s → %s%s", audio_ext, target_ext,
                     " + 封面" if cover_path else "")
        result = subprocess.run(cmd, capture_output=True, timeout=300)

        # Bug 2: ffmpeg 失败时返回错误，不静默重命名
        if result.returncode != 0:
            err_msg = result.stderr.decode()[:300]
            logger.error("ffmpeg 失败: %s", err_msg)
            temp_path.unlink(missing_ok=True)
            return DownloadResult(
                success=False, title=title, uploader=uploader,
                error=f"转码失败: {err_msg[:100]}"
            )

        temp_path.unlink(missing_ok=True)

        file_size = target_path.stat().st_size
        logger.info("完成: %s (%s, %d MB)", target_path.name, target_ext, file_size // 1024 // 1024)

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


# ── YouTube 等（yt-dlp）──────────────────────────

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
        logger.info("yt-dlp 代理: %s", YTDL_PROXY)
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts


def _download_ytdlp(url: str, progress_hook=None) -> DownloadResult:
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
                matches = [m for m in DOWNLOAD_DIR.glob(f"{title} - {uploader}.*")
                           if m.suffix.lower() not in ('.jpg', '.png', '.webp')]
                if matches:
                    file_path = matches[0]
            if file_path is None:
                return DownloadResult(success=False, title=title, uploader=uploader,
                                      error="找不到输出文件")
            return DownloadResult(
                success=True, file_path=file_path, title=title, uploader=uploader,
                duration=duration, format_name=file_path.suffix.lstrip('.'),
                file_size=file_path.stat().st_size,
            )
    except Exception as e:
        logger.exception("yt-dlp 失败: %s", url)
        return DownloadResult(success=False, error=str(e))


# ── 统一入口 ──────────────────────────────────────

def download_audio(url: str, progress_hook=None) -> DownloadResult:
    if _is_bilibili(url):
        logger.info("B站 API: %s", url)
        return _download_bilibili(url, progress_hook)
    else:
        logger.info("yt-dlp: %s", url)
        return _download_ytdlp(url, progress_hook)
