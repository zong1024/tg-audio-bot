"""配置管理 - 从环境变量或 .env 文件加载"""

import os
from pathlib import Path

# Telegram Bot Token（必须）
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# 音频下载目录（NAS 挂载路径）
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))

# 音频格式：flac | aac | best（best = 保留原始编码，不转码）
AUDIO_FORMAT = os.getenv("AUDIO_FORMAT", "flac")

# 允许使用 Bot 的 Telegram 用户 ID（逗号分隔），留空 = 所有人可用
ALLOWED_USERS: list[int] = [
    int(uid) for uid in os.getenv("ALLOWED_USERS", "").split(",") if uid.strip()
]

# B站 Cookies 文件路径（可选，部分高清内容需要登录态）
BILIBILI_COOKIES = os.getenv("BILIBILI_COOKIES", "")

# 文件大小上限（字节）：超过此值不发送到 Telegram，只存 NAS
# 使用 Local Bot API Server 可达 2GB
TG_FILE_LIMIT = int(os.getenv("TG_FILE_LIMIT", str(2 * 1024 * 1024 * 1024)))

# Telegram Local Bot API Server 地址（留空则使用官方 API）
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "")

# 代理配置（YouTube/Telegram 需要翻墙）
PROXY_HOST = os.getenv("PROXY_HOST", "")
PROXY_HTTP_PORT = os.getenv("PROXY_HTTP_PORT", "1081")
PROXY_SOCKS_PORT = os.getenv("PROXY_SOCKS_PORT", "1080")

# 构建代理 URL
if PROXY_HOST:
    HTTP_PROXY = f"http://{PROXY_HOST}:{PROXY_HTTP_PORT}"
    SOCKS_PROXY = f"socks5://{PROXY_HOST}:{PROXY_SOCKS_PORT}"
else:
    # 从环境变量读取（Docker 已设置的情况）
    HTTP_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY", "")
    SOCKS_PROXY = os.getenv("ALL_PROXY", "")

# yt-dlp 使用的代理（SOCKS5 优先，YouTube 效果更好）
YTDL_PROXY = SOCKS_PROXY or HTTP_PROXY

# 确保下载目录存在
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
