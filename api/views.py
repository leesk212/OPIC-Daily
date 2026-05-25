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
    'notify_email_to': 'leesk212@gmail.com',
    'notify_email_from': '',          # 발신자 이메일 (Direct MX 모드에서 보일 주소)
    'notify_from_name': '매일 영어',  # 발신자 이름
    'site_url': 'http://localhost:8000',
    # ntfy.sh 푸시 알림 (있으면 자동 사용 — 가장 간단)
    'ntfy_topic': '',                 # 예: opic-daily-leesk212-secret-7n3
    # Gmail SMTP (있으면 사용 — 실제 이메일 원할 때)
    'gmail_user': '',
    'gmail_app_password': '',
}

SETTINGS_KEY = 'app_settings'


def get_settings() -> dict:
    """Load settings: DB → env → defaults"""
    try:
        pref = Preference.objects.get(key=SETTINGS_KEY)
        stored = pref.value if isinstance(pref.value, dict) else {}
    except Preference.DoesNotExist:
        stored = {}

    merged = dict(DEFAULT_SETTINGS)
    # env overrides defaults
    import os as _os
    env_map = {
        'notify_email_to': 'NOTIFY_EMAIL',
        'notify_email_from': 'NOTIFY_FROM_EMAIL',
        'notify_from_name': 'NOTIFY_FROM_NAME',
        'site_url': 'SITE_URL',
        'gmail_user': 'GMAIL_USER',
        'gmail_app_password': 'GMAIL_APP_PASSWORD',
        'ntfy_topic': 'NTFY_TOPIC',
    }
    for k, env_k in env_map.items():
        v = _os.environ.get(env_k)
        if v:
            merged[k] = v
    # DB overrides everything
    merged.update({k: v for k, v in stored.items() if v not in (None, '')})
    return merged


def save_settings(new: dict) -> dict:
    pref, _ = Preference.objects.get_or_create(key=SETTINGS_KEY, defaults={'value': {}})
    cur = pref.value if isinstance(pref.value, dict) else {}
    cur.update({k: v for k, v in new.items() if v not in (None, '')})
    pref.value = cur
    pref.save()
    return get_settings()

logger = logging.getLogger(__name__)


def index(request):
    return render(request, 'index.html')


def health(request):
    return JsonResponse({'status': 'ok'})


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
        # Attempt repair: smart quotes, trailing commas
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
        # mask app password — only indicate set/unset
        s['gmail_app_password_set'] = bool(s.get('gmail_app_password'))
        s['gmail_app_password'] = ''
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
    for k in ['notify_email_to', 'notify_email_from', 'notify_from_name', 'site_url', 'gmail_user', 'ntfy_topic']:
        if k in data:
            to_save[k] = str(data[k]).strip()
    # Only update app password if user provided a non-empty value
    if data.get('gmail_app_password'):
        to_save['gmail_app_password'] = str(data['gmail_app_password']).strip()
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
        if len(cleaned) > 50:
            return JsonResponse({'error': 'too many topics'}, status=400)
        to_save['opic_selected_topics'] = cleaned
    s = save_settings(to_save)
    s['gmail_app_password_set'] = bool(s.get('gmail_app_password'))
    s['gmail_app_password'] = ''
    return JsonResponse(s)


@csrf_exempt
@require_http_methods(['POST'])
def test_email(request):
    """현재 저장된 설정으로 테스트 이메일 발송.
    Gmail SMTP 정보 있으면 그걸로, 없으면 direct MX."""
    from .mailer import send_alert, MailerError
    from .email_template import build_email

    s = get_settings()
    to_email = (s.get('notify_email_to') or '').strip()
    ntfy_topic = (s.get('ntfy_topic') or '').strip()
    if not to_email and not ntfy_topic:
        return JsonResponse({'error': '수신자 이메일 또는 ntfy 토픽을 먼저 설정하세요'}, status=400)

    from_name = (s.get('notify_from_name') or '매일 영어').strip()
    site_url = (s.get('site_url') or 'http://localhost:8000').rstrip('/')

    # tunnel URL 우선
    from pathlib import Path
    from django.conf import settings as ds
    tunnel_file = Path(ds.BASE_DIR) / 'data' / 'tunnel_url.txt'
    if tunnel_file.exists():
        url = tunnel_file.read_text().strip()
        if url:
            site_url = url

    subject, text_body, html_body = build_email(
        site_url=site_url,
        status={'has_diary': False, 'has_opic': False},
    )

    try:
        result = send_alert(
            settings=s,
            to_email=to_email,
            from_name=from_name,
            subject='[테스트] ' + subject,
            body_text=text_body,
            body_html=html_body,
            click_url=site_url,
        )
        return JsonResponse({
            'status': 'sent',
            'to': result.get('topic') or to_email,
            'method': result['method'],
            'via': result['host'],
        })
    except MailerError as e:
        return JsonResponse({'error': str(e), 'type': 'mailer_error'}, status=500)
    except Exception as e:
        import traceback
        logger.exception('test_email unexpected error')
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
