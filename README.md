# 🎵 Telegram 音频下载 Bot

发送 B站/YouTube 视频链接到 Telegram Bot，自动提取高清无损音频保存到 NAS。

## 特性

- ✅ 支持 B站（bilibili.com、b23.tv 短链）
- ✅ 支持 YouTube（youtube.com、youtu.be）
- ✅ FLAC 无损音频输出（可配置）
- ✅ 下载进度实时显示
- ✅ 文件同时存 NAS + 发送到 Telegram（< 50MB）
- ✅ Docker 一键部署
- ✅ 可选用户白名单
- ✅ 可选 B站 Cookies（高清内容）

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
# 1. 复制配置文件
cp .env.example .env

# 2. 编辑 .env，填入 BOT_TOKEN 和 NAS 路径
vim .env

# 3. 启动
docker compose up -d

# 查看日志
docker compose logs -f
```

### 方式二：直接运行

```bash
# 安装依赖
pip install -r requirements.txt
# 确保系统已安装 ffmpeg

# 设置环境变量
export BOT_TOKEN='your_token_here'
export DOWNLOAD_DIR='/mnt/nas/music'
export AUDIO_FORMAT='flac'

# 运行
python bot.py
```

## 配置说明

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `BOT_TOKEN` | （必填） | Telegram Bot Token |
| `DOWNLOAD_DIR` | `/downloads/music` | 音频保存目录 |
| `AUDIO_FORMAT` | `flac` | 输出格式：`flac` / `aac` / `best` |
| `ALLOWED_USERS` | （空=所有人） | 限定用户 ID，逗号分隔 |
| `BILIBILI_COOKIES` | （空） | B站 Cookies 文件路径 |
| `TG_FILE_LIMIT` | `52428800` | 发送到 Telegram 的文件大小上限（字节） |

## NAS 挂载示例

### 群晖 Synology
```yaml
volumes:
  - /volume1/music:/downloads
```

### NFS 挂载
```bash
# 先在宿主机挂载 NFS
sudo mount -t nfs nas_ip:/volume1/music /mnt/nas/music
```

```yaml
volumes:
  - /mnt/nas/music:/downloads
```

### SMB/CIFS 挂载
```bash
# 先在宿主机挂载 SMB
sudo mount -t cifs //nas_ip/music /mnt/smb/music -o username=xxx,password=xxx
```

## B站 Cookies 获取

部分 B站高清内容需要登录态。获取 Cookies：

1. 安装浏览器插件 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
2. 登录 B站，导出 Cookies 为 `cookies.txt`
3. 将文件放到项目目录，配置 `BILIBILI_COOKIES=./cookies.txt`

## 项目结构

```
tg-audio-bot/
├── bot.py              # Telegram Bot 主逻辑
├── downloader.py       # yt-dlp 下载封装
├── config.py           # 配置管理
├── requirements.txt    # Python 依赖
├── Dockerfile          # Docker 镜像
├── docker-compose.yml  # Docker Compose 部署
├── .env.example        # 环境变量模板
└── README.md           # 本文件
```
