#!/bin/bash
# 매일 영어 (일기 + Opic) — 로컬 Django 서버 + Cloudflare Quick Tunnel 한 번에 실행
set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
VENV_DIR=".venv"
PORT=${PORT:-8000}
NO_TUNNEL=${NO_TUNNEL:-0}   # NO_TUNNEL=1 ./run.sh 로 끄기

echo "🔍 Claude Code CLI 확인..."
if ! command -v claude &> /dev/null; then
  echo "⚠️  'claude' 명령어가 PATH에 없어요."
  echo "   설치: https://docs.claude.com/en/docs/claude-code/quickstart"
  echo "   설치 후에 './run.sh' 다시 실행해주세요."
  exit 1
fi
echo "   ✅ Claude Code 발견: $(which claude)"

if [ ! -d "$VENV_DIR" ]; then
  echo "📦 가상환경(venv) 만들기..."
  $PY -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

echo "📥 의존성 설치..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "🗄  마이그레이션 파일 생성 (필요시)..."
python manage.py makemigrations api --noinput

echo "🗄  DB 마이그레이션..."
python manage.py migrate --noinput

# Expression 시드 데이터를 DB로 적재 (idempotent upsert)
if [ -f "data/expressions.json" ]; then
  python manage.py seed_expressions 2>&1 | tail -1 || true
fi

mkdir -p data

# ⚙️ 설정에 저장된 알림 스케줄을 호스트 user crontab에 자동 등록.
# macOS crontab이 권한 prompt 등으로 가끔 멈춰서 서버 시작이 막히는 일이
# 있어 백그라운드로 fire-and-forget. 결과는 data/install-cron.log에 남음.
if command -v crontab &> /dev/null; then
  ( ./install-cron.sh --quiet > data/install-cron.log 2>&1 || true ) &
  echo "🕐 cron 등록 백그라운드 진행 중 (결과: tail data/install-cron.log)"
fi

# 포트 사용 중이면 자동으로 정리 시도
if lsof -ti tcp:$PORT > /dev/null 2>&1; then
  echo "⚠️  포트 $PORT 가 이미 사용 중이에요. 기존 프로세스 정리..."
  lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null || true
  sleep 1
fi

mkdir -p data
URL_FILE="data/tunnel_url.txt"
TUNNEL_LOG="data/tunnel.log"
TUNNEL_PID=""

# ===== Cloudflare Quick Tunnel 자동 시작 (있을 때만) =====
start_tunnel() {
  if [ "$NO_TUNNEL" = "1" ]; then
    echo "🌐 NO_TUNNEL=1 — Cloudflare Tunnel 스킵"
    return
  fi
  if ! command -v cloudflared &> /dev/null; then
    echo "ℹ️  cloudflared 미설치 — 외부 접속 URL 생성 스킵"
    echo "   설치하면 외부 URL이 자동 생성됩니다 (macOS: brew install cloudflared)"
    return
  fi

  echo "🌐 Cloudflare Quick Tunnel 시작..."
  rm -f "$URL_FILE"
  cloudflared tunnel --url "http://localhost:$PORT" --no-autoupdate \
    > "$TUNNEL_LOG" 2>&1 &
  TUNNEL_PID=$!

  # 최대 20초 URL 대기
  for i in $(seq 1 40); do
    if grep -qE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null; then
      URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)
      echo "$URL" > "$URL_FILE"
      echo "   ✅ Tunnel URL: $URL"
      echo "      → 설정 모달과 알림에 자동 반영됨"
      return
    fi
    sleep 0.5
  done
  echo "   ⚠️  20초 안에 URL 못 받음 ($TUNNEL_LOG 확인). 서버는 그대로 동작."
}

# 종료 시 청소
cleanup() {
  echo ""
  echo "🛑 종료 중..."
  rm -f "$URL_FILE"
  if [ -n "$TUNNEL_PID" ]; then
    kill "$TUNNEL_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

start_tunnel

echo ""
echo "🚀 서버 시작 → http://localhost:$PORT"
echo "   종료하려면 Ctrl+C"
echo ""

# 자동 브라우저 오픈 (백그라운드, 1.5초 대기)
(sleep 1.5 && (command -v open >/dev/null && open "http://localhost:$PORT" || \
               command -v xdg-open >/dev/null && xdg-open "http://localhost:$PORT")) &

# Django를 foreground로 (Ctrl+C로 종료하면 위 cleanup이 tunnel도 정리)
python manage.py runserver "0.0.0.0:$PORT"
