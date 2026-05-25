#!/bin/bash
# Django runserver 종료. 기본은 서버만 끄고 tunnel은 살림.
# 옵션:
#   ./stop.sh              # 서버만 종료 (tunnel detached로 유지 → 다음 ./run.sh가 reuse)
#   ./stop.sh --all        # tunnel까지 같이 종료 + tunnel_url.txt 삭제
PORT=${PORT:-8000}
cd "$(dirname "$0")"

KILL_TUNNEL=0
[ "$1" = "--all" ] && KILL_TUNNEL=1

# 1. Django 종료
PIDS=$(lsof -ti tcp:$PORT 2>/dev/null || true)
if [ -z "$PIDS" ]; then
  echo "🛑 $PORT 포트에 실행 중인 프로세스 없음"
else
  echo "🛑 Django 종료 (SIGKILL): $PIDS"
  kill -9 $PIDS 2>/dev/null || true
  sleep 1
fi
REMAINING=$(lsof -ti tcp:$PORT 2>/dev/null || true)
if [ -n "$REMAINING" ]; then
  echo "⚠️  남은 프로세스 강제 종료: $REMAINING"
  kill -9 $REMAINING 2>/dev/null || true
fi

# 2. Tunnel — --all 일 때만 같이 종료
if [ "$KILL_TUNNEL" = "1" ]; then
  TPIDS=$(pgrep -f "cloudflared tunnel" || true)
  if [ -n "$TPIDS" ]; then
    echo "🌐 Cloudflare Tunnel 종료: $TPIDS"
    kill $TPIDS 2>/dev/null || true
  fi
  rm -f data/tunnel_url.txt
  echo "   tunnel_url.txt 삭제"
else
  if pgrep -f "cloudflared tunnel" > /dev/null 2>&1; then
    URL=$(cat data/tunnel_url.txt 2>/dev/null)
    echo "🌐 Tunnel은 그대로 유지: $URL"
    echo "   완전 종료: ./stop.sh --all"
  fi
fi

echo "✅ 정리 완료"
