FROM python:3.12-slim

ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NODE_VERSION=20 \
    TZ=Asia/Seoul

# System deps:
#  - Node.js 20 + Claude Code CLI (for AI feedback subprocess)
#  - cron (daily 23:00 KST notify)
#  - tzdata (so cron fires at the right wall-clock time)
#  - cloudflared (Cloudflare Quick Tunnel for external access)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg cron tzdata \
    && ln -sf /usr/share/zoneinfo/Asia/Seoul /etc/localtime \
    && echo "Asia/Seoul" > /etc/timezone \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && curl -fL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${TARGETARCH}" \
         -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# System crontab (runs as root, includes user field)
COPY docker/crontab /etc/cron.d/opic-daily
RUN chmod 0644 /etc/cron.d/opic-daily \
    && touch /var/log/cron.log

RUN mkdir -p /app/data /root/.claude

EXPOSE 8000

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
