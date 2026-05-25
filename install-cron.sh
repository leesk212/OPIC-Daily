#!/bin/bash
# 호스트 macOS/Linux의 user crontab에 OPIC-Daily 알림 cron 라인 등록.
# ⚙️ 설정에 저장된 스케줄 (start_hour / interval / count)을 그대로 사용.
#
# 사용:
#   ./install-cron.sh          # 등록 (기존 OPIC-Daily 라인은 교체)
#   ./install-cron.sh --remove # 제거
#   ./install-cron.sh --show   # 현재 등록될 라인 미리보기
set -e
cd "$(dirname "$0")"

PROJECT_DIR=$(pwd)
VENV_PY="$PROJECT_DIR/.venv/bin/python"
TAG="# OPIC-Daily notify"

if [ ! -x "$VENV_PY" ]; then
  echo "❌ .venv가 없어요. 먼저 ./run.sh 한 번 실행해서 venv 만드세요."
  exit 1
fi

# write_crontab --print으로 시스템 crontab 형식 받아오고,
# 호스트 user crontab 형식으로 변환 (5필드 + 명령. user 필드 제거. /app/data → 실제 경로)
generate_lines() {
  "$VENV_PY" manage.py write_crontab --print \
    | grep -E '^[0-9 ]+\* \* \* root cd /app' \
    | sed -E "s|root cd /app|cd $PROJECT_DIR|g; s|/usr/local/bin/python|$VENV_PY|g; s|/app/data|$PROJECT_DIR/data|g" \
    | sed -E "s|$| $TAG|"
}

case "${1:-install}" in
  --show)
    echo "===등록될 cron 라인 (현재 ⚙️ 설정 기준)==="
    generate_lines
    ;;

  --remove)
    echo "🧹 기존 OPIC-Daily cron 라인 제거..."
    (crontab -l 2>/dev/null | grep -v "$TAG" | grep -v "^$") | crontab -
    echo "✅ 제거 완료"
    crontab -l 2>/dev/null || echo "(crontab 비어있음)"
    ;;

  install|*)
    LINES=$(generate_lines)
    if [ -z "$LINES" ]; then
      echo "⚠️  알림 비활성화 상태 (notify_count=0). 등록 안 함."
      echo "    ⚙️ 설정에서 횟수 1+로 바꾼 뒤 다시 실행."
      exit 0
    fi
    echo "📋 등록할 라인:"
    echo "$LINES"
    echo ""
    # 기존 OPIC-Daily 라인 제거 후 새 라인 추가
    CURRENT=$(crontab -l 2>/dev/null | grep -v "$TAG" || true)
    (echo "$CURRENT"; echo "$LINES") | grep -v "^$" | crontab -
    echo "✅ crontab 등록 완료. 확인:"
    crontab -l | grep "$TAG"
    echo ""
    echo "ℹ️  macOS는 잠자기 중 cron이 안 돕니다."
    echo "    노트북이면 caffeinate 또는 launchd의 WakeFromSleep 필요."
  ;;
esac
