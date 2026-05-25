#!/bin/sh
set -e

mkdir -p /app/data
python manage.py migrate --noinput

# ===== 1. Cron daemon (daily 23:00 KST notify) =====
echo "🕐 Starting cron daemon (TZ=$TZ)..."
cron

# ===== 2. Cloudflare Quick Tunnel (background) =====
# URL goes into /app/data/tunnel_url.txt which views.py serves to the settings UI
# and notify.py uses for the email/push link.
echo "🌐 Starting Cloudflare Quick Tunnel..."
rm -f /app/data/tunnel_url.txt
cloudflared tunnel --url http://localhost:8000 --no-autoupdate \
    > /app/data/tunnel.log 2>&1 &
TUNNEL_PID=$!

# Poll the log for up to ~20s for the trycloudflare.com URL
i=0
while [ $i -lt 40 ]; do
  if [ -f /app/data/tunnel.log ] && \
     grep -qE 'https://[a-z0-9-]+\.trycloudflare\.com' /app/data/tunnel.log 2>/dev/null; then
    URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /app/data/tunnel.log | head -1)
    echo "$URL" > /app/data/tunnel_url.txt
    echo "✅ Tunnel URL: $URL"
    break
  fi
  i=$((i + 1))
  sleep 0.5
done

if [ ! -f /app/data/tunnel_url.txt ]; then
  echo "⚠️  Tunnel URL not received in 20s. App still works on http://localhost:8000."
  echo "    Check /app/data/tunnel.log for errors. Tunnel may also come up later."
fi

# ===== 3. Claude Code CLI auth check =====
if [ ! -f /root/.claude/.credentials.json ] && [ -z "$ANTHROPIC_API_KEY" ]; then
  cat <<'EOF'
================================================================
ℹ️  Claude Code CLI 인증이 안 되어 있어요.

다음 명령으로 로그인하세요 (브라우저 URL이 출력됩니다):
   docker exec -it opic-daily claude login

인증 정보는 마운트된 볼륨에 보존되므로 1번만 하면 됩니다.
================================================================
EOF
fi

# ===== 4. Run Django as PID 1 =====
# Cron + cloudflared continue running as children of the original shell;
# when the container stops, both are reaped along with Django.
echo "🚀 Starting Django on 0.0.0.0:8000..."
exec "$@"
