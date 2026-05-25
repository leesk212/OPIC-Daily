#!/bin/bash
# Django runserver 프로세스 종료 (강제)
PORT=${PORT:-8000}
PIDS=$(lsof -ti tcp:$PORT 2>/dev/null || true)
if [ -z "$PIDS" ]; then
  echo "🛑 $PORT 포트에 실행 중인 프로세스가 없어요."
else
  echo "🛑 종료 (SIGKILL): $PIDS"
  kill -9 $PIDS 2>/dev/null || true
  sleep 1
fi

# 한 번 더 확인 (자식 프로세스 등)
REMAINING=$(lsof -ti tcp:$PORT 2>/dev/null || true)
if [ -n "$REMAINING" ]; then
  echo "⚠️  남은 프로세스 강제 종료: $REMAINING"
  kill -9 $REMAINING 2>/dev/null || true
fi

echo "✅ 정리 완료"
