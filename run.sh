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

# 기존 cloudflared가 살아있고 URL 파일이 유효하면 그대로 재사용 (URL 안 바뀜).
# 새로 띄울 땐 nohup + disown으로 셸 종료와 분리 → 다음 ./run.sh가 reuse 가능.
detect_existing_tunnel() {
  [ "$NO_TUNNEL" = "1" ] && return 1
  [ -s "$URL_FILE" ] || return 1
  command -v cloudflared &> /dev/null || return 1
  pgrep -f "cloudflared tunnel" > /dev/null 2>&1 || return 1
  local existing_url
  existing_url=$(head -1 "$URL_FILE" | tr -d '[:space:]')
  [ -n "$existing_url" ] || return 1
  local pid
  pid=$(pgrep -f 'cloudflared tunnel' | head -1)
  echo "   ♻️  기존 Tunnel 재사용: $existing_url (PID $pid)"
  return 0
}

# ===== Cloudflare Quick Tunnel: reuse or start =====
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

  if detect_existing_tunnel; then
    return
  fi

  echo "🌐 Cloudflare Quick Tunnel 새로 시작..."
  rm -f "$URL_FILE"
  # nohup + disown으로 detach → 이 셸이 종료돼도 tunnel은 계속 살아있음
  nohup cloudflared tunnel --url "http://localhost:$PORT" --no-autoupdate \
    > "$TUNNEL_LOG" 2>&1 &
  local tpid=$!
  disown "$tpid" 2>/dev/null || true

  # 최대 20초 URL 대기
  for i in $(seq 1 40); do
    if grep -qE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null; then
      URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)
      echo "$URL" > "$URL_FILE"
      echo "   ✅ Tunnel URL: $URL (PID $tpid, detached)"
      echo "      → 설정 모달과 알림에 자동 반영됨"
      echo "      → ./run.sh 재실행 시 같은 URL 재사용 (명시적 종료: pkill -f 'cloudflared tunnel')"
      return
    fi
    sleep 0.5
  done
  echo "   ⚠️  20초 안에 URL 못 받음 ($TUNNEL_LOG 확인). 서버는 그대로 동작."
}

# Ctrl+C 시 tunnel은 그대로 둠 (detached) → 다음 ./run.sh에서 reuse
cleanup() {
  echo ""
  if [ -s "$URL_FILE" ] && pgrep -f "cloudflared tunnel" > /dev/null 2>&1; then
    echo "🛑 Django 서버만 종료. Tunnel은 백그라운드 유지: $(cat "$URL_FILE")"
    echo "   tunnel도 끄려면: pkill -f 'cloudflared tunnel'"
  else
    echo "🛑 종료 중..."
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
