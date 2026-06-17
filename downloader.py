"""下载封装 - B站用 API 直接下载，YouTube 用 yt-dlp

超时与可靠性策略：
- 每个 socket 连接有独立 timeout（BILI_DL_SOCKET_TIMEOUT）
- 整体下载任务有总超时（DOWNLOAD_TOTAL_TIMEOUT），由 bot 层 asyncio.wait_for 控制
- 重试 3 次，每次间隔 2 秒
- 所有 response 对象用 with/try-finally 确保关闭
- ffmpeg 转码有独立超时（FFMPEG_TIMEOUT）
"""

import re
import os
import json
import time as _time
import logging
import subprocess
import urllib.request
import http.cookiejar
from pathlib import Path
from dataclasses import dataclass

import yt_dlp

from config import (
    DOWNLOAD_DIR, AUDIO_FORMAT, BILIBILI_COOKIES, YTDL_PROXY,
    BILI_API_TIMEOUT, BILI_DL_SOCKET_TIMEOUT, FFMPEG_TIMEOUT,
)

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
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Referer': 'https://www.bilibili.com',
}

# B站 Cookie-aware opener（登录态大幅提升频率限制）
_bili_opener = None
_last_bili_request = 0.0


def _get_bili_opener():
    """构建带 Cookies 的 urllib opener（单例）"""
    global _bili_opener
    if _bili_opener is not None:
        return _bili_opener

    cj = http.cookiejar.MozillaCookieJar()
    if BILIBILI_COOKIES and Path(BILIBILI_COOKIES).exists():
        try:
            cj.load(BILIBILI_COOKIES, ignore_discard=True, ignore_expires=True)
            logger.info("已加载 B站 Cookies: %s (%d 条)", BILIBILI_COOKIES, len(cj))
        except Exception as e:
            logger.warning("加载 B站 Cookies 失败: %s", e)
    else:
        logger.info("未配置 B站 Cookies，频率限制较严格")

    _bili_opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj)
    )
    return _bili_opener


def _bili_api_get(url: str) -> dict:
    """调用 B站 API，带 Cookies + 频率限制 + 514 退避"""
    global _last_bili_request
    opener = _get_bili_opener()

    # 频率限制：每次请求间隔至少 500ms
    elapsed = _time.time() - _last_bili_request
    if elapsed < 0.5:
        _time.sleep(0.5 - elapsed)

    for attempt in range(3):
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with opener.open(req, timeout=BILI_API_TIMEOUT) as resp:
                _last_bili_request = _time.time()
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 514:
                wait = 5 * (attempt + 1)  # 5s, 10s, 15s 退避
                logger.warning("B站频率限制 (514)，等待 %ds 后重试 %d/3", wait, attempt + 1)
                _time.sleep(wait)
                if attempt == 2:
                    raise
            else:
                raise
    # unreachable but keeps type checker happy
    raise RuntimeError("B站 API 重试耗尽")


def _extract_bvid(url: str) -> str | None:
    # 直接匹配 BV 号
    m = re.search(r'(BV[a-zA-Z0-9]+)', url)
    if m:
        return m.group(1)

    # b23.tv 短链 → 解析重定向
    if 'b23.tv' in url:
        # 方法 1: HTTPSConnection（可控超时，确保关闭）
        conn = None
        try:
            import http.client
            from urllib.parse import urlparse
            parsed = urlparse(url)
            conn = http.client.HTTPSConnection(parsed.hostname, timeout=10)
            conn.request('GET', parsed.path, headers=_HEADERS)
            resp = conn.getresponse()
            location = resp.getheader('Location', '')
            logger.info("b23.tv → %s", location[:80])
            m = re.search(r'(BV[a-zA-Z0-9]+)', location)
            if m:
                return m.group(1)
        except Exception as e:
            logger.warning("b23.tv HTTPS 解析失败: %s", e)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        # 方法 2: urllib 跟随重定向
        try:
            req2 = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                final_url = resp2.url
            logger.info("b23.tv 最终 URL: %s", final_url[:80])
            m = re.search(r'(BV[a-zA-Z0-9]+)', final_url)
            if m:
                return m.group(1)
        except Exception as e:
            logger.warning("b23.tv urllib 解析失败: %s", e)

    return None


def _cleanup_temp_files():
    """清理残留临时文件"""
    for f in DOWNLOAD_DIR.glob("*_temp.*"):
        try:
            f.unlink()
            logger.info("清理临时文件: %s", f.name)
        except Exception as e:
            logger.debug("清理临时文件失败 %s: %s", f.name, e)


def _download_bilibili(url: str, progress_hook=None) -> DownloadResult:
    bvid = _extract_bvid(url)
    if not bvid:
        return DownloadResult(success=False, error="无法提取 BV 号，请检查链接")

    try:
        _cleanup_temp_files()

        # 1. 获取视频信息
        info = _bili_api_get(f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}')
        if info.get('code') != 0:
            return DownloadResult(success=False, error=f"B站返回错误: {info.get('message')}")

        data = info['data']
        title = _sanitize_filename(data.get('title', 'unknown'))
        uploader = _sanitize_filename(data.get('owner', {}).get('name', 'unknown'))
        cid = data.get('cid') or data['pages'][0]['cid']
        duration = data.get('duration', 0)
        pages = data.get('pages', [])

        if len(pages) > 1:
            logger.warning("多P视频 (共%dP)，仅下载第1P: %s", len(pages), pages[0].get('part', ''))

        logger.info("B站: %s - %s (cid=%s, %ds)", title, uploader, cid, duration)

        # 2. 获取音频流
        play_data = _bili_api_get(
            f'https://api.bilibili.com/x/player/playurl'
            f'?bvid={bvid}&cid={cid}&fnval=4048&qn=0&fourk=1'
        )
        if play_data.get('code') != 0:
            return DownloadResult(success=False, error=f"播放信息获取失败: {play_data.get('message')}")

        dash = play_data.get('data', {}).get('dash')
        if not dash:
            return DownloadResult(success=False, error="未获取到 DASH 音频流")

        # 3. 选择最佳音频流（Hi-Res FLAC > Dolby > 普通）
        audio_url = None
        audio_ext = 'm4a'

        flac_info = dash.get('flac', {})
        if flac_info and flac_info.get('audio'):
            audio_url = flac_info['audio'].get('baseUrl') or flac_info['audio'].get('base_url')
            audio_ext = 'flac'
            logger.info("Hi-Res FLAC, bw=%s", flac_info['audio'].get('bandwidth'))

        if not audio_url:
            dolby_info = dash.get('dolby', {})
            if dolby_info and dolby_info.get('audio'):
                streams = dolby_info['audio']
                if isinstance(streams, list) and streams:
                    best = max(streams, key=lambda s: s.get('bandwidth', 0))
                    audio_url = best.get('baseUrl') or best.get('base_url')
                    audio_ext = 'ac3'
                    logger.info("Dolby, bw=%s", best.get('bandwidth'))

        if not audio_url:
            audio_streams = dash.get('audio', [])
            if not audio_streams:
                return DownloadResult(success=False, error="没有可用的音频流")
            best = max(audio_streams, key=lambda s: (s.get('id', 0), s.get('bandwidth', 0)))
            audio_url = best.get('baseUrl') or best.get('base_url')
            logger.info("普通音频, id=%s, bw=%s", best.get('id'), best.get('bandwidth'))

        # 4. 下载（带重试 + 累计字节 + 速率熔断）
        temp_path = DOWNLOAD_DIR / f"{title} - {uploader}_temp.{audio_ext}"

        def _do_download(src_url: str, dest: Path, hook=None) -> int:
            """下载文件，3 次重试，带速率熔断 + 514 退避"""
            cumulative = 0
            expected_total = 0
            STALL_TIMEOUT = 30
            opener = _get_bili_opener()

            for attempt in range(3):
                try:
                    existing = dest.stat().st_size if dest.exists() else 0
                    req = urllib.request.Request(src_url, headers={
                        **_HEADERS, 'Range': f'bytes={existing}-',
                    })
                    resp = opener.open(req, timeout=BILI_DL_SOCKET_TIMEOUT)
                    try:
                        content_len = int(resp.headers.get('Content-Length', 0))
                        expected_total = content_len + existing
                        cumulative = existing
                        mode = 'ab' if existing and resp.status == 206 else 'wb'
                        if mode == 'wb':
                            cumulative = 0
                        dl_start = _time.time()
                        last_data_time = _time.time()

                        with open(dest, mode) as f:
                            while True:
                                chunk = resp.read(65536)
                                if not chunk:
                                    break
                                f.write(chunk)
                                cumulative += len(chunk)
                                last_data_time = _time.time()

                                # 速率熔断：连续 STALL_TIMEOUT 秒无数据
                                if _time.time() - last_data_time > STALL_TIMEOUT:
                                    raise IOError(f"下载卡死 {STALL_TIMEOUT}s 无数据")

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
                    finally:
                        resp.close()

                    if cumulative == 0:
                        raise IOError("下载 0 字节")
                    return cumulative

                except Exception as e:
                    logger.warning("下载重试 %d/3: %s (已下载 %d KB)",
                                   attempt + 1, e, cumulative // 1024)
                    if attempt == 2:
                        raise
                    # 514 频率限制 → 更长退避
                    if hasattr(e, 'code') and e.code == 514:
                        _time.sleep(10 * (attempt + 1))
                    else:
                        _time.sleep(2)

            return cumulative

        downloaded = _do_download(audio_url, temp_path, progress_hook)
        logger.info("下载完成: %d MB", downloaded // 1024 // 1024)

        # 4.5 验证文件大小
        actual_size = temp_path.stat().st_size
        if actual_size < 1024:
            temp_path.unlink(missing_ok=True)
            return DownloadResult(success=False, title=title, uploader=uploader,
                                  error=f"文件过小 ({actual_size} bytes)，下载可能失败")

        # 4.6 完整性校验（时长检查）
        try:
            probe = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', str(temp_path)],
                capture_output=True, text=True, timeout=30
            )
            actual_dur = float(probe.stdout.strip()) if probe.stdout.strip() else 0
            logger.info("时长: %.1fs (预期 %ds)", actual_dur, duration)
            if duration > 0 and actual_dur > 0:
                ratio = actual_dur / duration
                if ratio < 0.97:
                    logger.warning("音频不完整! %.1fs < %ds 的 97%%", actual_dur, duration)
                    temp_path.unlink(missing_ok=True)
                    return DownloadResult(success=False, title=title, uploader=uploader,
                                          error=f"音频不完整 ({actual_dur:.0f}s / {duration}s)")
                if ratio > 1.1:
                    logger.warning("音频异常长! %.1fs > %ds 的 110%%", actual_dur, duration)
            elif actual_dur == 0:
                logger.warning("ffprobe 返回 0 时长，文件可能损坏")
        except subprocess.TimeoutExpired:
            logger.warning("ffprobe 超时（30s），跳过时长校验")
        except Exception as e:
            logger.warning("时长校验异常: %s", e)

        # 4.7 下载封面 + 缩放到 320px
        cover_path = DOWNLOAD_DIR / f"{title} - {uploader}_cover.jpg"
        cover_url = data.get('pic', '')
        if cover_url:
            try:
                if cover_url.startswith('//'):
                    cover_url = 'https:' + cover_url
                raw = DOWNLOAD_DIR / f"{title} - {uploader}_cover_raw.jpg"
                req = urllib.request.Request(cover_url, headers=_HEADERS)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    with open(raw, 'wb') as f:
                        f.write(resp.read())
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
                logger.warning("封面下载失败: %s", e)
                cover_path = None
        else:
            cover_path = None

        # 5. 转换 + 嵌入封面 + 元数据
        target_ext = AUDIO_FORMAT if AUDIO_FORMAT != 'best' else audio_ext
        target_path = DOWNLOAD_DIR / f"{title} - {uploader}.{target_ext}"

        cmd = ['ffmpeg', '-y', '-i', str(temp_path)]
        if cover_path and cover_path.exists():
            cmd += ['-i', str(cover_path)]
        cmd += ['-map', '0:a']

        if target_ext != audio_ext:
            if target_ext == 'flac':
                cmd += ['-c:a', 'flac']
            elif target_ext == 'aac':
                cmd += ['-c:a', 'aac', '-b:a', '320k']
            elif target_ext == 'mp3':
                cmd += ['-c:a', 'libmp3lame', '-b:a', '320k']
            elif target_ext == 'ac3':
                cmd += ['-c:a', 'ac3']
        else:
            cmd += ['-c:a', 'copy']

        if cover_path and cover_path.exists():
            cmd += ['-map', '1:v', '-c:v', 'mjpeg',
                    '-disposition:v:0', 'attached_pic',
                    '-metadata:s:v', 'title=Album cover',
                    '-metadata:s:v', 'comment=Cover (front)']

        cmd += ['-metadata', f'title={title}', '-metadata', f'artist={uploader}']
        cmd.append(str(target_path))

        logger.info("ffmpeg: %s → %s%s", audio_ext, target_ext,
                     " + 封面" if cover_path else "")
        result = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT)

        if result.returncode != 0:
            err_full = result.stderr.decode()
            err_short = err_full[:150]
            logger.error("ffmpeg 失败 (完整 stderr):\n%s", err_full)
            temp_path.unlink(missing_ok=True)
            return DownloadResult(
                success=False, title=title, uploader=uploader,
                error=f"转码失败: {err_short}"
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
        "socket_timeout": 30,
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
    audio_exts = {'.flac', '.aac', '.m4a', '.mp3', '.opus', '.wav', '.ogg'}
    before = {f for f in DOWNLOAD_DIR.iterdir() if f.suffix.lower() in audio_exts}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            duration = info.get("duration", 0)
            title = info.get("title", "unknown")
            uploader = info.get("uploader", "unknown")

            after = {f for f in DOWNLOAD_DIR.iterdir() if f.suffix.lower() in audio_exts}
            new_files = after - before

            if new_files:
                file_path = max(new_files, key=lambda f: f.stat().st_mtime)
            else:
                # fallback: 最近 60 秒修改的文件
                file_path = None
                cutoff = _time.time() - 60
                for ext in ["flac", "aac", "m4a", "mp3", "opus", "wav"]:
                    for f in DOWNLOAD_DIR.glob(f"*{ext}"):
                        if f.stat().st_mtime > cutoff:
                            file_path = f
                            break
                    if file_path:
                        break

            if file_path is None:
                return DownloadResult(success=False, title=title, uploader=uploader,
                                      error="找不到输出文件")

            # 清理 yt-dlp 下载的封面
            for img in DOWNLOAD_DIR.glob(f"{file_path.stem}.*"):
                if img.suffix.lower() in ('.jpg', '.png', '.webp'):
                    img.unlink(missing_ok=True)

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
