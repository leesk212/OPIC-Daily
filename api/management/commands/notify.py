"""
Daily reminder — Python이 수신자 도메인으로 direct send (Gmail SMTP 불필요).

읽는 설정 우선순위:
  1. data/tunnel_url.txt (cloudflared가 띄운 URL — 있으면 site_url로 사용)
  2. DB Preference (WebUI에서 저장한 값)
  3. .env (NOTIFY_EMAIL 등)
  4. Defaults

Usage:
    python manage.py notify              # 평소 알림
    python manage.py notify --force      # 이미 다 했어도 전송
    python manage.py notify --dry-run    # 미리보기만
"""
from datetime import date
from pathlib import Path

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand

from api.email_template import build_email
from api.mailer import send_alert, MailerError
from api.models import Entry
from api.views import get_settings


class Command(BaseCommand):
    help = "Send 'come learn now' email via direct MX (no Gmail SMTP needed)"

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='이미 완료해도 강제 전송')
        parser.add_argument('--dry-run', action='store_true', help='실제 전송하지 않고 미리보기')

    def handle(self, *args, **options):
        today_str = date.today().isoformat()
        entries = Entry.objects.filter(date=today_str)
        has_diary = entries.filter(mode='diary').exists()
        has_opic = entries.filter(mode='opic').exists()

        s = get_settings()

        # tunnel URL 파일이 있으면 우선
        tunnel_file = Path(django_settings.BASE_DIR) / 'data' / 'tunnel_url.txt'
        if tunnel_file.exists():
            url = tunnel_file.read_text().strip()
            if url:
                s['site_url'] = url
                self.stdout.write(f'🌐 tunnel URL 사용: {url}')

        site_url = (s.get('site_url') or 'http://localhost:8000').rstrip('/')
        to_email = s.get('notify_email_to', '').strip()
        from_name = s.get('notify_from_name', '').strip() or '매일 영어'

        if not to_email:
            self.stdout.write(self.style.ERROR(
                '⚠️  수신자(notify_email_to) 설정 누락. WebUI ⚙️ 또는 .env에서 채워주세요.'
            ))
            return

        if has_diary and has_opic and not options['force']:
            self.stdout.write(self.style.SUCCESS('✅ 오늘 둘 다 완료. 알림 안 보냄.'))
            return

        subject, text_body, html_body = build_email(
            site_url=site_url,
            status={'has_diary': has_diary, 'has_opic': has_opic},
        )

        if options['dry_run']:
            self.stdout.write('=== DRY RUN ===')
            self.stdout.write(f'To: {to_email}')
            self.stdout.write(f'From name: {from_name}')
            self.stdout.write(f'Subject: {subject}')
            if s.get('ntfy_topic'):
                method = f'ntfy (topic: {s.get("ntfy_topic")})'
            elif s.get('gmail_app_password'):
                method = 'gmail_smtp'
            else:
                method = 'direct_mx'
            self.stdout.write(f'Method: {method}')
            self.stdout.write('--- TEXT ---')
            self.stdout.write(text_body)
            return

        try:
            result = send_alert(
                settings=s,
                to_email=to_email,
                from_name=from_name,
                subject=subject,
                body_text=text_body,
                body_html=html_body,
                click_url=site_url,
            )
            self.stdout.write(self.style.SUCCESS(
                f'✅ 발송 완료 (method={result["method"]}, via={result["host"]})'
            ))
        except MailerError as e:
            self.stdout.write(self.style.ERROR(f'❌ 발송 실패: {e}'))
