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
    'ntfy_topic': '',                  # 예: opic-daily-leesk212-7n3xq
    # 알림 스케줄 (24h, KST 기준 — cron이 들고 있음)
    'notify_start_hour': 23,           # 0-23
    'notify_interval_hours': 1,        # 1-24
    'notify_count': 1,                 # 0-24 (0이면 비활성화)
    # cron이 누구의 일기/Opic 완료 여부를 볼지. 비우면 모든 user 합산.
    'notify_user': '',
}

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
        'ntfy_topic': 'NTFY_TOPIC',
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
    for k in ('notify_start_hour', 'notify_interval_hours', 'notify_count'):
        try:
            merged[k] = int(merged.get(k, DEFAULT_SETTINGS[k]))
        except (TypeError, ValueError):
            merged[k] = DEFAULT_SETTINGS[k]
    return {k: v for k, v in merged.items() if k in ALLOWED_SETTING_KEYS}


def save_settings(new: dict) -> dict:
    pref, _ = Preference.objects.get_or_create(key=SETTINGS_KEY, defaults={'value': {}})
    cur = pref.value if isinstance(pref.value, dict) else {}
    # Don't filter empty lists ([] is valid for opic_selected_topics meaning "use all")
    cur.update({k: v for k, v in new.items() if v not in (None, '')})
    pref.value = cur
    pref.save()
    return get_settings()


def compute_notify_hours(s: dict) -> list[int]:
    """Given settings dict, return the list of hour-of-day values where notify cron fires.
    Caps hours at 23 — entries that would overflow are dropped."""
    start = max(0, min(23, int(s.get('notify_start_hour', 23))))
    interval = max(1, min(24, int(s.get('notify_interval_hours', 1))))
    count = max(0, min(24, int(s.get('notify_count', 1))))
    hours = []
    h = start
    for _ in range(count):
        if h > 23:
            break
        hours.append(h)
        h += interval
    return hours


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
    """Best-effort crontab refresh after settings change. Non-blocking:
    fires a background thread so settings POST returns immediately even if
    the host's `crontab` command hangs on a macOS permission prompt etc."""
    import threading
    from pathlib import Path

    def _worker():
        try:
            from django.core.management import call_command
            call_command('write_crontab', verbosity=0)
        except Exception as e:
            logger.info(f'write_crontab skipped: {e}')
        if not Path('/etc/cron.d').exists():
            hours = compute_notify_hours(get_settings())
            ok, msg = _install_user_crontab(hours)
            logger.info(f'host crontab update: ok={ok} — {msg}')

    threading.Thread(target=_worker, daemon=True, name='crontab-regen').start()


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

def pick_random_expression():
    """Return one random Expression (model instance), or None."""
    import random
    n = Expression.objects.count()
    if n == 0:
        return None
    return Expression.objects.all()[random.randint(0, n - 1)]


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


def build_notification_body(*extra_lines):
    """Append a random expression + random quote to the given body lines.

    Returns a single string with everything joined by newlines.
    """
    lines = list(extra_lines)
    exp = pick_random_expression()
    if exp:
        ex_line = f'💬 {exp.en}'
        if exp.ko:
            ex_line += f' — {exp.ko}'
        lines.append('')
        lines.append(ex_line)
    q = pick_random_quote()
    if q and q.get('text'):
        q_line = f'"{q["text"]}"'
        if q.get('author'):
            q_line += f' — {q["author"]}'
        lines.append(q_line)
    return '\n'.join(lines)


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
    """Return all expressions, optionally filtered by ?q= substring (en/ko/category)."""
    from django.db.models import Q
    qs = Expression.objects.all().order_by('category', 'en')
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
- "questions": array of EXACTLY 3 objects, each with:
  - "type": one of [Description, Routine, Past Experience, Comparison, Role-play (Ask), Role-play (Solve), Opinion]
  - "text": a friendly OPIc-style English question (full sentence, conversational tone like "Tell me about...")

A typical combo structure is: Description → Past Experience → Comparison.
Alternatives: Description → Routine → Past Experience, or Role-play (Ask) → Role-play (Solve) → Past Experience.

{topic_directive}

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
    """Public read: returns AI-added combos. Frontend appends to its OPIC_COMBOS const."""
    path = _combos_extras_path()
    if not path.is_file():
        return JsonResponse({'combos': []})
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        data = []
    if not isinstance(data, list):
        data = []
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

    added_items, skipped_dup, skipped_bad = [], 0, 0
    for row in parsed:
        if not isinstance(row, dict):
            skipped_bad += 1; continue
        cid = str(row.get('id') or '').strip()
        topic = str(row.get('topic') or '').strip()
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
        existing_lower.add(cid.lower())
        added_items.append({'id': cid, 'topic': topic, 'questions': ok_qs})

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
        'total_extras': len(extras_existing),
        'topic_hint': topic_hint,
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
    return JsonResponse(entry.to_dict(), status=201)


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
    entry.delete()
    return JsonResponse({'status': 'deleted'})


# ============ AI Feedback ============

def _extract_json(raw: str):
    """Try to extract a JSON object from arbitrary Claude output. Returns (parsed, error)."""
    if not raw or not raw.strip():
        return None, 'empty response'

    cleaned = raw.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```\s*$', '', cleaned, flags=re.IGNORECASE)
    first = cleaned.find('{')
    last = cleaned.rfind('}')
    if first == -1 or last == -1 or last <= first:
        return None, 'no JSON braces found'
    candidate = cleaned[first:last + 1]

    try:
        return json.loads(candidate), None
    except json.JSONDecodeError:
        repaired = (
            candidate
            .replace('“', '"').replace('”', '"')
            .replace('‘', "'").replace('’', "'")
        )
        repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
        try:
            return json.loads(repaired), None
        except json.JSONDecodeError as e:
            return None, f'JSON parse failed: {e}'


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
        s['notify_hours_preview'] = compute_notify_hours(s)
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
    for k in ['site_url', 'ntfy_topic', 'notify_user']:
        if k in data:
            to_save[k] = str(data[k]).strip()
    int_fields = {
        'notify_start_hour': (0, 23),
        'notify_interval_hours': (1, 24),
        'notify_count': (0, 24),
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
    그대로 호출. 오늘 일기/Opic 완료 상태에 따라 ntfy 발송 OR 스킵 응답."""
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
    """현재 저장된 ntfy 토픽으로 테스트 푸시 발송.
    POST body로 custom 포맷 지정 가능:
      {"title": "...", "message": "...", "tags": ["books", ...]}
    필드 비우면 기본 메시지/제목 사용."""
    from .mailer import send_via_ntfy, MailerError

    s = get_settings()
    ntfy_topic = (s.get('ntfy_topic') or '').strip()
    if not ntfy_topic:
        return JsonResponse({'error': 'ntfy 토픽을 먼저 설정하세요'}, status=400)

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
    custom_tags = data.get('tags') if isinstance(data.get('tags'), list) else None

    title = custom_title or '🌙 매일 영어 — 테스트 알림'
    if custom_message:
        message = custom_message
    else:
        message = build_notification_body('ntfy 알림이 정상 동작합니다 ✨')

    try:
        result = send_via_ntfy(
            topic=ntfy_topic,
            title=title,
            message=message,
            click_url=site_url,
            tags=custom_tags or ['white_check_mark'],
        )
        return JsonResponse({
            'status': 'sent',
            'topic': result['topic'],
            'method': 'ntfy',
            'via': result['host'],
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
