#!/bin/sh
set -e

# Ensure data dir and run migrations on every start (idempotent)
mkdir -p /app/data
python manage.py migrate --noinput

# Friendly notice if Claude Code CLI isn't authenticated yet
if [ ! -f /root/.claude/.credentials.json ] && [ -z "$ANTHROPIC_API_KEY" ]; then
  cat <<'EOF'
================================================================
ℹ️  Claude Code CLI 인증이 안 되어 있어요.

다음 명령으로 로그인하세요 (브라우저에서 인증):
   docker exec -it opic-daily claude login

또는 환경변수로 API key 주입:
   docker run -e ANTHROPIC_API_KEY=sk-... ...

인증 정보는 마운트된 볼륨에 보존되므로 1번만 하면 됩니다.
================================================================
EOF
fi

exec "$@"
