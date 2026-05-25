#!/bin/bash
# 매일 영어 (일기 + Opic) — 로컬 Django 서버 한 번에 실행
set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
VENV_DIR=".venv"
PORT=${PORT:-8000}

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

# 포트 사용 중이면 자동으로 정리 시도
if lsof -ti tcp:$PORT > /dev/null 2>&1; then
  echo "⚠️  포트 $PORT 가 이미 사용 중이에요. 기존 프로세스 정리..."
  lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null || true
  sleep 1
fi

echo ""
echo "🚀 서버 시작 → http://localhost:$PORT"
echo "   종료하려면 Ctrl+C"
echo ""

# 자동 브라우저 오픈 (백그라운드, 1초 대기)
(sleep 1.5 && (command -v open >/dev/null && open "http://localhost:$PORT" || \
               command -v xdg-open >/dev/null && xdg-open "http://localhost:$PORT")) &

exec python manage.py runserver "0.0.0.0:$PORT"
