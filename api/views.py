"""API views for entries and AI feedback."""
import json
import logging
import re

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .ai_client import call_claude, ClaudeCodeError, diagnose as claude_diagnose
from .models import Entry, Preference
from .prompts import build_diary_prompt, build_opic_prompt

# Default settings (used as fallback; UI shows these prefilled)
DEFAULT_SETTINGS = {
    'site_url': 'http://localhost:8000',
    'ntfy_topic': '',                  # 예: opic-daily-leesk212-7n3xq
    # 알림 스케줄 (24h, KST 기준 — cron이 들고 있음)
    'notify_start_hour': 23,           # 0-23
    'notify_interval_hours': 1,        # 1-24
    'notify_count': 1,                 # 0-24 (0이면 비활성화)
}

SETTINGS_KEY = 'app_settings'

# Whitelist of keys the API surfaces. Orphaned DB keys from earlier
# (email/SMTP era) are intentionally dropped so they never leak to clients.
ALLOWED_SETTING_KEYS = set(DEFAULT_SETTINGS.keys()) | {'opic_selected_topics'}


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


def _regenerate_crontab() -> None:
    """Best-effort: write /etc/cron.d/opic-daily from current settings.
    Silent no-op when not writable (host environment, no cron, etc.).
    cron daemon picks up changes within ~60 seconds."""
    try:
        from django.core.management import call_command
        call_command('write_crontab', verbosity=0)
    except Exception as e:
        logger.info(f'crontab regen skipped: {e}')


logger = logging.getLogger(__name__)


def index(request):
    resp = render(request, 'index.html')
    # 메인 페이지는 캐시 금지 — 사용자가 git pull 후 매번 새 HTML 보도록
    resp['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp['Pragma'] = 'no-cache'
    return resp


def health(request):
    return JsonResponse({'status': 'ok'})


def opic_combo_stats(request):
    """Aggregate Entry rows (mode=opic) by combo + question index.

    Returns counts only — the combo catalog itself lives in the frontend
    (OPIC_COMBOS const). The frontend joins these counts onto its catalog.
    """
    from django.db.models import Count
    rows = (Entry.objects
            .filter(mode='opic')
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


# ============ Entries ============

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def entries_collection(request):
    if request.method == 'GET':
        entries = Entry.objects.all().order_by('completed_at')
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
    try:
        entry = Entry.objects.get(id=entry_id)
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
        # Surface the computed schedule so the UI can show a preview
        s['notify_hours_preview'] = compute_notify_hours(s)
        # tunnel URL
        from pathlib import Path
        from django.conf import settings as ds
        tunnel_file = Path(ds.BASE_DIR) / 'data' / 'tunnel_url.txt'
        if tunnel_file.exists():
            url = tunnel_file.read_text().strip()
            if url:
                s['tunnel_url'] = url
        return JsonResponse(s)

    # POST — save
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    to_save = {}
    for k in ['site_url', 'ntfy_topic']:
        if k in data:
            to_save[k] = str(data[k]).strip()
    # Notify schedule: integers, validated
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
        to_save['opic_selected_topics'] = cleaned
    s = save_settings(to_save)
    # Re-emit cron file whenever schedule fields changed
    if any(k in to_save for k in int_fields):
        _regenerate_crontab()
    s['notify_hours_preview'] = compute_notify_hours(s)
    return JsonResponse(s)


@csrf_exempt
@require_http_methods(['POST'])
def test_notify(request):
    """현재 저장된 ntfy 토픽으로 테스트 푸시 발송."""
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

    try:
        result = send_via_ntfy(
            topic=ntfy_topic,
            title='🌙 매일 영어 — 테스트 알림',
            message='ntfy 알림이 정상 동작합니다 ✨',
            click_url=site_url,
            tags=['white_check_mark'],
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
