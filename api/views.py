"""API views for entries and AI feedback."""
from __future__ import annotations  # Python 3.9 호환 — `str | None` 등 PEP 604 어노테이션 lazy 평가

import json
import logging
import re

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .ai_client import call_claude, ClaudeCodeError, diagnose as claude_diagnose
from .models import Entry, Expression, Preference, User
from .prompts import build_diary_prompt, build_opic_prompt

import os
ADMIN_PASSWORD = os.environ.get('OPIC_ADMIN_PASSWORD', 'leek1122*')


def _current_user(request):
    """Resolve the requesting user from X-User-Id header. Returns User or None."""
    uid = request.headers.get('X-User-Id', '').strip()
    if not uid:
        return None
    try:
        return User.objects.get(username=uid)
    except User.DoesNotExist:
        return None


def _require_admin(request):
    """Check X-Admin-Password header against the configured admin password."""
    pw = request.headers.get('X-Admin-Password', '')
    return bool(pw) and pw == ADMIN_PASSWORD

# Default settings (used as fallback; UI shows these prefilled)
DEFAULT_SETTINGS = {
    'site_url': 'http://localhost:8000',
    'slack_webhook_url': '',           # admin이 admin 페이지에서 설정. https://hooks.slack.com/...
    'slack_mention_user_id': '',       # 본인 Slack member ID (예: U01ABCDEF) — 메시지 앞에 <@ID> 자동 부착해 모바일 푸시 강제
    # 알림 스케줄 (24h, KST 기준) — 분 단위
    'notify_start_hour': 23,           # 0-23
    'notify_start_minute': 0,          # 0-59
    'notify_interval_minutes': 60,     # 1-1440 (즉 1분 ~ 24시간)
    'notify_count': 1,                 # 0-48 (0이면 비활성화)
    # 누구의 일기/Opic 완료 여부를 볼지. 비우면 모든 user 합산.
    'notify_user': '',
}

# Keys that only admin can write/read in full. Non-admin GETs see masked value.
ADMIN_ONLY_SETTING_KEYS = {'slack_webhook_url', 'slack_mention_user_id'}

SETTINGS_KEY = 'app_settings'

# Whitelist of keys the API surfaces. Orphaned DB keys from earlier
# (email/SMTP era) are intentionally dropped so they never leak to clients.
ALLOWED_SETTING_KEYS = set(DEFAULT_SETTINGS.keys())  # global keys only; per-user keys live on User.preferences

# Keys that are stored per-user on User.preferences, NOT in the global Preference table.
PER_USER_SETTING_KEYS = {'opic_selected_topics'}


def get_settings() -> dict:
    """Load settings: DB → env → defaults, filtered to ALLOWED_SETTING_KEYS."""
    try:
        pref = Preference.objects.get(key=SETTINGS_KEY)
        stored = pref.value if isinstance(pref.value, dict) else {}
    except Preference.DoesNotExist:
        stored = {}

    merged = dict(DEFAULT_SETTINGS)
    import os as _os
    env_map = {
        'site_url': 'SITE_URL',
        'slack_webhook_url': 'SLACK_WEBHOOK_URL',
        'slack_mention_user_id': 'SLACK_MENTION_USER_ID',
    }
    for k, env_k in env_map.items():
        v = _os.environ.get(env_k)
        if v:
            merged[k] = v
    # DB overrides — but only for keys we still support
    merged.update({
        k: v for k, v in stored.items()
        if k in ALLOWED_SETTING_KEYS and v not in (None, '')
    })
    for k in ('notify_start_hour', 'notify_start_minute', 'notify_interval_minutes', 'notify_count'):
        try:
            merged[k] = int(merged.get(k, DEFAULT_SETTINGS[k]))
        except (TypeError, ValueError):
            merged[k] = DEFAULT_SETTINGS[k]
    # backwards-compat: 이전 버전의 notify_interval_hours가 DB에 남아있고
    # 새 키를 한 번도 저장하지 않았으면 *60으로 변환해서 보여줌.
    if 'notify_interval_hours' in stored and stored.get('notify_interval_minutes') in (None, ''):
        try:
            merged['notify_interval_minutes'] = max(1, int(stored['notify_interval_hours']) * 60)
        except (TypeError, ValueError):
            pass
    return {k: v for k, v in merged.items() if k in ALLOWED_SETTING_KEYS}


def save_settings(new: dict) -> dict:
    pref, _ = Preference.objects.get_or_create(key=SETTINGS_KEY, defaults={'value': {}})
    cur = pref.value if isinstance(pref.value, dict) else {}
    # Don't filter empty lists ([] is valid for opic_selected_topics meaning "use all")
    cur.update({k: v for k, v in new.items() if v not in (None, '')})
    pref.value = cur
    pref.save()
    return get_settings()


def compute_notify_slots(s: dict) -> list[tuple[int, int]]:
    """Given settings dict, return list of (hour, minute) tuples where notify fires.

    Slots are generated from notify_start_hour/notify_start_minute by adding
    notify_interval_minutes notify_count times. Slots that would fall past
    23:59 of the same day are dropped.
    """
    start_h = max(0, min(23, int(s.get('notify_start_hour', 23))))
    start_m = max(0, min(59, int(s.get('notify_start_minute', 0))))
    interval_min = max(1, min(1440, int(s.get('notify_interval_minutes', 60))))
    count = max(0, min(48, int(s.get('notify_count', 1))))
    slots: list[tuple[int, int]] = []
    cur = start_h * 60 + start_m
    for _ in range(count):
        if cur > 23 * 60 + 59:
            break
        slots.append((cur // 60, cur % 60))
        cur += interval_min
    return slots


def compute_notify_hours(s: dict) -> list[int]:
    """Legacy alias — returns just the unique hours from compute_notify_slots.
    Kept for any external caller; new code should use compute_notify_slots."""
    seen: set[int] = set()
    out: list[int] = []
    for h, _ in compute_notify_slots(s):
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


CRON_TAG = '# OPIC-Daily notify'


def _build_user_cron_lines(hours: list[int]) -> list[str]:
    """현재 venv python + 프로젝트 경로 기준 user-crontab 형식 5필드 라인 목록."""
    from django.conf import settings as ds
    import sys
    py = sys.executable  # 현재 실행 중인 venv python
    project = str(ds.BASE_DIR)
    log = f'{project}/data/notify.log'
    return [
        f'0 {h} * * * cd {project} && {py} manage.py notify >> {log} 2>&1 {CRON_TAG}'
        for h in hours
    ]


def _install_user_crontab(hours: list[int]) -> tuple[bool, str]:
    """호스트 user crontab을 직접 갱신 — subprocess만 사용 (Django 재시작 없음).
    Returns (success, summary). silent on missing `crontab` cmd."""
    import shutil, subprocess
    if not shutil.which('crontab'):
        return False, 'crontab 명령 없음'
    try:
        cur = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=5)
        existing = cur.stdout if cur.returncode == 0 else ''
        kept = [ln for ln in existing.splitlines() if CRON_TAG not in ln and ln.strip()]
        new_lines = _build_user_cron_lines(hours)
        merged = '\n'.join(kept + new_lines) + ('\n' if (kept or new_lines) else '')
        subprocess.run(['crontab', '-'], input=merged, text=True, timeout=5, check=True)
        if not hours:
            return True, '알림 비활성화 (cron 라인 0개)'
        return True, f'{len(hours)}개 시점: {", ".join(f"{h:02d}:00" for h in hours)}'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _regenerate_crontab() -> None:
    """No-op since 2026-05-27 — host cron이 macOS에서 종종 동작하지 않아
    `api/scheduler.py`의 내부 타이머 스레드가 알림 발사를 대신한다.
    스케줄러는 매 분 settings를 재로드하므로 schedule 변경은 즉시 반영된다.
    함수 시그니처는 호환을 위해 유지."""
    return


logger = logging.getLogger(__name__)


def index(request):
    resp = render(request, 'index.html')
    # 메인 페이지는 캐시 금지 — 사용자가 git pull 후 매번 새 HTML 보도록
    resp['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp['Pragma'] = 'no-cache'
    return resp


def health(request):
    return JsonResponse({'status': 'ok'})


# ============ Shared helpers for enriched notifications ============

# 알림 본문에 한 줄씩 넣는 OPIc 한 줄 꿀팁 (AL 지향 핵심 코칭).
# 프론트엔드 '오늘 연습할 팁'(OPIC_TIPS)의 title/sub를 한 줄로 압축해 함께 반영.
OPIC_NOTIFY_TIPS = [
    # 기본 운영·구조
    'AL은 어려운 단어 시험이 아니다 — 자연스러운 운영 능력이 핵심',
    '답변 흐름: Intro → Main Point → What/Feeling/Why → Detail → Example → Lesson → Summary',
    '답변은 Main Point 먼저 — "Well, I would say I really like..."로 시작',
    '첫 문장은 무조건 쉽게 — "Well, when it comes to ~"로 안전하게 출발',
    'Main Point 직후 What / Feeling / Why 세 묶음으로 풀어주기',
    '8~15문장이면 충분 — 짧고 구조적인 답이 더 강하다',
    # 분량 늘리기
    'Body 안 늘어나면 디테일·감정·예시·비교·의견 중 2~3개만 더 붙이기',
    '40초 답(묘사·습관·취향)은 7문장 템플릿에 끼워 넣기',
    '1분 답(과거 경험·비교)은 When/What/Feeling/Lesson 순으로',
    # 유형별
    '묘사 문제는 정보 나열보다 느낌·분위기 — 사람·사물·공간의 인상',
    '과거 경험: When → What → Feeling → Lesson 순서로',
    '비교 문제는 "In the past... / These days..." 대비 구조 (AL 핵심 유형)',
    'AI·기술 소재는 과거 vs 현재 비교에서 자연스럽게 녹이기',
    # 롤플레이
    '롤플은 문법보다 상황 해결 — 공손하고 자연스럽게',
    '롤플 질문: 인사 → 목적 → 질문 3~4개 → 마무리',
    '롤플 문제해결 5단: 인사 → 사과 톤 목적 → 상황 설명 → 대안 → 마무리',
    # 표현·전달
    '필러(Well, Honestly, You know, Actually) 섞되 같은 필러 반복은 NG',
    '같은 단어 반복 줄이기 — 표현 다양성이 레벨을 가른다',
    '시제 안정적으로 — 사건은 과거, 습관은 현재',
    '고급 인상: 시제·부사·구동사·연결어를 의식적으로 섞기',
    '마무리는 "So overall..." / "So that\'s why..."로 자연스럽게 멈추기',
    # 멘탈·전략
    '답이 안 떠오르면 멈추지 말고 비상 구조(아는 방향)로 틀기',
    '모르는 주제가 나와도 당황 X — 내가 아는 경험으로 끌고 오기',
    '콤보 대비: 주제 1개 = 5방향(묘사·루틴·경험·비교·롤플)으로 연습',
    '주제마다 외우지 말고 재활용 스토리 4개 묶음으로 준비',
]


def pick_opic_tip():
    """알림용 OPIc 한 줄 팁 랜덤 1개."""
    import random
    return random.choice(OPIC_NOTIFY_TIPS)


def pick_random_expression():
    """Return one random Expression (model instance), or None."""
    import random
    n = Expression.objects.count()
    if n == 0:
        return None
    return Expression.objects.all()[random.randint(0, n - 1)]


def pick_expressions_for_notification(user=None, n: int = 3, prefer_user_feedback: bool = True):
    """알림용 표현 N개 선택. 우선 풀(본인 첨삭 + 사용자 메모 보강)에서 최대 n-1개,
    부족하면 전체 풀(curated 포함)에서 보충.

    우선 풀 = source='feedback' AND source_user=user  OR  source='user_note'
    (user_note는 admin이 직접 등록한 학습 메모 — 전역 공유, 알림에 자주 노출시킴)

    user가 None이면 우선 풀은 user_note만. Returns: list[Expression] (최대 n개).
    """
    import random
    from django.db.models import Q

    out: list = []
    seen_ids: set = set()

    if prefer_user_feedback:
        priority_q = Q(source='user_note')
        if user is not None:
            priority_q = priority_q | Q(source='feedback', source_user=user)
        candidates = list(Expression.objects.filter(priority_q).order_by('-id')[:60])
        if candidates:
            take_max = min(len(candidates), max(0, n - 1))  # 최소 1자리는 일반 풀에 양보
            if take_max > 0:
                picked = random.sample(candidates, take_max)
                for e in picked:
                    out.append(e)
                    seen_ids.add(e.id)

    if len(out) < n:
        pool = list(Expression.objects.exclude(id__in=seen_ids))
        if pool:
            need = n - len(out)
            random.shuffle(pool)
            for e in pool[:need]:
                out.append(e)

    return out


def upsert_feedback_expressions(entry, items, user=None) -> int:
    """첨삭 feedback 안의 expressions[] → Expression 테이블에 upsert.

    en이 unique이라 기존 행이 있으면 덮어쓰지 않고 source_entry/source_user만 비어있을 때 채움.
    새 행은 source='feedback'으로 저장.
    Returns: 새로 만들어진 행 개수.
    """
    if not items or not isinstance(items, list):
        return 0
    created = 0
    for raw in items:
        if not isinstance(raw, dict):
            continue
        en = (raw.get('en') or '').strip()
        ko = (raw.get('ko') or '').strip()
        if not en or not ko:
            continue
        if len(en) > 200 or len(ko) > 300:
            continue
        defaults = {
            'ko': ko[:300],
            'example': (raw.get('example') or '').strip(),
            'tip': (raw.get('tip') or '').strip(),
            'category': (raw.get('category') or '').strip()[:50],
            'source': 'feedback',
            'source_entry': entry,
            'source_user': user,
        }
        try:
            obj, was_created = Expression.objects.get_or_create(en=en[:200], defaults=defaults)
            if was_created:
                created += 1
            else:
                # 기존 행이 source_entry 없으면 채워주기 (curated 표현이 다시 나온 케이스는 건드리지 않음)
                if obj.source == 'feedback' and obj.source_entry_id is None and entry is not None:
                    obj.source_entry = entry
                    if user is not None and obj.source_user_id is None:
                        obj.source_user = user
                    obj.save(update_fields=['source_entry', 'source_user'])
        except Exception:
            logger.exception(f'upsert feedback expression 실패: {en!r}')
    return created


def pick_random_quote():
    """Return one random quote dict from data/quotes.json, or None."""
    import random
    from pathlib import Path
    from django.conf import settings as ds
    path = Path(ds.BASE_DIR) / 'data' / 'quotes.json'
    if not path.is_file():
        return None
    try:
        quotes = json.loads(path.read_text(encoding='utf-8'))
        return random.choice(quotes) if quotes else None
    except Exception:
        return None


def build_status_line(user_label: str, has_diary: bool, has_opic: bool, day=None) -> str:
    """포맷: '오늘자 2026-05-26 Danny 일기 ✅, opic ☐'.
    user_label은 display_name 우선/없으면 username/둘 다 없으면 생략.
    day가 None이면 오늘 날짜 사용."""
    from datetime import date
    if day is None:
        day = date.today()
    date_str = day.isoformat() if hasattr(day, 'isoformat') else str(day)
    diary_box = '✅' if has_diary else '☐'
    opic_box = '✅' if has_opic else '☐'
    who = f' {user_label}' if user_label else ''
    return f'오늘자 {date_str}{who} 일기 {diary_box}, opic {opic_box}'


def resolve_user_label(user_obj_or_username) -> str:
    """User 객체 → display_name (없으면 username). 문자열이면 그대로. None이면 ''."""
    if not user_obj_or_username:
        return ''
    if isinstance(user_obj_or_username, str):
        try:
            u = User.objects.get(username=user_obj_or_username)
            return u.display_name or u.username
        except User.DoesNotExist:
            return user_obj_or_username
    return user_obj_or_username.display_name or user_obj_or_username.username


def _expression_block(exp) -> list:
    """표현 한 개를 본문 줄들로. 번역 + (있으면) 예문까지.
    Slack mrkdwn — 예문은 이탤릭(`_..._`)으로 들여쓰기.
    """
    head = f'💬 *{exp.en}*'
    if exp.ko:
        head += f' — {exp.ko}'
    block = [head]
    ex = (exp.example or '').strip()
    if ex:
        block.append(f'    _{ex}_')
    return block


def build_notification(user=None, status_line: str = '', flavor: str = '', expressions_count: int = 3):
    """알림 title + body를 함께 생성.

    안드로이드/Slack mobile push 미리보기는 title + 본문 앞 몇 줄만 보여주므로,
    리마인드 핵심인 영어 표현을 **title과 본문 맨 위**에 배치한다.

    본문 순서:
      (title과 간격용 빈 줄 2개) → 표현 N개(번역+예문) → 빈 줄
      → 명언 → OPIc 한 줄 꿀팁 → 빈 줄 → 진척도(status_line) → 격려(flavor)

    user를 넘기면 그 user의 첨삭/노트 표현을 우선 선택, 부족하면 curated 보충.
    Returns: (title, body) 튜플.
    """
    picks = pick_expressions_for_notification(user=user, n=expressions_count)

    # title: 번역 없이 표현 N개의 영어만 ' · '로 구분해 한 줄에 (push 미리보기에 한눈에).
    if picks:
        title = '💬 ' + ' · '.join(exp.en for exp in picks)
    else:
        title = '🌙 오늘의 영어'

    # title(header)과 본문(section) 사이를 더 띄우기 위해 본문을 빈 줄 2개로 시작.
    lines = ['', '']
    for exp in picks:
        lines.extend(_expression_block(exp))
    q = pick_random_quote()
    if q and q.get('text'):
        lines.append('')
        q_line = f'"{q["text"]}"'
        if q.get('author'):
            q_line += f' — {q["author"]}'
        lines.append(q_line)
    # 명언 바로 밑에 OPIc 한 줄 꿀팁
    lines.append(f'🎯 OPIc 팁: {pick_opic_tip()}')
    if status_line:
        lines.append('')
        lines.append(status_line)
    if flavor:
        lines.append(flavor)
    return title, '\n'.join(lines)


def expression_random(request):
    """Return a single random Expression. No auth required (public content)."""
    from django.db.models import Count
    n = Expression.objects.aggregate(c=Count('id'))['c'] or 0
    if n == 0:
        return JsonResponse({'error': 'no expressions seeded'}, status=404)
    import random
    idx = random.randint(0, n - 1)
    exp = Expression.objects.all()[idx]
    return JsonResponse(exp.to_dict())


def expression_list(request):
    """Return all expressions, optionally filtered by ?q= substring (en/ko/category).

    최근 추가된 것 먼저(-created_at). admin UI에서 최신 항목이 위로 노출되도록.
    """
    from django.db.models import Q
    qs = Expression.objects.all().order_by('-created_at', 'en')
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(en__icontains=q) | Q(ko__icontains=q) | Q(category__icontains=q) | Q(tip__icontains=q))
    return JsonResponse({
        'count': qs.count(),
        'expressions': [e.to_dict() for e in qs],
    })


def expression_categories(request):
    """Return distinct categories with counts (for admin UI category filter)."""
    from django.db.models import Count
    rows = (Expression.objects
            .exclude(category='')
            .values('category')
            .annotate(n=Count('id'))
            .order_by('-n', 'category'))
    return JsonResponse({'categories': list(rows)})


def quotes_list(request):
    """Return all motivational quotes from data/quotes.json."""
    from pathlib import Path
    from django.conf import settings as ds
    path = Path(ds.BASE_DIR) / 'data' / 'quotes.json'
    if not path.is_file():
        return JsonResponse({'count': 0, 'quotes': []})
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        data = []
    return JsonResponse({'count': len(data), 'quotes': data})


def opic_combo_stats(request):
    """Aggregate Entry rows (mode=opic) by combo + question index for the current user.

    Returns counts only — the combo catalog itself lives in the frontend
    (OPIC_COMBOS const). The frontend joins these counts onto its catalog.
    """
    user = _current_user(request)
    if user is None:
        return JsonResponse({'totalAnswers': 0, 'combosAnswered': 0, 'byCombo': {}})

    from django.db.models import Count
    rows = (Entry.objects
            .filter(user=user, mode='opic')
            .values('opic_combo', 'opic_question_index')
            .annotate(count=Count('id')))

    # { combo_id: { q_index: count, ..., total: N } }
    out: dict = {}
    total_answers = 0
    for r in rows:
        cid = r['opic_combo']
        qidx = r['opic_question_index']
        cnt = r['count']
        if not cid:
            continue
        bucket = out.setdefault(cid, {'questions': {}, 'total': 0})
        if qidx is not None:
            bucket['questions'][str(qidx)] = cnt
        bucket['total'] += cnt
        total_answers += cnt

    return JsonResponse({
        'totalAnswers': total_answers,
        'combosAnswered': len(out),
        'byCombo': out,
    })


def diagnose(request):
    """Diagnostic endpoint: tests whether Claude CLI is callable and works."""
    return JsonResponse(claude_diagnose(), json_dumps_params={'indent': 2})


# ============ Auth (ID-based for users, password gate for admin) ============

@csrf_exempt
@require_http_methods(['POST'])
def auth_login(request):
    """User login by ID. Body: {username}. Returns user info or 404."""
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    uid = (data.get('username') or '').strip()
    if not uid:
        return JsonResponse({'error': 'username required'}, status=400)
    try:
        u = User.objects.get(username=uid)
    except User.DoesNotExist:
        return JsonResponse({'error': '등록되지 않은 ID입니다. 관리자에게 문의하세요.'}, status=404)
    return JsonResponse(u.to_dict())


@csrf_exempt
@require_http_methods(['POST'])
def auth_admin_check(request):
    """Admin login. Body: {password}. Returns ok or 403."""
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    if (data.get('password') or '') != ADMIN_PASSWORD:
        return JsonResponse({'error': '비밀번호가 틀렸습니다.'}, status=403)
    return JsonResponse({'ok': True})


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def admin_users(request):
    """List or create users. Requires X-Admin-Password header."""
    if not _require_admin(request):
        return JsonResponse({'error': '관리자 인증 필요'}, status=403)

    if request.method == 'GET':
        from django.db.models import Count
        users = User.objects.annotate(n=Count('entries')).order_by('created_at')
        return JsonResponse({
            'users': [u.to_dict(entries_count=u.n) for u in users],
        })

    # POST — create new user
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    uid = (data.get('username') or '').strip()
    if not uid:
        return JsonResponse({'error': 'username required'}, status=400)
    if not re.match(r'^[A-Za-z0-9_.-]{2,64}$', uid):
        return JsonResponse({'error': 'username은 영문/숫자/_.- 2~64자'}, status=400)
    if User.objects.filter(username=uid).exists():
        return JsonResponse({'error': '이미 존재하는 ID입니다.'}, status=409)
    u = User.objects.create(username=uid, display_name=(data.get('displayName') or '').strip())
    return JsonResponse(u.to_dict(entries_count=0), status=201)


@csrf_exempt
@require_http_methods(['DELETE'])
def admin_user_detail(request, username: str):
    if not _require_admin(request):
        return JsonResponse({'error': '관리자 인증 필요'}, status=403)
    try:
        u = User.objects.get(username=username)
    except User.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    u.delete()  # cascades to entries
    return JsonResponse({'status': 'deleted'})


# ============ Admin: AI-fetch more expressions ============

EXPRESSION_FETCH_PROMPT = """You are curating Korean-friendly English conversational expressions for an OPIc study app.

Generate exactly {count} NEW English conversational expressions (single phrases or short useful patterns — not full sentences) suitable for AL-level OPIc speakers.

{category_directive}

Each entry MUST be a JSON object with these fields:
- "en": the English expression (a phrase/idiom/pattern, keep under ~40 chars where possible)
- "ko": natural Korean meaning (translation), short
- "example": ONE short English example sentence using it
- "tip": short Korean usage note (under 60 chars) — when/how to use it
- "category": one short Korean tag (use one from the preferred set if it fits)

Preferred category set:
필러, 감정표현, 시간표현, 비교/대조, 요약, 추측표현, 정중표현, 가정법, 조동사, 패턴, 구동사, 슬랭, 맞장구, Pausing, 스몰토크, 의견표현, 자기소개, 형용사, 역접접속사, 인과접속사, 목적접속사, 조건접속사, 시간접속사

CRITICAL — DO NOT include any of these already-existing expressions (matched case-insensitively):
{existing_csv}

Output discipline:
- Return ONLY the JSON array. No prose. No markdown fences. No commentary.
- Your output starts with `[` and ends with `]`.
- Make sure every entry is genuinely useful for casual / OPIc conversation (avoid textbook phrases).
"""


def _category_directive(categories):
    """Build a prompt directive describing the requested category focus."""
    if not categories:
        return 'Mix categories — spread across many of the preferred categories. Do not dump everything in one bucket.'
    cats_quoted = ', '.join(f'"{c}"' for c in categories)
    return f'FOCUS: Generate expressions ONLY for these categories: {cats_quoted}. Every entry\'s "category" field MUST be one of these. Distribute roughly evenly across them.'


@csrf_exempt
@require_http_methods(['POST'])
def admin_expressions_fetch(request):
    """AI-driven expression generator. Admin-only.

    Body: { "count": 20, "model": "sonnet" }
    """
    if not _require_admin(request):
        return JsonResponse({'error': '관리자 인증 필요'}, status=403)

    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    try:
        count = max(5, min(60, int(data.get('count', 20))))
    except (TypeError, ValueError):
        count = 20
    model = (data.get('model') or 'sonnet').strip().lower()
    if model not in {'haiku', 'sonnet', 'opus'}:
        model = 'sonnet'
    categories_in = data.get('categories') or []
    categories = [str(c).strip() for c in categories_in if str(c).strip()] if isinstance(categories_in, list) else []

    # Existing en list — keep prompt size sane by dropping past ~250 entries to a sample.
    existing_qs = list(Expression.objects.values_list('en', flat=True))
    existing_lower = {e.lower() for e in existing_qs}
    existing_for_prompt = existing_qs if len(existing_qs) <= 250 else existing_qs[:250]
    existing_csv = ', '.join(f'"{e}"' for e in existing_for_prompt)

    prompt = EXPRESSION_FETCH_PROMPT.format(
        count=count,
        existing_csv=existing_csv,
        category_directive=_category_directive(categories),
    )

    try:
        raw = call_claude(prompt, model=model, timeout=240)
    except ClaudeCodeError as e:
        return JsonResponse({'error': f'Claude 호출 실패: {e}'}, status=500)

    parsed, parse_err = _extract_json(raw)
    if parse_err or not isinstance(parsed, list):
        return JsonResponse({
            'error': 'AI 응답을 파싱할 수 없습니다.',
            'parse_error': parse_err,
            'raw_preview': raw[:600],
        }, status=502)

    added = 0
    skipped_dup = 0
    skipped_bad = 0
    new_rows = []
    for row in parsed:
        if not isinstance(row, dict):
            skipped_bad += 1
            continue
        en = str(row.get('en') or '').strip()
        ko = str(row.get('ko') or '').strip()
        if not en or not ko:
            skipped_bad += 1
            continue
        if en.lower() in existing_lower:
            skipped_dup += 1
            continue
        existing_lower.add(en.lower())  # protect against in-batch dupes
        new_rows.append(Expression(
            en=en,
            ko=ko,
            example=str(row.get('example') or '').strip(),
            tip=str(row.get('tip') or '').strip(),
            category=str(row.get('category') or '').strip(),
        ))

    added_items = []
    if new_rows:
        # bulk_create returns the rows (with PKs on most DB backends incl. SQLite when ignore_conflicts is off)
        # Using ignore_conflicts=True silently drops dupes but loses PKs — so look up by en afterward.
        Expression.objects.bulk_create(new_rows, ignore_conflicts=True)
        added_ens = [r.en for r in new_rows]
        added_items = [e.to_dict() for e in Expression.objects.filter(en__in=added_ens)]
        added = len(added_items)

    return JsonResponse({
        'status': 'ok',
        'requested': count,
        'received': len(parsed),
        'added': added,
        'added_items': added_items,
        'skipped_duplicate': skipped_dup,
        'skipped_invalid': skipped_bad,
        'total_now': Expression.objects.count(),
        'categories': categories,
        'model': model,
    })


# --- AI batch enrichment (fill missing tips + improve examples) ---

EXPRESSION_ENRICH_PROMPT = """You are enriching English conversational expressions for a Korean OPIc study app.

For each item below, do TWO things:
1. Fill in a SHORT Korean usage tip (under 60 chars) — when/how to use it naturally. If the existing tip is empty or weak, replace it with a better one.
2. Improve the example sentence if it's missing or low-quality. Keep it short (under 100 chars), natural conversational English, and actually using the "en" expression.

CRITICAL:
- DO NOT change the "en", "ko", or "category" fields.
- Return the SAME number of items in the SAME order as a JSON array, with ALL fields preserved.

Items to enrich:
{items_json}

Output discipline:
- Return ONLY the JSON array. No prose. No markdown fences.
- Your output starts with `[` and ends with `]`.
"""


@csrf_exempt
@require_http_methods(['POST'])
def admin_expressions_enrich(request):
    """Batch-enrich expressions: fill missing tips + improve examples. Admin-only.

    Body: { mode: 'missing-tips' | 'all', batch_size: 20, model: 'sonnet', offset: 0 }
    Processes one batch per call. Frontend can loop until `remaining_missing_tips` is 0.
    """
    if not _require_admin(request):
        return JsonResponse({'error': '관리자 인증 필요'}, status=403)

    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    mode = (data.get('mode') or 'missing-tips').strip().lower()
    if mode not in {'missing-tips', 'all'}:
        mode = 'missing-tips'
    try:
        batch_size = max(5, min(40, int(data.get('batch_size', 20))))
    except (TypeError, ValueError):
        batch_size = 20
    try:
        offset = max(0, int(data.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    model = (data.get('model') or 'sonnet').strip().lower()
    if model not in {'haiku', 'sonnet', 'opus'}:
        model = 'sonnet'

    if mode == 'missing-tips':
        # always grab the first N with empty tip — each successful pass removes them from this set
        qs = Expression.objects.filter(tip='').order_by('id')[:batch_size]
    else:
        qs = Expression.objects.all().order_by('id')[offset:offset + batch_size]

    rows = list(qs)
    if not rows:
        return JsonResponse({
            'processed': 0, 'updated': 0, 'failed': 0,
            'remaining_missing_tips': Expression.objects.filter(tip='').count(),
            'total': Expression.objects.count(),
            'batch_items': [],
            'next_offset': offset,
            'done': True,
        })

    items_for_prompt = [
        {'en': r.en, 'ko': r.ko, 'example': r.example, 'tip': r.tip, 'category': r.category}
        for r in rows
    ]
    prompt = EXPRESSION_ENRICH_PROMPT.format(
        items_json=json.dumps(items_for_prompt, ensure_ascii=False, indent=2)
    )

    try:
        raw = call_claude(prompt, model=model, timeout=240)
    except ClaudeCodeError as e:
        return JsonResponse({'error': f'Claude 호출 실패: {e}'}, status=500)

    parsed, parse_err = _extract_json(raw)
    if parse_err or not isinstance(parsed, list):
        return JsonResponse({
            'error': 'AI 응답 파싱 실패',
            'parse_error': parse_err,
            'raw_preview': raw[:600],
        }, status=502)

    parsed_by_en = {}
    for p in parsed:
        if isinstance(p, dict) and p.get('en'):
            parsed_by_en[str(p['en']).strip().lower()] = p

    updated_items = []
    failed = 0
    for r in rows:
        p = parsed_by_en.get(r.en.lower())
        if not p:
            failed += 1
            continue
        new_tip = str(p.get('tip') or '').strip()
        new_ex = str(p.get('example') or '').strip()
        changed_fields = []
        if new_tip and new_tip != r.tip:
            r.tip = new_tip; changed_fields.append('tip')
        if new_ex and new_ex != r.example:
            r.example = new_ex; changed_fields.append('example')
        if changed_fields:
            r.save(update_fields=changed_fields)
            updated_items.append(r.to_dict())

    remaining = Expression.objects.filter(tip='').count()
    return JsonResponse({
        'processed': len(rows),
        'updated': len(updated_items),
        'failed': failed,
        'remaining_missing_tips': remaining,
        'total': Expression.objects.count(),
        'batch_items': updated_items,
        'next_offset': offset + len(rows),
        'done': (mode == 'missing-tips' and remaining == 0) or (mode == 'all' and offset + len(rows) >= Expression.objects.count()),
        'mode': mode,
        'model': model,
    })


# --- AI quote generation ---

QUOTE_FETCH_PROMPT = """You are curating short motivational English quotes for a language-learning app.

Generate exactly {count} NEW one-sentence English quotes about: language learning, persistence, growth, habits, courage, daily action, or wisdom. Keep them under 110 characters each.

Each entry MUST be a JSON object: {{"text": "...", "author": "..."}}.
- "text": the quote (one sentence, no surrounding quotes).
- "author": real attributable author OR null for anonymous/aphorism.

CRITICAL — DO NOT include any of these already-existing quotes (case-insensitive on text):
{existing_csv}

Output discipline:
- Return ONLY the JSON array. No prose. No markdown fences.
- Your output starts with `[` and ends with `]`.
"""


@csrf_exempt
@require_http_methods(['POST'])
def admin_quotes_fetch(request):
    """AI-driven quote generator. Appends to data/quotes.json. Admin-only."""
    if not _require_admin(request):
        return JsonResponse({'error': '관리자 인증 필요'}, status=403)

    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    try:
        count = max(5, min(40, int(data.get('count', 15))))
    except (TypeError, ValueError):
        count = 15
    model = (data.get('model') or 'sonnet').strip().lower()
    if model not in {'haiku', 'sonnet', 'opus'}:
        model = 'sonnet'

    from pathlib import Path
    from django.conf import settings as ds
    path = Path(ds.BASE_DIR) / 'data' / 'quotes.json'
    existing = []
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            existing = []
    if not isinstance(existing, list):
        existing = []

    existing_lower = {(q.get('text') or '').strip().lower() for q in existing if isinstance(q, dict)}
    existing_for_prompt = existing[:120]
    existing_csv = ', '.join(
        f'"{(q.get("text") or "").replace(chr(34), chr(39))}"'
        for q in existing_for_prompt
    )

    prompt = QUOTE_FETCH_PROMPT.format(count=count, existing_csv=existing_csv)

    try:
        raw = call_claude(prompt, model=model, timeout=180)
    except ClaudeCodeError as e:
        return JsonResponse({'error': f'Claude 호출 실패: {e}'}, status=500)

    parsed, parse_err = _extract_json(raw)
    if parse_err or not isinstance(parsed, list):
        return JsonResponse({
            'error': 'AI 응답을 파싱할 수 없습니다.',
            'parse_error': parse_err,
            'raw_preview': raw[:600],
        }, status=502)

    added_items, skipped_dup, skipped_bad = [], 0, 0
    for row in parsed:
        if not isinstance(row, dict):
            skipped_bad += 1; continue
        text = str(row.get('text') or '').strip().strip('"')
        if not text:
            skipped_bad += 1; continue
        if text.lower() in existing_lower:
            skipped_dup += 1; continue
        existing_lower.add(text.lower())
        author = row.get('author')
        if isinstance(author, str): author = author.strip() or None
        else: author = None
        added_items.append({'text': text, 'author': author})

    if added_items:
        existing.extend(added_items)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    return JsonResponse({
        'status': 'ok',
        'requested': count,
        'received': len(parsed),
        'added': len(added_items),
        'added_items': added_items,
        'skipped_duplicate': skipped_dup,
        'skipped_invalid': skipped_bad,
        'total_now': len(existing),
        'model': model,
    })


# --- AI combo generation ---

COMBO_FETCH_PROMPT = """You are designing new OPIc combo question sets for a Korean OPIc study app.

Generate exactly {count} NEW OPIc combos. Each combo is one topic with 3 connected questions following real OPIc structure.

Each combo MUST be a JSON object with these fields:
- "id": short English kebab-case identifier (e.g. "yoga", "winter-sports", "morning-routine"). MUST be unique.
- "topic": short Korean + English label e.g. "요리 (Cooking)"
- "category": ONE Korean keyword from the allowed list below (e.g. "요리"). MUST match the allowed_categories restriction.
- "questions": array of EXACTLY 3 objects, each with:
  - "type": one of [Description, Routine, Past Experience, Comparison, Role-play (Ask), Role-play (Solve), Opinion]
  - "text": a friendly OPIc-style English question (full sentence, conversational tone like "Tell me about...")

A typical combo structure is: Description → Past Experience → Comparison.
Alternatives: Description → Routine → Past Experience, or Role-play (Ask) → Role-play (Solve) → Past Experience.

{topic_directive}

ALLOWED CATEGORIES (사용자가 고른 OPIc Background Survey 카테고리):
{allowed_categories}
- Every combo MUST belong to one of these categories. Set `category` to the matching keyword.
- If the allowed list is "(no restriction)", you may pick any short Korean keyword.

CRITICAL — DO NOT use any of these already-taken combo IDs (case-insensitive):
{existing_csv}

Output discipline:
- Return ONLY the JSON array. No prose. No markdown fences.
- Your output starts with `[` and ends with `]`.
"""


def _combo_topic_directive(topic_hint: str) -> str:
    if not topic_hint:
        return 'Pick fresh topics not already covered. Mix everyday/hobby/seasonal/cultural areas. Avoid duplicates of common OPIc topics.'
    return f'TOPIC FOCUS: Generate combos around this theme: "{topic_hint}". Each combo should be a distinct angle on this theme.'


def _combos_extras_path():
    from pathlib import Path
    from django.conf import settings as ds
    return Path(ds.BASE_DIR) / 'data' / 'combos_extra.json'


def combos_extras_list(request):
    """Public read: returns AI-added combos. Frontend appends to its OPIC_COMBOS const.

    응답 시 각 콤보의 category를 정규화(공백 제거)하고, 비어있으면 topic에서 자동 추출.
    """
    path = _combos_extras_path()
    if not path.is_file():
        return JsonResponse({'combos': []})
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        data = []
    if not isinstance(data, list):
        data = []
    for c in data:
        if not isinstance(c, dict):
            continue
        cat = _normalize_category(c.get('category') or '')
        if not cat:
            cat = _derive_category(c.get('topic') or '')
        c['category'] = cat
    return JsonResponse({'combos': data})


@csrf_exempt
@require_http_methods(['POST'])
def admin_combos_fetch(request):
    """AI-driven combo generator. Appends to data/combos_extra.json. Admin-only.

    Body: { count, model, topic_hint?, existing_ids: [...] }
    existing_ids must come from the frontend (frontend is source of truth for built-in combos).
    """
    if not _require_admin(request):
        return JsonResponse({'error': '관리자 인증 필요'}, status=403)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    try:
        count = max(1, min(20, int(data.get('count', 5))))
    except (TypeError, ValueError):
        count = 5
    model = (data.get('model') or 'sonnet').strip().lower()
    if model not in {'haiku', 'sonnet', 'opus'}:
        model = 'sonnet'
    topic_hint = str(data.get('topic_hint') or '').strip()
    existing_ids_in = data.get('existing_ids') or []
    if not isinstance(existing_ids_in, list):
        existing_ids_in = []
    existing_lower = {str(x).strip().lower() for x in existing_ids_in if str(x).strip()}

    # 사용자가 고른 카테고리 풀 안에서만 생성하도록 prompt에 명시. 공백 제거 정규화.
    allowed_cats_in = data.get('allowed_categories') or []
    allowed_cats = [_normalize_category(c) for c in allowed_cats_in if isinstance(c, str) and c.strip()]
    allowed_cats = [c for c in allowed_cats if c]
    allowed_str = ', '.join(f'"{c}"' for c in allowed_cats) if allowed_cats else '(no restriction)'

    # Merge with whatever we already have in extras file
    path = _combos_extras_path()
    extras_existing = []
    if path.is_file():
        try:
            extras_existing = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            extras_existing = []
    if not isinstance(extras_existing, list):
        extras_existing = []
    for c in extras_existing:
        if isinstance(c, dict) and c.get('id'):
            existing_lower.add(str(c['id']).strip().lower())

    existing_for_prompt = list(existing_lower)[:200]
    existing_csv = ', '.join(f'"{x}"' for x in existing_for_prompt)

    prompt = COMBO_FETCH_PROMPT.format(
        count=count,
        existing_csv=existing_csv,
        topic_directive=_combo_topic_directive(topic_hint),
        allowed_categories=allowed_str,
    )

    try:
        raw = call_claude(prompt, model=model, timeout=240)
    except ClaudeCodeError as e:
        return JsonResponse({'error': f'Claude 호출 실패: {e}'}, status=500)

    parsed, parse_err = _extract_json(raw)
    if parse_err or not isinstance(parsed, list):
        return JsonResponse({
            'error': 'AI 응답을 파싱할 수 없습니다.',
            'parse_error': parse_err,
            'raw_preview': raw[:600],
        }, status=502)

    added_items, skipped_dup, skipped_bad, skipped_cat = [], 0, 0, 0
    allowed_set = {c for c in allowed_cats}  # 빈 set이면 무제한
    for row in parsed:
        if not isinstance(row, dict):
            skipped_bad += 1; continue
        cid = str(row.get('id') or '').strip()
        topic = str(row.get('topic') or '').strip()
        category = str(row.get('category') or '').strip()
        questions = row.get('questions') or []
        if not cid or not topic or not isinstance(questions, list) or len(questions) != 3:
            skipped_bad += 1; continue
        # validate each question
        ok_qs = []
        for q in questions:
            if not isinstance(q, dict): break
            qtype = str(q.get('type') or '').strip()
            qtext = str(q.get('text') or '').strip()
            if not qtype or not qtext: break
            ok_qs.append({'type': qtype, 'text': qtext})
        if len(ok_qs) != 3:
            skipped_bad += 1; continue
        if cid.lower() in existing_lower:
            skipped_dup += 1; continue
        category = _normalize_category(category)
        if not category:
            category = _derive_category(topic)
        # 사용자가 카테고리 제약을 걸었다면 그 안에 없는 콤보는 풀에 안 넣음.
        if allowed_set and category not in allowed_set:
            skipped_cat += 1; continue
        existing_lower.add(cid.lower())
        added_items.append({'id': cid, 'topic': topic, 'category': category, 'questions': ok_qs})

    if added_items:
        extras_existing.extend(added_items)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(extras_existing, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    return JsonResponse({
        'status': 'ok',
        'requested': count,
        'received': len(parsed),
        'added': len(added_items),
        'added_items': added_items,
        'skipped_duplicate': skipped_dup,
        'skipped_invalid': skipped_bad,
        'skipped_category': skipped_cat,
        'total_extras': len(extras_existing),
        'topic_hint': topic_hint,
        'allowed_categories': allowed_cats,
        'model': model,
    })


# --- Source extraction (URL / pasted text / file upload) ---

EXPRESSION_EXTRACT_PROMPT = """You are extracting English conversational expressions from source material for a Korean OPIc study app.

Below is source content (could be a web page, article, transcript, or notes). Your job: extract up to {count} of the most useful English conversational expressions/phrases/idioms from it that would help a Korean learner sound natural in OPIc speaking.

{category_directive}

Rules:
- Skip generic boilerplate, ads, navigation text, code snippets
- Prefer reusable phrases (idioms, patterns, fillers, transitions) over content-specific sentences
- Translate naturally to Korean (not literal)
- Each entry MUST be JSON with: en, ko, example, tip (optional Korean usage note), category (one short Korean tag)
- DO NOT include any of these already-existing expressions (case-insensitive):
{existing_csv}

Output discipline:
- Return ONLY the JSON array. No prose. No markdown fences. No commentary.
- Your output starts with `[` and ends with `]`.

Source content:
---
{source}
---
"""


def _strip_html_to_text(html: str) -> str:
    """Crude but dependency-free HTML → text. Drops scripts/styles, collapses whitespace."""
    import re
    # Drop script/style blocks entirely
    html = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    # Replace block tags with newlines so paragraphs survive
    html = re.sub(r'</?(p|div|li|br|h[1-6]|tr)[^>]*>', '\n', html, flags=re.IGNORECASE)
    # Strip all remaining tags
    html = re.sub(r'<[^>]+>', '', html)
    # Decode a few common HTML entities
    html = (html.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                .replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' '))
    # Collapse whitespace
    html = re.sub(r'\n{3,}', '\n\n', html)
    html = re.sub(r'[ \t]{2,}', ' ', html)
    return html.strip()


_BROWSER_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
               'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def _extract_youtube_video_id(url: str) -> str | None:
    """Pull the 11-char video ID out of any common YouTube URL form."""
    import re
    from urllib.parse import urlparse, parse_qs
    p = urlparse(url)
    host = (p.hostname or '').lower()
    if 'youtu.be' in host:
        m = re.match(r'/([A-Za-z0-9_-]{11})', p.path)
        return m.group(1) if m else None
    if 'youtube.com' in host or 'youtube-nocookie.com' in host:
        # /watch?v=ID
        if p.path == '/watch':
            v = parse_qs(p.query).get('v', [''])[0]
            if re.match(r'^[A-Za-z0-9_-]{11}$', v):
                return v
        # /shorts/ID, /embed/ID, /live/ID
        m = re.match(r'/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})', p.path)
        if m:
            return m.group(1)
    return None


def _fetch_youtube_transcript(video_id: str) -> str | None:
    """Try to fetch transcript via youtube-transcript-api. Returns plain text or None."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception:
        return None
    try:
        api = YouTubeTranscriptApi()
        # Try a sensible set of languages — English first, then Korean, then anything available
        try:
            fetched = api.fetch(video_id, languages=['en', 'ko', 'ja', 'en-US', 'en-GB'])
        except Exception:
            # fall back to whatever's available
            tlist = api.list(video_id)
            t = next(iter(tlist), None)
            if t is None:
                return None
            fetched = t.fetch()
        snippets = list(fetched)
        # Snippet objects have .text in v1.x; dicts have 'text' in older versions.
        parts = []
        for s in snippets:
            t = getattr(s, 'text', None) or (s.get('text') if isinstance(s, dict) else None)
            if t:
                parts.append(t.strip())
        text = '\n'.join(parts).strip()
        return text or None
    except Exception as e:
        logger.info(f'youtube transcript fetch failed: {e}')
        return None


def _fetch_og_meta(url: str, timeout: int = 15) -> dict:
    """Fetch a page and return its OpenGraph meta (og:title, og:description, og:site_name)."""
    import re
    import urllib.request
    req = urllib.request.Request(url, headers={
        'User-Agent': _BROWSER_UA,
        'Accept-Language': 'en-US,en;q=0.9,ko;q=0.8',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(2 * 1024 * 1024)
    try:
        html = raw.decode('utf-8', errors='replace')
    except Exception:
        html = raw.decode('latin-1', errors='replace')
    out: dict = {}
    # Capture all og: meta tags regardless of attribute order.
    for m in re.finditer(
        r'<meta[^>]+(?:property|name)\s*=\s*"(og:[^"]+|description|title)"[^>]*content\s*=\s*"([^"]*)"',
        html, flags=re.IGNORECASE):
        key = m.group(1).lower()
        out[key] = (m.group(2).replace('&amp;', '&').replace('&#39;', "'")
                              .replace('&quot;', '"').replace('&lt;', '<')
                              .replace('&gt;', '>').strip())
    # Also try reversed order (content before name/property)
    for m in re.finditer(
        r'<meta[^>]+content\s*=\s*"([^"]*)"[^>]*(?:property|name)\s*=\s*"(og:[^"]+|description|title)"',
        html, flags=re.IGNORECASE):
        key = m.group(2).lower()
        out.setdefault(key, m.group(1).strip())
    return out


def _fetch_url_text(url: str, max_chars: int = 30000, timeout: int = 15) -> str:
    """Fetch URL with smart routing for YouTube/Instagram, fallback to generic HTML strip."""
    import urllib.request
    if not url.lower().startswith(('http://', 'https://')):
        raise ValueError('URL은 http:// 또는 https://로 시작해야 합니다.')

    # --- YouTube: prefer transcript, fall back to og meta ---
    vid = _extract_youtube_video_id(url)
    if vid:
        transcript = _fetch_youtube_transcript(vid)
        if transcript:
            return ('[YouTube transcript]\n' + transcript)[:max_chars]
        # transcript unavailable → fall through to meta
        meta = {}
        try:
            meta = _fetch_og_meta(url, timeout=timeout)
        except Exception as e:
            raise RuntimeError(f'YouTube 자막을 가져올 수 없고 메타데이터도 실패: {e}')
        title = meta.get('og:title') or meta.get('title') or ''
        desc  = meta.get('og:description') or meta.get('description') or ''
        body = f'[YouTube — 자막 없음, 메타데이터만]\nTitle: {title}\nDescription: {desc}'.strip()
        if not (title or desc):
            raise RuntimeError('YouTube 자막도 메타도 비어있어요. 다른 영상을 시도해 주세요.')
        return body[:max_chars]

    # --- Instagram: og meta only (caption preview, login-walled content fails) ---
    host = url.split('//', 1)[-1].split('/', 1)[0].lower()
    if 'instagram.com' in host:
        try:
            meta = _fetch_og_meta(url, timeout=timeout)
        except Exception as e:
            raise RuntimeError(f'Instagram 메타데이터 가져오기 실패: {e}')
        title = meta.get('og:title') or meta.get('title') or ''
        desc  = meta.get('og:description') or meta.get('description') or ''
        body = f'[Instagram post]\nTitle: {title}\nCaption: {desc}'.strip()
        if not (title or desc):
            raise RuntimeError('Instagram 포스트가 로그인 필요거나 비공개로 보입니다. 캡션을 텍스트로 붙여넣어 주세요.')
        return body[:max_chars]

    # --- Generic page: HTML strip with browser UA ---
    req = urllib.request.Request(url, headers={
        'User-Agent': _BROWSER_UA,
        'Accept-Language': 'en-US,en;q=0.9,ko;q=0.8',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get('Content-Type', '')
        raw = resp.read(2 * 1024 * 1024)
    try:
        text_raw = raw.decode('utf-8', errors='replace')
    except Exception:
        text_raw = raw.decode('latin-1', errors='replace')
    if 'html' in ctype.lower() or '<html' in text_raw[:2000].lower():
        text = _strip_html_to_text(text_raw)
    else:
        text = text_raw
    return text[:max_chars]


@csrf_exempt
@require_http_methods(['POST'])
def admin_expressions_extract(request):
    """Extract expressions from a URL, pasted text, or uploaded text file. Admin-only.

    Accepts either JSON body:
      { "source_type": "url" | "text", "value": "...", "count": 20, "model": "sonnet" }
    Or multipart form-data with `file` (.txt/.md), plus `count`/`model` fields.
    """
    if not _require_admin(request):
        return JsonResponse({'error': '관리자 인증 필요'}, status=403)

    # --- Resolve source text ---
    source_text = ''
    source_label = ''
    count = 20
    model = 'sonnet'

    # categories filter — supplied in either JSON body or form field (comma-separated)
    categories = []

    if request.content_type and request.content_type.startswith('multipart/form-data'):
        upload = request.FILES.get('file')
        if not upload:
            return JsonResponse({'error': '파일이 첨부되지 않았어요'}, status=400)
        name = (upload.name or '').lower()
        if name.endswith('.pdf'):
            try:
                from pypdf import PdfReader
                from io import BytesIO
                data_bytes = upload.read(10 * 1024 * 1024)  # cap 10MB for PDFs
                reader = PdfReader(BytesIO(data_bytes))
                pages_text = []
                for page in reader.pages:
                    try:
                        pages_text.append(page.extract_text() or '')
                    except Exception:
                        continue
                source_text = '\n\n'.join(pages_text)[:30000]
            except Exception as e:
                return JsonResponse({'error': f'PDF 텍스트 추출 실패: {e}'}, status=400)
        elif name.endswith(('.txt', '.md', '.markdown')):
            try:
                data_bytes = upload.read(2 * 1024 * 1024)
                source_text = data_bytes.decode('utf-8', errors='replace')
            except Exception as e:
                return JsonResponse({'error': f'파일 읽기 실패: {e}'}, status=400)
        else:
            return JsonResponse({'error': '.txt, .md, .pdf 파일만 지원합니다. 다른 형식은 텍스트로 붙여넣어 주세요.'}, status=400)
        source_label = f'file:{upload.name}'
        try:
            count = max(5, min(60, int(request.POST.get('count', 20))))
        except (TypeError, ValueError):
            count = 20
        model = (request.POST.get('model') or 'sonnet').strip().lower()
        cats_raw = (request.POST.get('categories') or '').strip()
        if cats_raw:
            categories = [c.strip() for c in cats_raw.split(',') if c.strip()]
    else:
        try:
            data = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        src_type = (data.get('source_type') or '').strip().lower()
        value = (data.get('value') or '').strip()
        try:
            count = max(5, min(60, int(data.get('count', 20))))
        except (TypeError, ValueError):
            count = 20
        model = (data.get('model') or 'sonnet').strip().lower()
        cats_in = data.get('categories') or []
        if isinstance(cats_in, list):
            categories = [str(c).strip() for c in cats_in if str(c).strip()]
        if src_type == 'url':
            if not value:
                return JsonResponse({'error': 'URL을 입력하세요'}, status=400)
            try:
                source_text = _fetch_url_text(value)
                source_label = f'url:{value}'
            except Exception as e:
                return JsonResponse({'error': f'URL 가져오기 실패: {e}'}, status=400)
        elif src_type == 'text':
            if not value:
                return JsonResponse({'error': '본문을 입력하세요'}, status=400)
            source_text = value[:30000]
            source_label = f'text:{len(value)}chars'
        else:
            return JsonResponse({'error': 'source_type은 url 또는 text 여야 합니다'}, status=400)

    if model not in {'haiku', 'sonnet', 'opus'}:
        model = 'sonnet'
    if not source_text.strip():
        return JsonResponse({'error': '추출할 본문이 비어있습니다'}, status=400)

    # --- Build prompt with existing-en exclusion list (capped) ---
    existing_qs = list(Expression.objects.values_list('en', flat=True))
    existing_lower = {e.lower() for e in existing_qs}
    existing_for_prompt = existing_qs if len(existing_qs) <= 250 else existing_qs[:250]
    existing_csv = ', '.join(f'"{e}"' for e in existing_for_prompt)

    prompt = EXPRESSION_EXTRACT_PROMPT.format(
        count=count,
        existing_csv=existing_csv,
        source=source_text,
        category_directive=_category_directive(categories),
    )

    try:
        raw = call_claude(prompt, model=model, timeout=240)
    except ClaudeCodeError as e:
        return JsonResponse({'error': f'Claude 호출 실패: {e}'}, status=500)

    parsed, parse_err = _extract_json(raw)
    if parse_err or not isinstance(parsed, list):
        return JsonResponse({
            'error': 'AI 응답을 파싱할 수 없습니다.',
            'parse_error': parse_err,
            'raw_preview': raw[:600],
        }, status=502)

    added, skipped_dup, skipped_bad = 0, 0, 0
    new_rows = []
    for row in parsed:
        if not isinstance(row, dict):
            skipped_bad += 1
            continue
        en = str(row.get('en') or '').strip()
        ko = str(row.get('ko') or '').strip()
        if not en or not ko:
            skipped_bad += 1
            continue
        if en.lower() in existing_lower:
            skipped_dup += 1
            continue
        existing_lower.add(en.lower())
        new_rows.append(Expression(
            en=en, ko=ko,
            example=str(row.get('example') or '').strip(),
            tip=str(row.get('tip') or '').strip(),
            category=str(row.get('category') or '').strip(),
        ))
    added_items = []
    if new_rows:
        Expression.objects.bulk_create(new_rows, ignore_conflicts=True)
        added_ens = [r.en for r in new_rows]
        added_items = [e.to_dict() for e in Expression.objects.filter(en__in=added_ens)]
        added = len(added_items)

    return JsonResponse({
        'status': 'ok',
        'source': source_label,
        'source_chars': len(source_text),
        'requested': count,
        'received': len(parsed),
        'added': added,
        'added_items': added_items,
        'skipped_duplicate': skipped_dup,
        'skipped_invalid': skipped_bad,
        'total_now': Expression.objects.count(),
        'categories': categories,
        'model': model,
    })


# ============ Admin: User notes → expressions (raw or AI-enriched) ============

EXPRESSION_NOTE_ENRICH_PROMPT = """\
You are helping a Korean learner curate their personal English expression library.
Below is a free-form note they wrote — phrases they heard, learned, or want to remember.
Lines may be mixed Korean/English, may be partial, may include extra commentary.

Your job: Turn this note into a clean list of {count} expressions worth adding to a study library.

Rules:
- Output JSON ONLY — a single array of objects. No prose, no markdown fences.
- Each object MUST have: en, ko, example, tip, category
  - en: natural English expression / collocation (3-8 words ideal, not a whole sentence)
  - ko: short Korean meaning (under 40 chars)
  - example: one natural English example sentence
  - tip: short Korean usage note (when/how to use it, under 60 chars)
  - category: one short Korean tag (예: '감정 표현', '의견', '연결어', '경험 묘사', '회화 표현', '비즈니스')
- If the user wrote Korean, translate to natural English. If they wrote English, keep and polish it.
- Skip overly common single words (good, nice, hello) — focus on chunks worth practicing.
- Deduplicate. Don't include items already in this exclusion list (en):
  EXCLUDE: [{existing_csv}]

User's note:
\"\"\"
{source}
\"\"\"

Output ONLY the JSON array:"""


def _parse_raw_notes(text: str) -> list[dict]:
    """라인별로 분리해서 (en, ko) 추출. 구분자: '—' '–' '-' ':' '|' '/' 우선순위.

    구분자 없으면 라인 전체가 en. 빈 줄/주석(#로 시작)은 스킵.
    Returns: list of {en, ko, example:'', tip:'', category:''}
    """
    seps = ['—', '–', '::', ':', '|', '/', ' - ', ' -- ']
    out: list[dict] = []
    seen: set = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        en, ko = line, ''
        for sep in seps:
            if sep in line:
                left, _, right = line.partition(sep)
                en = left.strip()
                ko = right.strip()
                if en and ko:
                    break
                en, ko = line, ''
        en = en[:200].strip(' .,;')
        ko = ko[:300].strip()
        if not en:
            continue
        key = en.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({'en': en, 'ko': ko, 'example': '', 'tip': '', 'category': ''})
    return out


@csrf_exempt
@require_http_methods(['POST'])
def admin_expressions_from_notes(request):
    """사용자 학습 노트를 미리보기 items로 변환 (저장은 안 함).

    Body:
      { "text": "...", "mode": "raw"|"ai", "model": "haiku"|"sonnet"|"opus", "count": 12 }

    raw 모드: 라인별 단순 파싱 (en — ko 패턴 인식)
    ai  모드: Claude 호출해서 정제된 items 받기
    """
    if not _require_admin(request):
        return JsonResponse({'error': '관리자 인증 필요'}, status=403)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    text = (data.get('text') or '').strip()
    mode = (data.get('mode') or 'raw').strip().lower()
    if not text:
        return JsonResponse({'error': '본문이 비어있어요'}, status=400)
    if mode not in {'raw', 'ai'}:
        return JsonResponse({'error': 'mode는 raw 또는 ai 여야 합니다'}, status=400)

    if mode == 'raw':
        items = _parse_raw_notes(text[:30000])
        return JsonResponse({'status': 'ok', 'mode': 'raw', 'items': items})

    # AI mode
    model = (data.get('model') or 'sonnet').strip().lower()
    if model not in {'haiku', 'sonnet', 'opus'}:
        model = 'sonnet'
    try:
        count = max(3, min(40, int(data.get('count', 12))))
    except (TypeError, ValueError):
        count = 12

    existing_qs = list(Expression.objects.values_list('en', flat=True))
    existing_for_prompt = existing_qs if len(existing_qs) <= 250 else existing_qs[:250]
    existing_csv = ', '.join(f'"{e}"' for e in existing_for_prompt)

    prompt = EXPRESSION_NOTE_ENRICH_PROMPT.format(
        count=count,
        existing_csv=existing_csv,
        source=text[:20000],
    )
    try:
        raw = call_claude(prompt, model=model, timeout=180)
    except ClaudeCodeError as e:
        return JsonResponse({'error': f'Claude 호출 실패: {e}'}, status=500)

    parsed, parse_err = _extract_json(raw)
    if parse_err or not isinstance(parsed, list):
        return JsonResponse({
            'error': 'AI 응답을 파싱할 수 없습니다.',
            'parse_error': parse_err,
            'raw_preview': raw[:600],
        }, status=502)

    # normalize + dedupe by en (case-insensitive). 저장은 안 하고 미리보기만.
    cleaned: list[dict] = []
    seen: set = set()
    existing_lower = {e.lower() for e in existing_qs}
    for row in parsed:
        if not isinstance(row, dict):
            continue
        en = str(row.get('en') or '').strip()[:200]
        ko = str(row.get('ko') or '').strip()[:300]
        if not en or not ko:
            continue
        key = en.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({
            'en': en,
            'ko': ko,
            'example': str(row.get('example') or '').strip(),
            'tip': str(row.get('tip') or '').strip(),
            'category': str(row.get('category') or '').strip()[:50],
            'already_in_library': key in existing_lower,
        })

    return JsonResponse({
        'status': 'ok',
        'mode': 'ai',
        'model': model,
        'items': cleaned,
        'received': len(parsed),
    })


@csrf_exempt
@require_http_methods(['POST'])
def admin_expressions_save_notes(request):
    """미리보기에서 편집된 items[]를 source='user_note'로 라이브러리에 저장.

    Body: { "items": [{en, ko, example, tip, category}, ...] }
    en이 unique이라 기존 행 있으면 스킵.
    """
    if not _require_admin(request):
        return JsonResponse({'error': '관리자 인증 필요'}, status=403)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return JsonResponse({'error': 'items가 비어있어요'}, status=400)

    existing_lower = {e.lower() for e in Expression.objects.values_list('en', flat=True)}
    added, skipped_dup, skipped_bad = 0, 0, 0
    new_rows: list = []
    for row in items:
        if not isinstance(row, dict):
            skipped_bad += 1
            continue
        en = str(row.get('en') or '').strip()[:200]
        ko = str(row.get('ko') or '').strip()[:300]
        if not en or not ko:
            skipped_bad += 1
            continue
        if en.lower() in existing_lower:
            skipped_dup += 1
            continue
        existing_lower.add(en.lower())
        new_rows.append(Expression(
            en=en, ko=ko,
            example=str(row.get('example') or '').strip(),
            tip=str(row.get('tip') or '').strip(),
            category=str(row.get('category') or '').strip()[:50],
            source='user_note',
        ))
    if new_rows:
        Expression.objects.bulk_create(new_rows, ignore_conflicts=True)
        added_ens = [r.en for r in new_rows]
        added_items = [e.to_dict() for e in Expression.objects.filter(en__in=added_ens)]
        added = len(added_items)
    else:
        added_items = []

    return JsonResponse({
        'status': 'ok',
        'added': added,
        'added_items': added_items,
        'skipped_duplicate': skipped_dup,
        'skipped_invalid': skipped_bad,
        'total_now': Expression.objects.count(),
    })


# ============ Admin: User notes → combos (AI-enriched) ============

COMBO_NOTE_ENRICH_PROMPT = """\
You are helping a Korean OPIc learner curate their custom combo library.
Below is a free-form note they wrote — combo topics with 3 questions each,
in Korean or mixed Korean/English. Lines may be numbered (e.g. "2. 호텔 묘사"),
and combos are typically separated by a blank line and/or a topic header
(e.g. "집휴가-서베이").

Your job: Turn this note into a clean JSON array of OPIc combos.

ALLOWED CATEGORIES (사용자가 고른 OPIc Background Survey 카테고리들):
{allowed_categories}
- Every combo's `category` MUST be one of the above (한국어 한 단어). Match the topic to the closest category.
  If a combo doesn't fit any allowed category, SKIP it (don't invent a new category).
- If the allowed list is empty, use any short Korean keyword (공원/음악/집/카페/...) as category.

Output discipline:
- Return ONLY a JSON array. No prose. No markdown fences. Starts with `[`, ends with `]`.
- Each combo MUST be: {{"id": "...", "topic": "...", "category": "...", "questions": [{{...}}, {{...}}, {{...}}]}}
- Exactly 3 questions per combo.
- id: short kebab-case English slug, unique (e.g. "hotel-survey", "home-vacation-survey", "music-listening-survey").
- topic: short Korean OPIc-style topic label (e.g. "호텔-서베이", "집휴가-서베이", "음악감상-서베이").
- category: ONE Korean keyword from the allowed list above (예: "공원", "호텔", "음악").
- Each question:
    - "type": one short Korean tag — pick from: 묘사, 루틴, 경험, 롤플, 계기, 비교, 변화
      (infer from the user's text — e.g. "묘사" → 묘사, "루틴" → 루틴, "경험/인상깊었던" → 경험,
       "처음 ... 계기" → 계기, "잘못된 ... 롤플/티켓 구매 롤플" → 롤플)
    - "text": natural OPIc-style English question (15~35 words), AL-level phrasing.
      Translate the Korean intent into a typical OPIc question. Use 2nd person, casual register.
      Examples:
        "호텔 묘사" → "I'd like to know about hotels you usually stay at. Could you describe one of your favorite hotels? What does it look like and what makes it special?"
        "마지막으로 집에서 보낸 휴가" → "Could you tell me about the last time you spent a vacation at home? What did you do during that time and how did you spend your days?"
- Skip lines that are pure section headers without 3 follow-up questions.
- If the user's note is ambiguous, infer the most likely OPIc-survey-style interpretation.

CRITICAL — DO NOT use any of these already-taken combo IDs (case-insensitive):
{existing_csv}

User's note:
\"\"\"
{source}
\"\"\"

Output ONLY the JSON array:"""


def _derive_category(topic: str) -> str:
    """콤보 topic에서 한국어 카테고리 키워드 자동 추출. 공백 없는 한 단어로 정규화.
    OPIc Background Survey 카테고리는 띄어쓰기 없이 통일.

      '공원 (Park)'                → '공원'
      '기술 / AI (Technology)'     → '기술'
      '날씨·계절 (Weather)'        → '날씨'
      '호텔-서베이'                → '호텔'
      '국내 여행 (Domestic Travel)' → '국내여행'    ← 공백 제거 → 한 키워드 통합
      '스포츠 관람 (Watching ...)'  → '스포츠관람'

    Frontend의 deriveCategory()와 동일 규칙.
    """
    if not topic:
        return ''
    import re
    s = topic.split('(', 1)[0]
    s = s.split('/', 1)[0]
    s = s.split('-', 1)[0]
    s = s.split('·', 1)[0]
    s = s.split(',', 1)[0]
    return re.sub(r'\s+', '', s).strip()


def _normalize_category(s) -> str:
    """사용자 입력 / 과거 저장값에서 공백 제거하여 한 키워드로."""
    if not s:
        return ''
    import re
    return re.sub(r'\s+', '', str(s)).strip()


def _parse_raw_combos(text: str) -> list[dict]:
    """라인 그룹별 콤보 파싱 (raw 모드).

    그룹 = 빈 줄로 구분. 한 그룹에 정확히 3개 또는 4개 라인이면 콤보로 인식.
    4개면 첫 줄을 topic header로, 3개면 topic 비워둠.
    질문 텍스트 앞의 "숫자. " 패턴은 제거. type은 키워드 휴리스틱으로 추정.
    영문 변환은 안 함 (AI 모드 권장).
    """
    import re
    out: list[dict] = []
    blocks = re.split(r'\n\s*\n', text.strip())
    type_map = [
        ('묘사', '묘사'), ('루틴', '루틴'), ('경험', '경험'),
        ('롤플', '롤플'), ('계기', '계기'), ('비교', '비교'), ('변화', '변화'),
    ]

    def guess_type(q: str) -> str:
        for k, v in type_map:
            if k in q:
                return v
        return '묘사'

    for blk in blocks:
        lines = [l.strip() for l in blk.splitlines() if l.strip() and not l.strip().startswith('#')]
        if len(lines) < 3:
            continue
        topic = ''
        questions_raw: list[str] = []
        if len(lines) >= 4:
            topic = lines[0]
            questions_raw = lines[1:4]
        else:
            questions_raw = lines[:3]
        # 번호 prefix 제거 (e.g. "2. 호텔 묘사" → "호텔 묘사")
        questions = []
        for q in questions_raw:
            q = re.sub(r'^\s*\d+[\.\)]\s*', '', q).strip()
            if not q:
                continue
            questions.append({'type': guess_type(q), 'text': q})
        if len(questions) != 3:
            continue
        # id 생성 — topic이 있으면 거기서, 없으면 첫 질문에서. 한국어 그대로 + 인덱스
        base = topic or questions[0]['text']
        slug = re.sub(r'[^0-9a-zA-Z가-힣]+', '-', base).strip('-').lower()[:40] or f'combo-{len(out)+1}'
        out.append({
            'id': slug,
            'topic': topic,
            'category': _derive_category(topic) or _derive_category(questions[0]['text']),
            'questions': questions,
        })
    return out


@csrf_exempt
@require_http_methods(['POST'])
def admin_combos_from_notes(request):
    """사용자 콤보 노트를 미리보기 items로 변환 (저장 안 함).

    Body: { "text": "...", "mode": "raw"|"ai", "model": "haiku"|"sonnet"|"opus" }
    """
    if not _require_admin(request):
        return JsonResponse({'error': '관리자 인증 필요'}, status=403)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    text = (data.get('text') or '').strip()
    mode = (data.get('mode') or 'ai').strip().lower()
    if not text:
        return JsonResponse({'error': '본문이 비어있어요'}, status=400)
    if mode not in {'raw', 'ai'}:
        return JsonResponse({'error': 'mode는 raw 또는 ai 여야 합니다'}, status=400)

    # 기존 콤보 id (frontend OPIC_COMBOS 빌트인 + extras 파일)
    existing_ids_in = data.get('existing_ids') or []
    existing_lower = {str(x).strip().lower() for x in existing_ids_in if str(x).strip()}
    path = _combos_extras_path()
    if path.is_file():
        try:
            for c in json.loads(path.read_text(encoding='utf-8')) or []:
                if isinstance(c, dict) and c.get('id'):
                    existing_lower.add(str(c['id']).strip().lower())
        except Exception:
            pass

    # 허용 카테고리 — frontend가 사용자 선택을 넘기면 그 안에서만 생성하도록 prompt에 명시.
    allowed_cats_in = data.get('allowed_categories') or []
    allowed_cats = [_normalize_category(c) for c in allowed_cats_in if isinstance(c, str) and c.strip()]
    allowed_cats = [c for c in allowed_cats if c]

    if mode == 'raw':
        items = _parse_raw_combos(text[:30000])
        for it in items:
            it['category'] = _normalize_category(it.get('category')) or _derive_category(it.get('topic') or '')
            it['already_in_library'] = it['id'].lower() in existing_lower
        return JsonResponse({'status': 'ok', 'mode': 'raw', 'items': items})

    # AI mode
    model = (data.get('model') or 'sonnet').strip().lower()
    if model not in {'haiku', 'sonnet', 'opus'}:
        model = 'sonnet'

    existing_for_prompt = list(existing_lower)[:200]
    existing_csv = ', '.join(f'"{x}"' for x in existing_for_prompt) if existing_for_prompt else '(none)'
    allowed_str = ', '.join(f'"{c}"' for c in allowed_cats) if allowed_cats else '(no restriction — pick any short Korean keyword)'
    prompt = COMBO_NOTE_ENRICH_PROMPT.format(
        existing_csv=existing_csv,
        source=text[:20000],
        allowed_categories=allowed_str,
    )
    try:
        raw = call_claude(prompt, model=model, timeout=240)
    except ClaudeCodeError as e:
        return JsonResponse({'error': f'Claude 호출 실패: {e}'}, status=500)

    parsed, parse_err = _extract_json(raw)
    if parse_err or not isinstance(parsed, list):
        return JsonResponse({
            'error': 'AI 응답을 파싱할 수 없습니다.',
            'parse_error': parse_err,
            'raw_preview': raw[:600],
        }, status=502)

    cleaned: list[dict] = []
    for row in parsed:
        if not isinstance(row, dict):
            continue
        cid = str(row.get('id') or '').strip()
        topic = str(row.get('topic') or '').strip()
        category = str(row.get('category') or '').strip()
        questions_in = row.get('questions') or []
        if not cid or not isinstance(questions_in, list) or len(questions_in) != 3:
            continue
        qs = []
        for q in questions_in:
            if not isinstance(q, dict):
                break
            qtype = str(q.get('type') or '').strip()
            qtext = str(q.get('text') or '').strip()
            if not qtype or not qtext:
                break
            qs.append({'type': qtype[:30], 'text': qtext[:600]})
        if len(qs) != 3:
            continue
        category = _normalize_category(category)
        if not category:
            category = _derive_category(topic)
        cleaned.append({
            'id': cid[:50],
            'topic': topic[:80],
            'category': category[:40],
            'questions': qs,
            'already_in_library': cid.lower() in existing_lower,
        })

    return JsonResponse({
        'status': 'ok',
        'mode': 'ai',
        'model': model,
        'items': cleaned,
        'received': len(parsed),
    })


@csrf_exempt
@require_http_methods(['POST'])
def admin_combos_save_notes(request):
    """미리보기에서 편집된 콤보 items[]를 combos_extra.json에 append.

    Body: { "items": [{id, topic, questions:[{type, text}, ...]}, ...] }
    id 중복 (extras 파일 안에 있는 것)이면 스킵. 빌트인과의 중복은 frontend가 existing_ids로 막을 책임.
    """
    if not _require_admin(request):
        return JsonResponse({'error': '관리자 인증 필요'}, status=403)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    items_in = data.get('items')
    if not isinstance(items_in, list) or not items_in:
        return JsonResponse({'error': 'items가 비어있어요'}, status=400)

    path = _combos_extras_path()
    extras_existing: list = []
    if path.is_file():
        try:
            extras_existing = json.loads(path.read_text(encoding='utf-8')) or []
        except Exception:
            extras_existing = []
    if not isinstance(extras_existing, list):
        extras_existing = []
    existing_lower = {str(c.get('id', '')).strip().lower()
                      for c in extras_existing if isinstance(c, dict)}
    # frontend가 보내준 builtin id도 차단
    builtin_in = data.get('existing_ids') or []
    if isinstance(builtin_in, list):
        for x in builtin_in:
            sx = str(x).strip().lower()
            if sx:
                existing_lower.add(sx)

    added, skipped_dup, skipped_bad = [], 0, 0
    for row in items_in:
        if not isinstance(row, dict):
            skipped_bad += 1; continue
        cid = str(row.get('id') or '').strip()[:50]
        topic = str(row.get('topic') or '').strip()[:80]
        category = str(row.get('category') or '').strip()[:40]
        questions_raw = row.get('questions') or []
        if not cid or not isinstance(questions_raw, list) or len(questions_raw) != 3:
            skipped_bad += 1; continue
        ok_qs = []
        for q in questions_raw:
            if not isinstance(q, dict):
                break
            qtype = str(q.get('type') or '').strip()[:30]
            qtext = str(q.get('text') or '').strip()[:600]
            if not qtype or not qtext:
                break
            ok_qs.append({'type': qtype, 'text': qtext})
        if len(ok_qs) != 3:
            skipped_bad += 1; continue
        if cid.lower() in existing_lower:
            skipped_dup += 1; continue
        existing_lower.add(cid.lower())
        category = _normalize_category(category)
        if not category:
            category = _derive_category(topic)
        added.append({'id': cid, 'topic': topic, 'category': category, 'questions': ok_qs})

    if added:
        extras_existing.extend(added)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(extras_existing, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    return JsonResponse({
        'status': 'ok',
        'added': len(added),
        'added_items': added,
        'skipped_duplicate': skipped_dup,
        'skipped_invalid': skipped_bad,
        'total_extras': len(extras_existing),
    })


# ============ Entries ============

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def entries_collection(request):
    user = _current_user(request)
    if user is None:
        return JsonResponse({'error': '로그인 필요', 'entries': []}, status=401)

    if request.method == 'GET':
        entries = Entry.objects.filter(user=user).order_by('completed_at')
        return JsonResponse({'entries': [e.to_dict() for e in entries]})

    # POST — create new entry
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    required = ['date', 'mode', 'text']
    for field in required:
        if not data.get(field):
            return JsonResponse({'error': f'{field} required'}, status=400)

    entry = Entry.objects.create(
        user=user,
        date=data['date'],
        mode=data['mode'],
        text=data['text'],
        feedback=data.get('feedback'),
        raw_feedback=data.get('rawFeedback'),
        model=data.get('model', 'haiku'),
        opic_combo=data.get('opicCombo'),
        opic_question_index=data.get('opicQuestionIndex'),
        opic_question_text=data.get('opicQuestion'),
        opic_question_type=data.get('opicQuestionType'),
    )

    # 첨삭에서 추출된 expressions를 라이브러리에 자동 반영 (실패는 entry 저장 막지 않음).
    extracted = 0
    fb = data.get('feedback')
    if isinstance(fb, dict):
        try:
            extracted = upsert_feedback_expressions(entry, fb.get('expressions'), user=user)
        except Exception:
            logger.exception('expressions 자동 추출 실패')

    # 녹음 파일 link — transcribe에서 keep_audio로 반환된 audio_id가 있으면 entry에 연결.
    audio_id = (data.get('audioId') or '').strip()
    if audio_id and entry.mode == 'opic':
        try:
            _link_temp_audio_to_entry(entry, audio_id)
        except Exception:
            logger.exception('audio link 실패')

    resp = entry.to_dict()
    if extracted:
        resp['extractedExpressions'] = extracted
    return JsonResponse(resp, status=201)


@csrf_exempt
@require_http_methods(['DELETE'])
def entry_detail(request, entry_id: int):
    user = _current_user(request)
    if user is None:
        return JsonResponse({'error': '로그인 필요'}, status=401)
    try:
        entry = Entry.objects.get(id=entry_id, user=user)
    except Entry.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    # 녹음 파일 정리
    if entry.audio_filename:
        try:
            p = _recordings_dir() / entry.audio_filename
            if p.is_file():
                p.unlink()
        except Exception:
            logger.exception('audio 파일 삭제 실패')
    entry.delete()
    return JsonResponse({'status': 'deleted'})


# ============ AI Feedback ============

def _extract_json(raw: str):
    """Try to extract a JSON value (object or array) from arbitrary Claude output.

    Admin AI endpoints expect arrays (`[{...}, {...}, ...]`), while a couple of
    other endpoints expect objects. Tries both: whichever opening bracket
    appears first in the cleaned text is treated as the root. Falls back to
    minor repairs (smart quotes, trailing commas).

    Returns (parsed, error).
    """
    if not raw or not raw.strip():
        return None, 'empty response'

    cleaned = raw.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```\s*$', '', cleaned, flags=re.IGNORECASE)

    obj_start = cleaned.find('{')
    arr_start = cleaned.find('[')
    candidates: list[tuple[str, str, int]] = []  # (open, close, start_idx)
    if arr_start != -1:
        candidates.append(('[', ']', arr_start))
    if obj_start != -1:
        candidates.append(('{', '}', obj_start))
    if not candidates:
        return None, 'no JSON brackets found'
    candidates.sort(key=lambda t: t[2])  # earliest opener wins

    last_err = ''
    for open_ch, close_ch, start in candidates:
        end = cleaned.rfind(close_ch)
        if end <= start:
            continue
        candidate = cleaned[start:end + 1]
        try:
            return json.loads(candidate), None
        except json.JSONDecodeError as e:
            last_err = f'{e}'
            repaired = (
                candidate
                .replace('“', '"').replace('”', '"')
                .replace('‘', "'").replace('’', "'")
            )
            repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
            try:
                return json.loads(repaired), None
            except json.JSONDecodeError as e2:
                last_err = f'{e2}'
                continue

    return None, f'JSON parse failed (tried array and object): {last_err}'


@csrf_exempt
@require_http_methods(['POST'])
def feedback(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    mode = data.get('mode', 'diary')
    text = (data.get('text') or '').strip()
    model = data.get('model', 'haiku')
    opic_question = data.get('opicQuestion', '')

    if not text:
        return JsonResponse({'error': 'text is required'}, status=400)

    if mode == 'opic':
        prompt = build_opic_prompt(text, opic_question)
    else:
        prompt = build_diary_prompt(text)

    try:
        raw = call_claude(prompt, model=model)
    except ClaudeCodeError as e:
        logger.exception('Claude call failed')
        return JsonResponse({
            'error': str(e),
            'type': 'claude_error',
        }, status=500)

    parsed, err = _extract_json(raw)
    if parsed is None:
        return JsonResponse({
            'error': err,
            'raw': raw[:4000],
            'type': 'parse_error',
        }, status=500)

    return JsonResponse({'feedback': parsed, 'raw': raw[:8000]})


# ============ Settings (WebUI 설정) ============

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def settings_view(request):
    if request.method == 'GET':
        s = get_settings()
        is_admin = _require_admin(request)
        # Admin-only secrets: non-admin sees only a "set / not set" boolean
        for k in ADMIN_ONLY_SETTING_KEYS:
            v = s.get(k, '')
            s[f'{k}_present'] = bool(v)
            if not is_admin:
                s[k] = ''
        s['notify_hours_preview'] = compute_notify_hours(s)
        s['notify_slots_preview'] = [{'h': h, 'm': m} for h, m in compute_notify_slots(s)]
        # tunnel URL
        from pathlib import Path
        from django.conf import settings as ds
        tunnel_file = Path(ds.BASE_DIR) / 'data' / 'tunnel_url.txt'
        if tunnel_file.exists():
            url = tunnel_file.read_text().strip()
            if url:
                s['tunnel_url'] = url
        s['last_notify_run'] = _notify_log_mtime()
        # Merge in per-user prefs if logged in. Per-user values override globals
        # of the same key (none currently overlap, but keeps the API symmetric).
        u = _current_user(request)
        if u and isinstance(u.preferences, dict):
            for k in PER_USER_SETTING_KEYS:
                if k in u.preferences:
                    s[k] = u.preferences[k]
        return JsonResponse(s)

    # POST — save (split: per-user keys go to User.preferences, others to global Preference)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    # --- per-user keys ---
    user_updates: dict = {}
    if 'opic_selected_topics' in data:
        raw = data['opic_selected_topics']
        if not isinstance(raw, list):
            return JsonResponse({'error': 'opic_selected_topics must be a list'}, status=400)
        seen: set[str] = set()
        cleaned: list[str] = []
        for v in raw:
            sv = str(v).strip()
            if sv and sv not in seen:
                seen.add(sv)
                cleaned.append(sv)
        if len(cleaned) > 100:
            return JsonResponse({'error': 'too many topics'}, status=400)
        user_updates['opic_selected_topics'] = cleaned

    if user_updates:
        u = _current_user(request)
        if u is None:
            return JsonResponse({'error': '로그인 필요 (per-user setting)'}, status=401)
        prefs = u.preferences if isinstance(u.preferences, dict) else {}
        prefs.update(user_updates)
        u.preferences = prefs
        u.save(update_fields=['preferences'])

    # --- global keys ---
    to_save: dict = {}
    is_admin = _require_admin(request)
    for k in ['site_url', 'notify_user']:
        if k in data:
            to_save[k] = str(data[k]).strip()
    if 'slack_webhook_url' in data:
        if not is_admin:
            return JsonResponse({'error': 'admin only — Slack webhook URL은 관리자만 변경 가능'}, status=403)
        val = str(data['slack_webhook_url']).strip()
        if val and not val.startswith('https://hooks.slack.com/'):
            return JsonResponse({'error': 'Slack Incoming Webhook URL 형식이 아님 (https://hooks.slack.com/...)'}, status=400)
        to_save['slack_webhook_url'] = val
    if 'slack_mention_user_id' in data:
        if not is_admin:
            return JsonResponse({'error': 'admin only — Slack mention ID는 관리자만 변경 가능'}, status=403)
        val = str(data['slack_mention_user_id']).strip()
        # Slack member ID: 'U' or 'W' 시작, 영숫자. 비우면 멘션 없이 발송.
        if val and not (len(val) >= 2 and val[0] in ('U', 'W') and val[1:].isalnum()):
            return JsonResponse({'error': 'Slack member ID 형식이 아님 (예: U01ABCDEF)'}, status=400)
        to_save['slack_mention_user_id'] = val
    int_fields = {
        'notify_start_hour': (0, 23),
        'notify_start_minute': (0, 59),
        'notify_interval_minutes': (1, 1440),
        'notify_count': (0, 48),
    }
    for k, (lo, hi) in int_fields.items():
        if k in data:
            try:
                v = int(data[k])
            except (TypeError, ValueError):
                return JsonResponse({'error': f'{k} must be an integer'}, status=400)
            if v < lo or v > hi:
                return JsonResponse({'error': f'{k} must be between {lo} and {hi}'}, status=400)
            to_save[k] = v

    s = save_settings(to_save) if to_save else get_settings()
    if any(k in to_save for k in int_fields):
        _regenerate_crontab()
    s['notify_hours_preview'] = compute_notify_hours(s)
    # POST 응답에도 GET과 동일하게 slots_preview 채움 — 안 채우면 frontend가
    # 빈 배열로 잘못 해석해서 저장 직후 "알림 비활성화"로 표시되는 버그.
    s['notify_slots_preview'] = [{'h': h, 'm': m} for h, m in compute_notify_slots(s)]
    s['last_notify_run'] = _notify_log_mtime()
    # tunnel URL
    from pathlib import Path
    from django.conf import settings as ds
    tunnel_file = Path(ds.BASE_DIR) / 'data' / 'tunnel_url.txt'
    if tunnel_file.exists():
        url = tunnel_file.read_text().strip()
        if url:
            s['tunnel_url'] = url
    # Mask admin-only secrets in response too
    for k in ADMIN_ONLY_SETTING_KEYS:
        v = s.get(k, '')
        s[f'{k}_present'] = bool(v)
        if not is_admin:
            s[k] = ''
    # echo back per-user values too (for the same merge logic as GET)
    u = _current_user(request)
    if u and isinstance(u.preferences, dict):
        for k in PER_USER_SETTING_KEYS:
            if k in u.preferences:
                s[k] = u.preferences[k]
    return JsonResponse(s)


@csrf_exempt
@require_http_methods(['POST'])
def run_notify(request):
    """대시보드 '🚀 지금 실행' 버튼용. 실제 cron이 부르는 그 명령(`manage.py notify`)을
    그대로 호출. 오늘 일기/Opic 완료 상태에 따라 Slack 발송 OR 스킵 응답."""
    from io import StringIO
    from django.core.management import call_command

    buf = StringIO()
    try:
        # --force 옵션으로 완료 여부 관계없이 메시지 발사 (테스트 목적)
        force = bool(request.GET.get('force')) or bool(
            (json.loads(request.body or b'{}') or {}).get('force')
        )
    except Exception:
        force = False

    # 대시보드 테스트는 항상 "지금 로그인된 user 기준"으로 카운트해야 의도와 맞음
    # (settings.notify_user는 cron 발사용 기본값이지 대시보드 호출과는 별개)
    args = ['notify']
    if force:
        args.append('--force')
    me = _current_user(request)
    if me:
        args.extend(['--user', me.username])
    try:
        call_command(*args, stdout=buf)
        out = buf.getvalue().strip()
        last_run_iso = _notify_log_mtime()
        return JsonResponse({
            'status': 'ok',
            'output': out,
            'lastRun': last_run_iso,
            'userScope': me.username if me else None,
        })
    except Exception as e:
        logger.exception('run_notify failed')
        return JsonResponse({'status': 'error', 'error': f'{type(e).__name__}: {e}'}, status=500)


def _notify_log_mtime() -> str | None:
    """data/notify.log mtime (있으면) — 가장 최근에 cron이 발사된 시점 근사치."""
    from pathlib import Path
    from datetime import datetime, timezone
    from django.conf import settings as ds
    p = Path(ds.BASE_DIR) / 'data' / 'notify.log'
    if not p.exists():
        return None
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
    except Exception:
        return None


@csrf_exempt
@require_http_methods(['POST'])
def test_notify(request):
    """현재 저장된 Slack webhook으로 테스트 알림 발송.
    POST body로 custom 포맷 지정 가능:
      {"title": "...", "message": "..."}
    필드 비우면 기본 메시지/제목 사용."""
    from .mailer import send_via_slack, MailerError

    s = get_settings()
    webhook_url = (s.get('slack_webhook_url') or '').strip()
    mention_user_id = (s.get('slack_mention_user_id') or '').strip()
    if not webhook_url:
        return JsonResponse({'error': 'Slack webhook URL을 먼저 admin 페이지에서 설정하세요'}, status=400)

    # tunnel URL 우선
    from pathlib import Path
    from django.conf import settings as ds
    site_url = (s.get('site_url') or 'http://localhost:8000').rstrip('/')
    tunnel_file = Path(ds.BASE_DIR) / 'data' / 'tunnel_url.txt'
    if tunnel_file.exists():
        url = tunnel_file.read_text().strip()
        if url:
            site_url = url

    # custom 포맷 파싱 (body 비어도 OK)
    try:
        data = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        data = {}
    custom_title = (data.get('title') or '').strip()
    custom_message = (data.get('message') or '').strip()

    if custom_message:
        title = custom_title or '🌙 매일 영어 — 테스트 알림'
        message = custom_message
    else:
        # cron과 동일한 포맷: 표현 3개(맨 위) + 명언 + 진척도 + flavor. title도 표현 1개.
        from datetime import date
        me = _current_user(request)
        today_str = date.today().isoformat()
        entries = Entry.objects.filter(date=today_str)
        if me:
            entries = entries.filter(user=me)
        has_diary = entries.filter(mode='diary').exists()
        has_opic = entries.filter(mode='opic').exists()
        user_label = resolve_user_label(me) if me else ''
        status_line = build_status_line(user_label, has_diary, has_opic)
        auto_title, message = build_notification(
            user=me, status_line=status_line, flavor='🔔 (테스트 발송)',
        )
        title = custom_title or auto_title

    try:
        result = send_via_slack(
            webhook_url=webhook_url,
            title=title,
            message=message,
            click_url=site_url,
            mention_user_id=mention_user_id or None,
        )
        return JsonResponse({
            'status': 'sent',
            'method': 'slack',
            'via': result['host'],
            'mention': bool(mention_user_id),
        })
    except MailerError as e:
        return JsonResponse({'error': str(e), 'type': 'mailer_error'}, status=500)
    except Exception as e:
        import traceback
        logger.exception('test_notify unexpected error')
        return JsonResponse({
            'error': f'{type(e).__name__}: {e}',
            'type': 'internal_error',
            'traceback': traceback.format_exc().splitlines()[-10:],
        }, status=500)


# ============ Import (from artifact's localStorage export) ============

@csrf_exempt
@require_http_methods(['POST'])
def import_data(request):
    user = _current_user(request)
    if user is None:
        return JsonResponse({'error': '로그인 필요'}, status=401)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    entries_data = data.get('entries', {})
    imported = 0
    skipped = 0

    for date_key, entries_list in entries_data.items():
        if not isinstance(entries_list, list):
            entries_list = [entries_list]
        for e in entries_list:
            if not isinstance(e, dict) or not e.get('text'):
                skipped += 1
                continue
            try:
                Entry.objects.create(
                    user=user,
                    date=date_key,
                    mode=e.get('mode', 'diary'),
                    text=e.get('text', ''),
                    feedback=e.get('feedback'),
                    raw_feedback=e.get('rawFeedback'),
                    model=e.get('model', 'haiku'),
                    opic_combo=e.get('opicCombo'),
                    opic_question_index=e.get('opicQuestionIndex'),
                    opic_question_text=e.get('opicQuestion'),
                    opic_question_type=e.get('opicQuestionType'),
                )
                imported += 1
            except Exception:
                logger.exception(f'Failed to import entry for {date_key}')
                skipped += 1

    return JsonResponse({'imported': imported, 'skipped': skipped})


# ============ STT (faster-whisper) + 녹음 저장 ============

# 녹음은 브라우저 MediaRecorder가 통째로 한 파일로 만든 뒤 multipart로 업로드.
# 5분 가까이 말해도 webm/opus 기준 ~3MB 안팎이지만, 보호용 상한.
_STT_MAX_BYTES = 25 * 1024 * 1024  # 25MB hard cap
_RECORDINGS_SUBDIR = 'recordings'
_TMP_RECORDINGS_SUBDIR = 'recordings/tmp'  # entry에 link되기 전 임시 보관 — keep_audio=true일 때만 생성


def _recordings_dir():
    from django.conf import settings as ds
    from pathlib import Path
    return Path(ds.BASE_DIR) / 'data' / _RECORDINGS_SUBDIR


def _tmp_recordings_dir():
    from django.conf import settings as ds
    from pathlib import Path
    return Path(ds.BASE_DIR) / 'data' / _TMP_RECORDINGS_SUBDIR


@csrf_exempt
@require_http_methods(['POST'])
def transcribe(request):
    """multipart/form-data로 audio 파일 받아 텍스트로 변환.

    필드:
      audio (file, 필수) — webm/ogg/mp4/wav 등 ffmpeg가 디코딩 가능한 포맷
      language (str, 선택) — 기본 'en'
      keep_audio (str '1'/'true', 선택) — 들어오면 audio를 임시 보관하고 audio_id 반환.
        나중에 entries POST 시 audio_id를 함께 보내면 entry에 link됨.
    """
    audio_file = request.FILES.get('audio')
    if audio_file is None:
        return JsonResponse({'error': 'audio 파일이 필요합니다 (multipart 필드명: audio)'}, status=400)

    size = getattr(audio_file, 'size', 0) or 0
    if size <= 0:
        return JsonResponse({'error': '빈 파일'}, status=400)
    if size > _STT_MAX_BYTES:
        return JsonResponse({
            'error': f'파일이 너무 큽니다 ({size} bytes > {_STT_MAX_BYTES}).'
        }, status=413)

    language = (request.POST.get('language') or 'en').strip() or 'en'
    keep_audio = (request.POST.get('keep_audio') or '').strip().lower() in {'1', 'true', 'yes'}

    name = (audio_file.name or 'rec.webm')
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else 'webm'
    if ext not in {'webm', 'ogg', 'mp4', 'm4a', 'wav', 'mp3', 'flac', 'aac'}:
        ext = 'webm'

    # 보관 모드면 tmp 디렉토리에 영구 파일명으로 저장. 비보관 모드는 tempfile.
    audio_id = None
    if keep_audio:
        import uuid
        audio_id = f'{uuid.uuid4().hex}.{ext}'
        tmp_dir = _tmp_recordings_dir()
        tmp_dir.mkdir(parents=True, exist_ok=True)
        save_path = tmp_dir / audio_id
        tmp_path = str(save_path)
        with open(tmp_path, 'wb') as f:
            for chunk in audio_file.chunks():
                f.write(chunk)
    else:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=f'.{ext}', delete=False)
        tmp_path = tmp.name
        for chunk in audio_file.chunks():
            tmp.write(chunk)
        tmp.close()

    try:
        from . import stt as stt_mod
        result = stt_mod.transcribe_file(tmp_path, language=language)
        resp = {
            'text': result['text'],
            'duration': result['duration'],
            'language': result['language'],
            'model': os.environ.get('WHISPER_MODEL', 'base.en'),
        }
        if audio_id:
            resp['audio_id'] = audio_id
        return JsonResponse(resp)
    except RuntimeError as e:
        return JsonResponse({'error': str(e), 'type': 'stt_model_error'}, status=500)
    except Exception as e:
        logger.exception('STT 변환 실패')
        return JsonResponse({
            'error': f'STT 변환 실패: {type(e).__name__}: {e}',
            'type': 'stt_runtime_error',
        }, status=500)
    finally:
        # 비보관 모드만 임시 파일 삭제. 보관 모드면 tmp 디렉토리에 그대로 둠 (entries POST가 옮김).
        if not keep_audio:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _link_temp_audio_to_entry(entry, audio_id: str) -> bool:
    """tmp 디렉토리의 audio 파일을 entry 영구 위치로 이동하고 entry.audio_filename 저장."""
    if not audio_id:
        return False
    # audio_id 형식 검증: 16진수 + 확장자만 허용 (path traversal 방지)
    import re
    m = re.fullmatch(r'([a-f0-9]{16,64})\.([a-z0-9]{1,5})', audio_id.lower())
    if not m:
        logger.warning(f'잘못된 audio_id 형식: {audio_id!r}')
        return False
    src = _tmp_recordings_dir() / audio_id
    if not src.is_file():
        logger.warning(f'tmp audio 파일 없음: {src}')
        return False
    ext = m.group(2)
    dst_dir = _recordings_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    new_name = f'{entry.id}.{ext}'
    dst = dst_dir / new_name
    try:
        src.replace(dst)
    except Exception:
        logger.exception('audio 파일 이동 실패')
        return False
    entry.audio_filename = new_name
    entry.save(update_fields=['audio_filename'])
    return True


def entry_audio(request, entry_id: int):
    """GET 본인 entry의 녹음 파일 서빙."""
    user = _current_user(request)
    if user is None:
        return JsonResponse({'error': '로그인 필요'}, status=401)
    try:
        entry = Entry.objects.get(id=entry_id, user=user)
    except Entry.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    if not entry.audio_filename:
        return JsonResponse({'error': '녹음 파일 없음'}, status=404)
    path = _recordings_dir() / entry.audio_filename
    if not path.is_file():
        return JsonResponse({'error': '파일 누락'}, status=410)
    from django.http import FileResponse
    ext = entry.audio_filename.rsplit('.', 1)[-1].lower()
    mime = {
        'webm': 'audio/webm', 'ogg': 'audio/ogg',
        'mp4': 'audio/mp4', 'm4a': 'audio/mp4',
        'wav': 'audio/wav', 'mp3': 'audio/mpeg',
        'flac': 'audio/flac', 'aac': 'audio/aac',
    }.get(ext, 'application/octet-stream')
    return FileResponse(open(path, 'rb'), content_type=mime)
