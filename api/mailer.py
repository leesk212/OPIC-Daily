"""
알림 발송 — Slack Incoming Webhook 전용.

관리자가 Slack app에서 발급한 Incoming Webhook URL을 admin 페이지에 저장하면,
cron + 대시보드 테스트 버튼이 그 URL로 POST한다.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


class MailerError(Exception):
    pass


def _is_slack_webhook(url: str) -> bool:
    return url.startswith('https://hooks.slack.com/')


def send_via_slack(
    webhook_url: str,
    title: str,
    message: str,
    click_url: Optional[str] = None,
    mention_user_id: Optional[str] = None,
    timeout: int = 10,
) -> dict:
    """Slack Incoming Webhook으로 POST. Block Kit으로 제목/본문/링크 분리.

    mention_user_id가 주어지면 메시지 본문 첫 줄에 `<@ID>`를 박아 모바일 푸시를 강제한다.
    (Slack은 멘션 메시지를 데스크탑 활성 여부와 관계없이 모바일에 푸시함.)

    Returns dict with method/host/response — caller-friendly summary.
    Raises MailerError on failure (HTTP error / network / invalid response).
    """
    if not webhook_url:
        raise MailerError('Slack webhook URL이 비어있음')
    if not _is_slack_webhook(webhook_url):
        raise MailerError('Slack webhook URL 형식이 아님 (https://hooks.slack.com/...)')

    if mention_user_id:
        mention = f'<@{mention_user_id}>'
        message = f'{mention}\n{message}'
        fallback_text = f'{mention} {title}\n{message}'
    else:
        fallback_text = f'{title}\n{message}'

    blocks = [
        {
            'type': 'header',
            'text': {'type': 'plain_text', 'text': title, 'emoji': True},
        },
        {
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': message},
        },
    ]
    if click_url:
        blocks.append({
            'type': 'actions',
            'elements': [{
                'type': 'button',
                'text': {'type': 'plain_text', 'text': '📝 지금 쓰러 가기', 'emoji': True},
                'url': click_url,
                'style': 'primary',
            }],
        })

    payload = {
        'text': fallback_text,  # fallback text for notifications + mobile push
        'blocks': blocks,
    }

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    import ssl
    contexts_to_try = [None]
    try:
        import certifi
        contexts_to_try.append(ssl.create_default_context(cafile=certifi.where()))
    except ImportError:
        pass
    insecure_ctx = ssl.create_default_context()
    insecure_ctx.check_hostname = False
    insecure_ctx.verify_mode = ssl.CERT_NONE
    contexts_to_try.append(insecure_ctx)

    last_err = None
    for idx, ctx in enumerate(contexts_to_try):
        try:
            kwargs = {'timeout': timeout}
            if ctx is not None:
                kwargs['context'] = ctx
            with urllib.request.urlopen(req, **kwargs) as resp:
                body = resp.read().decode('utf-8', 'replace')
            label = ['default', 'certifi', 'insecure'][min(idx, 2)]
            if idx > 0:
                logger.warning(f'slack 전송 성공 (SSL fallback={label})')
            if body.strip() != 'ok':
                raise MailerError(f'Slack 응답이 ok 아님: {body[:200]}')
            return {'method': 'slack', 'host': 'hooks.slack.com', 'response': body[:200], 'ssl': label}
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode('utf-8', 'replace')[:300]
            except Exception:
                err_body = ''
            raise MailerError(f'Slack HTTP {e.code}: {err_body}')
        except urllib.error.URLError as e:
            reason = getattr(e, 'reason', None)
            last_err = f'URLError({type(reason).__name__ if reason else "?"}): {reason if reason else "no reason"}'
            logger.warning(f'slack 시도 {idx} 실패: {last_err}')
            continue
        except MailerError:
            raise
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
            logger.warning(f'slack 시도 {idx} 실패: {last_err}')
            continue

    raise MailerError(f'Slack 전송 실패 (모든 SSL 옵션 시도): {last_err}')
