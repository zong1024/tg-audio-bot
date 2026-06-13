FROM python:3.12-slim

# 安装 ffmpeg + Node.js（yt-dlp YouTube 需要 JS 运行时）
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 下载目录（挂载 NAS）
VOLUME /downloads

CMD ["python", "bot.py"]
