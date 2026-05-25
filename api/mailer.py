"""
알림 발송 — ntfy.sh 푸시 전용.

휴대폰에 ntfy 앱 설치 + 토픽 구독, 또는 데스크탑은 https://ntfy.sh/<topic> 열어두기.
가입/인증 불필요.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Optional, List

logger = logging.getLogger(__name__)


class MailerError(Exception):
    pass


def send_via_ntfy(
    topic: str,
    title: str,
    message: str,
    click_url: Optional[str] = None,
    tags: Optional[List[str]] = None,
    priority: int = 3,  # 1=min, 3=default, 5=max
    timeout: int = 10,
) -> dict:
    if not topic:
        raise MailerError('ntfy 토픽이 비어있음')

    payload = {
        'topic': topic,
        'title': title,
        'message': message,
        'priority': int(priority) if isinstance(priority, (int, str)) and str(priority).isdigit() else 3,
    }
    if click_url:
        payload['click'] = click_url
    if tags:
        payload['tags'] = list(tags)

    req = urllib.request.Request(
        'https://ntfy.sh',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    # SSL 문제(특히 macOS) 대비 3단계 fallback: default → certifi → 검증 비활성화
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
                logger.warning(f'ntfy 전송 성공 (SSL fallback={label})')
            return {'method': 'ntfy', 'host': 'ntfy.sh', 'topic': topic, 'response': body[:200], 'ssl': label}
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode('utf-8', 'replace')[:300]
            except Exception:
                err_body = ''
            raise MailerError(f'ntfy HTTP {e.code}: {err_body}')
        except urllib.error.URLError as e:
            reason = getattr(e, 'reason', None)
            last_err = f'URLError({type(reason).__name__ if reason else "?"}): {reason if reason else "no reason"}'
            logger.warning(f'ntfy 시도 {idx} 실패: {last_err}')
            continue
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
            logger.warning(f'ntfy 시도 {idx} 실패: {last_err}')
            continue

    raise MailerError(f'ntfy 전송 실패 (모든 SSL 옵션 시도): {last_err}')
