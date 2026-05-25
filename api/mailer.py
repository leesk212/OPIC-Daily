"""
알림 발송 — 3가지 방법 지원:

1) send_via_ntfy()  — ntfy.sh 푸시 알림. 인증 불필요. 휴대폰/데스크탑 푸시. ⭐ 가장 간단.
2) send_via_gmail() — Gmail SMTP. 앱 비밀번호 1번 설정으로 끝. 안정적인 "실제 이메일".
3) send_direct()    — 수신자 MX로 직접. auth 불필요지만 가정 IP에선 거부됨. 비추천.

send_alert(settings, ...) 가 settings 기반으로 자동 분기.
"""
from __future__ import annotations  # for Python 3.9 compat (str | None syntax)

import json
import logging
import smtplib
import socket
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Optional, List

import dns.resolver

logger = logging.getLogger(__name__)


class MailerError(Exception):
    pass


# ============ Method 0: ntfy.sh (인증 없음, 푸시 알림) — 추천 ============

def send_via_ntfy(
    topic: str,
    title: str,
    message: str,
    click_url: Optional[str] = None,
    tags: Optional[List[str]] = None,
    priority: int = 3,  # 1=min, 3=default, 5=max
    timeout: int = 10,
) -> dict:
    """
    https://ntfy.sh로 푸시 알림 전송.
    - 가입/인증 불필요
    - 사용자가 휴대폰에 ntfy 앱 설치 + 토픽 구독
    - 데스크탑은 https://ntfy.sh/<topic> 열어두면 알림 받음
    """
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

    # SSL 문제(특히 macOS) 대비 — 3단계 fallback: 기본 → certifi → 검증 비활성화
    import ssl
    contexts_to_try = [None]  # 1. default

    try:
        import certifi
        contexts_to_try.append(ssl.create_default_context(cafile=certifi.where()))
    except ImportError:
        pass

    # Last resort: insecure (잘 모르는 경우 임시 우회용)
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
            else:
                logger.info(f'ntfy 전송 완료: topic={topic}')
            return {'method': 'ntfy', 'host': 'ntfy.sh', 'topic': topic, 'response': body[:200], 'ssl': label}
        except urllib.error.HTTPError as e:
            # HTTP 에러는 SSL 문제 아니니까 즉시 raise
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

    raise MailerError(
        f'ntfy 전송 실패 (모든 SSL 옵션 시도): {last_err}\n'
        f'macOS의 경우: /Applications/Python\\ 3.x/Install\\ Certificates.command 실행, '
        f'또는 `pip install certifi` 시도해보세요.'
    )


def _build_message(to_email, from_email, from_name, subject, body_text, body_html):
    """RFC 5322 준수 EmailMessage 생성 (Message-ID + Date 포함)."""
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = f'{from_name} <{from_email}>' if from_name else from_email
    msg['To'] = to_email
    # RFC 5322 — Gmail 등 엄격한 서버가 요구
    domain = from_email.split('@', 1)[-1] if '@' in from_email else 'localhost'
    msg['Message-ID'] = make_msgid(domain=domain)
    msg['Date'] = formatdate(localtime=True)
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype='html')
    return msg


# ============ Method 1: Gmail SMTP (앱 비밀번호) — 추천 ============

def send_via_gmail(
    to_email: str,
    gmail_user: str,
    gmail_app_password: str,
    from_name: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """
    Gmail SMTP (smtp.gmail.com:587 + STARTTLS)로 발송.
    gmail_app_password는 https://myaccount.google.com/apppasswords 에서 생성.

    가장 안정적. Gmail이 본인 계정이라 SPF/DKIM 자동 통과.
    """
    if not gmail_user or '@' not in gmail_user:
        raise MailerError('Gmail 사용자 이메일이 잘못됨')
    if not gmail_app_password:
        raise MailerError('Gmail 앱 비밀번호가 비어있음')

    msg = _build_message(
        to_email=to_email,
        from_email=gmail_user,
        from_name=from_name,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
    )

    try:
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=timeout) as srv:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            srv.login(gmail_user, gmail_app_password)
            srv.send_message(msg)
        logger.info(f'Gmail SMTP 발송 완료: {gmail_user} → {to_email}')
        return {'method': 'gmail_smtp', 'host': 'smtp.gmail.com', 'response': 'OK'}
    except smtplib.SMTPAuthenticationError as e:
        raise MailerError(
            f'Gmail 인증 실패 ({e.smtp_code}): {e.smtp_error.decode("utf-8", "replace") if e.smtp_error else ""}. '
            f'앱 비밀번호가 정확한지 확인하세요 — https://myaccount.google.com/apppasswords'
        )
    except (smtplib.SMTPException, socket.error, OSError) as e:
        raise MailerError(f'Gmail SMTP 발송 실패: {type(e).__name__}: {e}')


# ============ Method 2: Direct MX (auth 없음, 자주 차단됨) ============

def _resolve_mx(domain: str) -> List[str]:
    try:
        answers = dns.resolver.resolve(domain, 'MX')
    except Exception as e:
        raise MailerError(f'{domain} MX 조회 실패: {e}')
    sorted_mx = sorted(answers, key=lambda r: r.preference)
    return [str(r.exchange).rstrip('.') for r in sorted_mx]


def send_direct(
    to_email: str,
    from_email: str,
    from_name: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """
    수신자 도메인의 MX 서버로 직접 SMTP 연결.
    auth 불필요지만 SPF/PTR 없는 IP에선 Gmail 등이 차단함.
    """
    if '@' not in to_email:
        raise MailerError(f'잘못된 이메일 형식: {to_email}')
    domain = to_email.split('@', 1)[1]

    mx_hosts = _resolve_mx(domain)
    if not mx_hosts:
        raise MailerError(f'{domain}의 MX 레코드가 없어요')

    msg = _build_message(
        to_email=to_email,
        from_email=from_email,
        from_name=from_name,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
    )

    last_err = None
    for mx_host in mx_hosts:
        try:
            logger.info(f'Direct MX 시도: {mx_host}:25')
            with smtplib.SMTP(mx_host, 25, timeout=timeout) as srv:
                srv.ehlo(socket.gethostname() or 'localhost')
                try:
                    srv.starttls()
                    srv.ehlo(socket.gethostname() or 'localhost')
                except Exception as e:
                    logger.info(f'STARTTLS 불가, plain 계속: {e}')
                srv.send_message(msg)
            return {'method': 'direct_mx', 'host': mx_host, 'response': 'OK'}
        except (smtplib.SMTPException, socket.error, OSError) as e:
            last_err = e
            logger.warning(f'{mx_host} 실패: {e}')
            continue

    raise MailerError(
        f'모든 MX 시도 실패. 마지막: {last_err}\n'
        f'(가정 IP에서 Gmail로 direct 발송은 SPF/DKIM 미설정으로 거부됩니다. Gmail SMTP 사용 권장.)'
    )


# ============ Smart dispatch ============

def send_alert(settings: dict, to_email, from_name, subject, body_text, body_html=None, click_url=None) -> dict:
    """
    settings 기반 자동 분기 (우선순위):
    1. ntfy_topic 있으면 → ntfy 푸시 알림 (간단·즉시)
    2. gmail_app_password 있으면 → Gmail SMTP (실제 이메일)
    3. 그 외 → direct MX (best effort, 자주 실패)
    """
    ntfy_topic = (settings.get('ntfy_topic') or '').strip()
    if ntfy_topic:
        return send_via_ntfy(
            topic=ntfy_topic,
            title=subject,
            message=body_text,
            click_url=click_url,
            tags=['sparkles', 'books'],
        )

    gmail_user = (settings.get('gmail_user') or settings.get('notify_email_from') or '').strip()
    gmail_pw = (settings.get('gmail_app_password') or '').strip()
    if gmail_pw and gmail_user and gmail_user.endswith('@gmail.com'):
        return send_via_gmail(
            to_email=to_email,
            gmail_user=gmail_user,
            gmail_app_password=gmail_pw,
            from_name=from_name,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )

    from_email = gmail_user or f'opic-daily@{socket.gethostname() or "localhost"}'
    return send_direct(
        to_email=to_email,
        from_email=from_email,
        from_name=from_name,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
    )


# Backward-compat alias
send_email = send_alert
