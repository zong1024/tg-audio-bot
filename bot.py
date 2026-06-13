"""
Telegram Bot - B站/YouTube 高清音频下载
"""

import logging
import asyncio
import time as _time
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
    HTTP_PROXY, SOCKS_PROXY,
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


def _esc(text: str) -> str:
    """HTML 转义"""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _progress_bar(pct: float, total: int = 10) -> str:
    """🟣🟣🟣⚪⚪⚪⚪⚪⚪⚪"""
    filled = round(total * pct / 100)
    return "🟣" * filled + "⚪" * (total - filled)


def _fmt_bytes(b: float) -> str:
    if b >= 1024 * 1024:
        return f"{b / 1024 / 1024:.1f} MB"
    return f"{b / 1024:.0f} KB"


# ── 命令 ─────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 <b>音频下载 Bot</b>\n\n"
        "发送 B站 或 YouTube 视频链接，自动提取最佳音质音频保存到 NAS。\n\n"
        "支持平台：\n"
        "• bilibili.com / b23.tv\n"
        "• youtube.com / youtu.be\n\n"
        f"音频格式：<code>{AUDIO_FORMAT.upper()}</code>\n\n"
        "命令：\n"
        "/start - 显示帮助\n"
        "/status - 查看 Bot 状态",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import shutil
    usage = shutil.disk_usage(str(DOWNLOAD_DIR))
    await update.message.reply_text(
        f"📊 <b>Bot 状态</b>\n\n"
        f"音频格式：<code>{AUDIO_FORMAT.upper()}</code>\n"
        f"存储目录：<code>{_esc(str(DOWNLOAD_DIR))}</code>\n"
        f"磁盘剩余：<code>{usage.free / (1024**3):.1f} GB</code>",
        parse_mode=ParseMode.HTML,
    )


# ── 消息处理 ──────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    user_id = message.from_user.id
    text = message.text.strip()

    if not _check_user_allowed(user_id):
        await message.reply_text("❌ 你没有使用此 Bot 的权限。")
        return

    url = None
    for word in text.split():
        if is_supported_url(word):
            url = word
            break
    if not url:
        return

    logger.info("用户 %s 请求下载: %s", user_id, url)

    # ── 阶段 1：解析 ──
    status_msg = await message.reply_text(
        "🔍 正在解析链接...\n"
        "🟣⚪⚪⚪⚪⚪⚪⚪⚪⚪",
        parse_mode=None,
    )

    # ── 阶段 2：下载（带进度） ──
    state = {"last_update": 0.0, "text": "", "started": False}

    def _update_progress(d: dict):
        now = _time.time()

        # 第一次回调 → 说明解析完成，开始下载
        if not state["started"]:
            state["started"] = True
            state["last_update"] = now
            new_text = (
                "⬇️ 开始下载...\n"
                "⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪"
            )
            try:
                loop = asyncio.get_event_loop()
                loop.call_soon_threadsafe(
                    lambda t=new_text: asyncio.ensure_future(
                        status_msg.edit_text(t, parse_mode=None)
                    )
                )
            except Exception:
                pass

        # 节流 1.5 秒
        if now - state["last_update"] < 1.5:
            return
        state["last_update"] = now

        pct = float(d.get("percent", 0))
        pct = min(max(pct, 0), 100)
        downloaded = d.get("downloaded_bytes", 0)
        total = d.get("total_bytes", 0) or d.get("total_bytes_estimate", 0)
        speed = d.get("speed")
        eta = d.get("eta")

        bar = _progress_bar(pct)
        size = _fmt_bytes(downloaded)
        size_total = f" / {_fmt_bytes(total)}" if total else ""
        speed_s = f"{_fmt_bytes(speed)}/s" if speed else ""
        eta_s = f"剩余 {eta}s" if eta else ""

        parts = " · ".join(filter(None, [f"{size}{size_total}", speed_s, eta_s]))

        new_text = (
            f"⬇️ 下载中 {pct:.1f}%\n"
            f"{bar}\n"
            f"📦 {parts}"
        )

        if new_text == state["text"]:
            return
        state["text"] = new_text

        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(
                lambda t=new_text: asyncio.ensure_future(
                    status_msg.edit_text(t, parse_mode=None)
                )
            )
        except Exception:
            pass

    # ── 执行下载 ──
    result: DownloadResult = await asyncio.get_event_loop().run_in_executor(
        None, download_audio, url, _update_progress
    )

    if not result.success:
        await status_msg.edit_text(
            f"❌ 下载失败\n\n{result.error}",
            parse_mode=None,
        )
        return

    # ── 阶段 3：完成 ──
    duration_str = ""
    if result.duration:
        m, s = divmod(result.duration, 60)
        h, m = divmod(m, 60)
        duration_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    size_mb = result.file_size / (1024 * 1024)

    done_text = (
        f"✅ 下载完成\n\n"
        f"🎵 {result.title}\n"
        f"👤 {result.uploader}\n"
        f"⏱ {duration_str}\n"
        f"💿 {result.format_name.upper()}  📦 {size_mb:.1f} MB\n"
        f"📂 已保存到 NAS"
    )

    # 小于 50MB 发送到聊天
    if result.file_size <= TG_FILE_LIMIT and result.file_path:
        try:
            await status_msg.edit_text(done_text + "\n\n📤 正在发送文件...", parse_mode=None)
            # 读取封面缩略图
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
            # 清理封面临时文件
            if result.cover_path and result.cover_path.exists():
                result.cover_path.unlink(missing_ok=True)
            done_text += "\n📤 已发送到聊天"
        except Exception as e:
            logger.warning("发送文件失败: %s", e)
            if result.cover_path and result.cover_path.exists():
                result.cover_path.unlink(missing_ok=True)
            done_text += "\n📤 发送失败（文件可能过大或网络超时）"

    await status_msg.edit_text(done_text, parse_mode=None)


# ── 启动 ──────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("❌ 请设置 BOT_TOKEN 环境变量")
        return

    print(f"🎵 音频下载 Bot 启动中...")
    print(f"   音频格式: {AUDIO_FORMAT}")
    print(f"   下载目录: {DOWNLOAD_DIR}")
    if ALLOWED_USERS:
        print(f"   白名单: {ALLOWED_USERS}")
    else:
        print(f"   所有用户可用")

    builder = Application.builder().token(BOT_TOKEN)
    proxy_url = SOCKS_PROXY or HTTP_PROXY

    # 增大超时（代理 + 大文件上传需要更长时间）
    from telegram.request import HTTPXRequest
    req_kwargs = dict(
        read_timeout=120,
        write_timeout=120,
        connect_timeout=30,
    )
    if proxy_url:
        import httpx
        req_kwargs["proxy"] = httpx.Proxy(proxy_url)
        print(f"   代理: {proxy_url}")
    request = HTTPXRequest(**req_kwargs)
    builder = builder.request(request)

    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
