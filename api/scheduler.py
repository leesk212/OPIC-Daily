"""
내부 알림 스케줄러 — Django 프로세스 안에서 매 분 깨어나 settings의
notify_hours에 도달하면 `manage.py notify`를 호출.

OS-level cron이 안 도는 환경(권한/sleep)에서도 동작하게 만든 fallback.
Django 프로세스가 떠 있는 동안만 동작 (run.sh가 백그라운드로 유지).

중복 발사 방지:
  data/last_notify_fire.txt 에 "YYYY-MM-DD HH:MM" 형태로 마지막 발사 키 기록.
  매 분 wake-up에서 현재 "YYYY-MM-DD HH:MM"와 비교, 일치하면 스킵.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_started = False
_lock = threading.Lock()

POLL_SECONDS = 60  # 매 분 깨어남
_LAST_FIRE_FILENAME = 'last_notify_fire.txt'


def _last_fire_path() -> Path:
    from django.conf import settings as ds
    return Path(ds.BASE_DIR) / 'data' / _LAST_FIRE_FILENAME


def _read_last_fire_key() -> str:
    try:
        p = _last_fire_path()
        if p.is_file():
            return p.read_text(encoding='utf-8').strip()
    except Exception:
        pass
    return ''


def _write_last_fire_key(key: str) -> None:
    try:
        p = _last_fire_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(key, encoding='utf-8')
    except Exception as e:
        logger.warning(f'last_fire 기록 실패: {e}')


def _current_key(now: datetime) -> str:
    return f'{now.strftime("%Y-%m-%d")} {now.hour:02d}:{now.minute:02d}'


def _tick() -> None:
    """한 번의 wake-up에서 발사 조건 체크하고 필요하면 manage.py notify 호출."""
    from django.core.management import call_command
    from api.views import compute_notify_slots, get_settings

    s = get_settings()
    slots = compute_notify_slots(s)  # list of (hour, minute)
    if not slots:
        return

    now = datetime.now()
    if (now.hour, now.minute) not in slots:
        return

    cur_key = _current_key(now)
    last_key = _read_last_fire_key()
    if cur_key == last_key:
        return

    logger.info(f'[scheduler] firing notify (key={cur_key}, slots={slots})')
    try:
        call_command('notify')
    except Exception as e:
        logger.exception(f'[scheduler] notify 호출 실패: {e}')
    finally:
        # 실패해도 키 기록 — 같은 시각에 무한 재시도 방지. 다음 hour에 다시 시도.
        _write_last_fire_key(cur_key)


def _loop() -> None:
    """매 POLL_SECONDS마다 _tick. 예외가 나도 스레드는 살아있게."""
    logger.info('[scheduler] notify scheduler thread started')
    while True:
        try:
            _tick()
        except Exception as e:
            logger.exception(f'[scheduler] tick error: {e}')
        time.sleep(POLL_SECONDS)


def start() -> None:
    """프로세스당 한 번만 background daemon 스레드를 띄움.

    Django runserver의 autoreloader는 부모/자식 두 프로세스를 띄우는데
    둘 다 AppConfig.ready()를 호출한다. 양쪽에서 scheduler thread가 시작되면
    매 슬롯마다 알림이 두 번 발사된다. RUN_MAIN 환경변수로 자식만 통과시킨다:
      - reloader 부모: RUN_MAIN 미설정 → 스킵
      - reloader 자식: RUN_MAIN='true' → 시작
      - runserver --noreload / gunicorn 등: RUN_MAIN 미설정이지만 자식 프로세스
        자체가 없으므로 argv를 보고 runserver+reloader인지 판별
    """
    global _thread, _started
    import sys
    with _lock:
        if _started:
            return
        argv = sys.argv
        running_runserver = any('runserver' in a for a in argv)
        noreload = '--noreload' in argv
        # runserver + reloader ON + 부모 프로세스(RUN_MAIN!=true)인 경우만 스킵.
        # 그 외(자식, --noreload, manage.py 일반 커맨드, gunicorn 등)는 전부 시작.
        if running_runserver and not noreload and os.environ.get('RUN_MAIN') != 'true':
            logger.info('[scheduler] skipping start in runserver reloader parent')
            return
        _thread = threading.Thread(target=_loop, name='notify-scheduler', daemon=True)
        _thread.start()
        _started = True
        logger.info('[scheduler] start() invoked — thread spawned')
