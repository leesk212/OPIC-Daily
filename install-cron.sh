#!/bin/bash
# 호스트 macOS/Linux의 user crontab에 OPIC-Daily 알림 cron 라인 등록.
# ⚙️ 설정에 저장된 스케줄 (start_hour / interval / count)을 그대로 사용.
#
# 사용:
#   ./install-cron.sh           # 등록 (기존 OPIC-Daily 라인은 교체)
#   ./install-cron.sh --quiet   # 등록하고 한 줄 요약만 출력 (run.sh가 사용)
#   ./install-cron.sh --remove  # 제거
#   ./install-cron.sh --show    # 등록될 라인 미리보기 (등록 안 함)
set -e
cd "$(dirname "$0")"

PROJECT_DIR=$(pwd)
VENV_PY="$PROJECT_DIR/.venv/bin/python"
TAG="# OPIC-Daily notify"

if [ ! -x "$VENV_PY" ]; then
  [ "$1" = "--quiet" ] || echo "❌ .venv가 없어요. 먼저 ./run.sh 한 번 실행해서 venv 만드세요."
  exit 1
fi

# write_crontab --print으로 시스템 crontab 라인 받아 호스트 user 형식으로 변환
# (5필드 + 명령, user 필드 제거, /app/data → 실제 경로)
generate_lines() {
  "$VENV_PY" manage.py write_crontab --print \
    | grep -E '^[0-9 ]+\* \* \* root cd /app' \
    | sed -E "s|root cd /app|cd $PROJECT_DIR|g; s|/usr/local/bin/python|$VENV_PY|g; s|/app/data|$PROJECT_DIR/data|g" \
    | sed -E "s|$| $TAG|"
}

# 실제 등록 로직 (install + quiet가 공유)
do_install() {
  local lines=$(generate_lines)
  if [ -z "$lines" ]; then
    echo "(no-op) 알림 비활성화(count=0). cron 등록 건너뜀."
    return 0
  fi
  local current=$(crontab -l 2>/dev/null | grep -v "$TAG" || true)
  (echo "$current"; echo "$lines") | grep -v "^$" | crontab -
  # 등록된 시간 요약
  local times=$(echo "$lines" | awk '{printf "%02d:%02d ", $2, $1}')
  echo "🕐 cron 등록: $(echo "$lines" | wc -l | tr -d ' ')개 시점 ($times)"
}

case "${1:-install}" in
  --quiet)
    do_install 2>/dev/null || true
    ;;

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
    CURRENT=$(crontab -l 2>/dev/null | grep -v "$TAG" || true)
    (echo "$CURRENT"; echo "$LINES") | grep -v "^$" | crontab -
    echo "✅ crontab 등록 완료. 확인:"
    crontab -l | grep "$TAG"
    echo ""
    echo "ℹ️  macOS는 잠자기 중 cron이 안 돕니다."
    echo "    노트북이면 caffeinate 또는 launchd의 WakeFromSleep 필요."
  ;;
esac
