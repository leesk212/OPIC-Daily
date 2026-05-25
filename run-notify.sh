#!/bin/bash
# cron/launchd가 호출하는 알림 실행 래퍼
# .env를 읽고 venv 활성화해서 notify 명령 실행
set -e
cd "$(dirname "$0")"

# venv 활성화
if [ -d ".venv" ]; then
  source .venv/bin/activate
fi

# .env는 settings.py가 알아서 로드 (별도 source 불필요)

# 로그를 data/notify.log에 추가
LOG_FILE="data/notify.log"
mkdir -p data

{
  echo ""
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
  python manage.py notify "$@"
} >> "$LOG_FILE" 2>&1

# 마지막 결과를 stdout에도 (수동 실행 시 보이게)
tail -n 20 "$LOG_FILE"
