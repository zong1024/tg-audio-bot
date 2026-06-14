"""
Telegram Bot - B站/YouTube 高清音频下载（支持批量 + 队列）
"""

import logging
import asyncio
import time as _time
from dataclasses import dataclass, field
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import (
    BOT_TOKEN, DOWNLOAD_DIR, AUDIO_FORMAT, ALLOWED_USERS, TG_FILE_LIMIT,
    HTTP_PROXY, SOCKS_PROXY, LOCAL_API_URL,
)
from downloader import is_supported_url, download_audio, DownloadResult

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _check_user_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


def _progress_bar(pct: float, total: int = 10) -> str:
    filled = round(total * pct / 100)
    return "🟣" * filled + "⚪" * (total - filled)


def _fmt_bytes(b: float) -> str:
    if b >= 1024 * 1024:
        return f"{b / 1024 / 1024:.1f} MB"
    return f"{b / 1024:.0f} KB"


# ── 下载队列 ──────────────────────────────────────

@dataclass
class QueueItem:
    url: str
    user_id: int
    message: object          # telegram.Message（发送链接的原始消息）
    status_msg: object = None  # telegram.Message（进度消息）
    position: int = 0


download_queue: asyncio.Queue = None
queue_items: list[QueueItem] = []   # 可视化队列（正在执行 + 等待中）


async def _process_queue():
    """后台 worker：逐个处理队列中的下载任务"""
    while True:
        item: QueueItem = await download_queue.get()
        try:
            await _execute_download(item)
        except Exception as e:
            logger.exception("队列任务异常: %s", item.url)
            try:
                await item.status_msg.edit_text(f"❌ 异常: {e}", parse_mode=None)
            except Exception:
                pass
        finally:
            if item in queue_items:
                queue_items.remove(item)
            download_queue.task_done()


async def _execute_download(item: QueueItem):
    """执行单个下载任务"""
    url = item.url
    message = item.message
    status_msg = item.status_msg

    # 更新队列位置
    _update_queue_positions()

    # ── 进度状态 ──
    progress = {
        "started": False, "pct": 0.0,
        "downloaded": 0, "total": 0,
        "speed": None, "eta": None,
        "done": False,
    }

    def _update_progress(d: dict):
        progress["started"] = True
        progress["pct"] = min(max(float(d.get("percent", 0)), 0), 100)
        progress["downloaded"] = d.get("downloaded_bytes", 0)
        progress["total"] = d.get("total_bytes", 0) or d.get("total_bytes_estimate", 0)
        progress["speed"] = d.get("speed")
        progress["eta"] = d.get("eta")

    # 异步轮询进度
    async def _poll_progress():
        last_text = ""
        while not progress["done"]:
            await asyncio.sleep(1.5)
            if not progress["started"]:
                continue
            p = progress["pct"]
            bar = _progress_bar(p)
            sz = _fmt_bytes(progress["downloaded"])
            sz_t = f" / {_fmt_bytes(progress['total'])}" if progress["total"] else ""
            spd = f"{_fmt_bytes(progress['speed'])}/s" if progress["speed"] else ""
            eta = f"剩余 {progress['eta']}s" if progress["eta"] else ""
            info = " · ".join(filter(None, [f"{sz}{sz_t}", spd, eta]))
            queue_hint = _queue_hint_text()
            txt = f"⬇️ 下载中 {p:.1f}%\n{bar}\n📦 {info}"
            if queue_hint:
                txt += f"\n{queue_hint}"
            if txt == last_text:
                continue
            last_text = txt
            try:
                await status_msg.edit_text(txt, parse_mode=None)
            except Exception:
                pass

    poll_task = asyncio.create_task(_poll_progress())

    result: DownloadResult = await asyncio.get_event_loop().run_in_executor(
        None, download_audio, url, _update_progress
    )
    progress["done"] = True
    await poll_task

    # ── 结果处理 ──
    if not result.success:
        await status_msg.edit_text(f"❌ 下载失败\n\n{result.error}", parse_mode=None)
        return

    duration_str = ""
    if result.duration:
        m, s = divmod(result.duration, 60)
        h, m = divmod(m, 60)
        duration_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    size_mb = result.file_size / (1024 * 1024)
    queue_hint = _queue_hint_text()
    done_text = (
        f"✅ 下载完成\n\n"
        f"🎵 {result.title}\n"
        f"👤 {result.uploader}\n"
        f"⏱ {duration_str}\n"
        f"💿 {result.format_name.upper()}  📦 {size_mb:.1f} MB\n"
        f"📂 已保存到 NAS"
    )
    if queue_hint:
        done_text += f"\n{queue_hint}"

    # 发送文件
    if result.file_size <= TG_FILE_LIMIT and result.file_path:
        try:
            await status_msg.edit_text(done_text + "\n\n📤 正在发送...", parse_mode=None)
            thumb = None
            if result.cover_path and result.cover_path.exists():
                thumb = open(result.cover_path, "rb")
            with open(result.file_path, "rb") as f:
                await message.reply_audio(
                    audio=f,
                    title=result.title,
                    performer=result.uploader,
                    duration=result.duration or 0,
                    caption=f"🎵 {result.title}",
                    thumbnail=thumb,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )
            if thumb:
                thumb.close()
            if result.cover_path and result.cover_path.exists():
                result.cover_path.unlink(missing_ok=True)
            done_text += "\n📤 已发送到聊天"
        except Exception as e:
            logger.warning("发送失败: %s", e)
            if result.cover_path and result.cover_path.exists():
                result.cover_path.unlink(missing_ok=True)
            done_text += "\n📤 发送失败"

    await status_msg.edit_text(done_text, parse_mode=None)


def _update_queue_positions():
    """更新队列中等待项的位置显示"""
    for i, item in enumerate(queue_items):
        item.position = i


def _queue_hint_text() -> str:
    """生成队列状态提示"""
    waiting = [it for it in queue_items if it.position > 0]
    if not waiting:
        return ""
    return f"📋 队列中还有 {len(waiting)} 个任务等待下载"


# ── 命令 ─────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 <b>音频下载 Bot</b>\n\n"
        "发送链接即可下载，支持批量：\n"
        "• 一次发多个链接（换行分隔）\n"
        "• 随时发新链接，自动排队\n\n"
        "支持平台：\n"
        "• bilibili.com / b23.tv\n"
        "• youtube.com / youtu.be\n\n"
        f"音频格式：<code>{AUDIO_FORMAT.upper()}</code>\n\n"
        "命令：\n"
        "/start - 帮助\n"
        "/status - 状态\n"
        "/queue - 查看下载队列\n"
        "/scan - 扫描音乐库",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import shutil
    usage = shutil.disk_usage(str(DOWNLOAD_DIR))
    await update.message.reply_text(
        f"📊 <b>Bot 状态</b>\n\n"
        f"音频格式：<code>{AUDIO_FORMAT.upper()}</code>\n"
        f"磁盘剩余：<code>{usage.free / (1024**3):.1f} GB</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看当前下载队列"""
    if not queue_items:
        await update.message.reply_text("📋 下载队列为空", parse_mode=None)
        return

    lines = [f"📋 下载队列  共 {len(queue_items)} 个任务\n"]
    for i, item in enumerate(queue_items):
        label = "⬇️ 执行中" if i == 0 else f"⏳ 等待 #{i}"
        lines.append(f"{label}  {item.url[:60]}...")

    await update.message.reply_text("\n".join(lines), parse_mode=None)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import subprocess
    audio_exts = {'.flac', '.aac', '.m4a', '.mp3', '.opus', '.wav', '.ogg', '.eac3'}
    files = sorted(
        [f for f in DOWNLOAD_DIR.iterdir() if f.suffix.lower() in audio_exts],
        key=lambda f: f.stat().st_mtime, reverse=True,
    )
    if not files:
        await update.message.reply_text("📂 音乐文件夹为空", parse_mode=None)
        return

    total_size = sum(f.stat().st_size for f in files)
    lines = [f"📂 音乐库  共 {len(files)} 首  {_fmt_bytes(total_size)}\n"]
    for i, f in enumerate(files, 1):
        size = f.stat().st_size
        duration_str = ""
        try:
            p = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', str(f)],
                capture_output=True, text=True, timeout=10
            )
            if p.stdout.strip():
                secs = float(p.stdout.strip())
                m, s = divmod(int(secs), 60)
                duration_str = f"  {m}:{s:02d}"
        except Exception:
            pass
        lines.append(f"{i}. 🎵 {f.stem}\n    {f.suffix.lstrip('.').upper()} · {_fmt_bytes(size)}{duration_str}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + f"\n\n... 共 {len(files)} 首，已截断"
    await update.message.reply_text(text, parse_mode=None)


# ── 消息处理 ──────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    user_id = message.from_user.id
    text = message.text.strip()

    if not _check_user_allowed(user_id):
        await message.reply_text("❌ 无权限")
        return

    # 提取所有链接（支持换行/空格分隔的多链接）
    urls = []
    for word in text.replace('\n', ' ').split():
        if is_supported_url(word):
            urls.append(word)

    if not urls:
        return

    # 每个链接独立入队
    for url in urls:
        logger.info("入队: %s (用户 %s)", url, user_id)
        status_msg = await message.reply_text("📋 已加入下载队列...", parse_mode=None)
        item = QueueItem(url=url, user_id=user_id, message=message, status_msg=status_msg)
        queue_items.append(item)
        await download_queue.put(item)


# ── 启动 ──────────────────────────────────────────

def main():
    global download_queue

    if not BOT_TOKEN:
        print("❌ 请设置 BOT_TOKEN")
        return

    print(f"🎵 音频下载 Bot 启动中...")
    print(f"   格式: {AUDIO_FORMAT}")
    print(f"   目录: {DOWNLOAD_DIR}")

    download_queue = asyncio.Queue()

    builder = Application.builder().token(BOT_TOKEN)

    from telegram.request import HTTPXRequest
    req_kwargs = dict(read_timeout=120, write_timeout=120, connect_timeout=30)

    # 使用 Local Bot API Server（突破 50MB 限制）→ 不走代理（局域网直连）
    if LOCAL_API_URL:
        builder = builder.base_url(f"{LOCAL_API_URL}/bot")
        print(f"   Local API: {LOCAL_API_URL} (直连)")
    else:
        # 使用官方 API → 走代理
        proxy_url = SOCKS_PROXY or HTTP_PROXY
        if proxy_url:
            import httpx
            req_kwargs["proxy"] = httpx.Proxy(proxy_url)
            print(f"   代理: {proxy_url}")

    builder = builder.request(HTTPXRequest(**req_kwargs))

    app = builder.build()

    # 启动队列 worker
    async def post_init(application):
        asyncio.create_task(_process_queue())
        logger.info("下载队列 worker 已启动")

    app.post_init = post_init

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
