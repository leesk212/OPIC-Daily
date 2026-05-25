#!/bin/bash
# Cloudflare Quick Tunnel (무료, 계정 불필요) 실행
# URL을 data/tunnel_url.txt에 자동 저장 → notify가 그 URL을 알림에 사용
set -e
cd "$(dirname "$0")"

PORT=${PORT:-8000}
URL_FILE="data/tunnel_url.txt"

if ! command -v cloudflared &> /dev/null; then
  echo "❌ cloudflared 미설치"
  echo "   macOS: brew install cloudflared"
  echo "   기타:  https://pkg.cloudflare.com/"
  exit 1
fi

if ! lsof -ti tcp:$PORT > /dev/null 2>&1; then
  echo "⚠️  로컬 서버가 $PORT 포트에 안 떠 있어요. 먼저 ./run.sh 실행."
  exit 1
fi

mkdir -p data
LOG_FILE="data/tunnel.log"
rm -f "$URL_FILE"

echo "🌐 Cloudflare Quick Tunnel 시작..."
echo "   (계정 가입 불필요 · 무료)"
echo ""

# Run cloudflared in background, capture output to log
cloudflared tunnel --url "http://localhost:$PORT" > "$LOG_FILE" 2>&1 &
TUNNEL_PID=$!

# Wait for URL to appear in log (up to 20s)
echo "⏳ URL 발급 대기..."
for i in $(seq 1 40); do
  if grep -qE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_FILE" 2>/dev/null; then
    URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_FILE" | head -1)
    echo "$URL" > "$URL_FILE"
    echo ""
    echo "✅ Tunnel URL: $URL"
    echo "   → $URL_FILE 에 저장됨 (notify가 자동 사용)"
    echo ""
    echo "🔵 외부 접속: $URL"
    echo ""
    echo "   Ctrl+C로 종료 (URL은 파일에서 자동 제거됩니다)"
    break
  fi
  sleep 0.5
done

if [ ! -f "$URL_FILE" ]; then
  echo "❌ 20초 안에 URL 못 받았어요. 로그 확인: $LOG_FILE"
  kill $TUNNEL_PID 2>/dev/null
  exit 1
fi

# Cleanup on exit
cleanup() {
  echo ""
  echo "🛑 종료 중..."
  rm -f "$URL_FILE"
  kill $TUNNEL_PID 2>/dev/null || true
  wait $TUNNEL_PID 2>/dev/null
  echo "✅ Tunnel 종료, URL 파일 정리됨"
}
trap cleanup EXIT INT TERM

# Wait for tunnel process
wait $TUNNEL_PID
