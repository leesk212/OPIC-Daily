"""
오늘 일기/Opic 안 했으면 ntfy.sh 푸시 알림 발송.

설정 우선순위:
  1. data/tunnel_url.txt (cloudflared가 띄운 URL — site_url로 사용)
  2. DB Preference (WebUI에서 저장)
  3. .env
  4. Defaults

Usage:
    python manage.py notify              # 평소 알림
    python manage.py notify --force      # 이미 다 했어도 강제 전송
    python manage.py notify --dry-run    # 미리보기만
"""
import random
from datetime import date
from pathlib import Path

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand

from api.mailer import send_via_ntfy, MailerError
from api.models import Entry
from api.views import get_settings, build_notification_body


FLAVORS = [
    '오늘 영어 한 줄, 내일의 나에게 선물 ✨',
    '5문장만 쓰면 오늘도 streak 살아있어요 🔥',
    '1분만 투자하면 끝나요. 지금 가요 🚀',
    '잠들기 전 영어 한 입 🍪',
    '오늘 빠지면 내일 두 배. 지금이 편해요 😉',
    '탁월은 매일 하는 사람의 것 🌟',
    'Done is better than perfect. 일단 가요!',
]


class Command(BaseCommand):
    help = "Send 'come learn now' ntfy push notification"

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='이미 완료해도 강제 전송')
        parser.add_argument('--dry-run', action='store_true', help='실제 전송 없이 미리보기')

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
        ntfy_topic = (s.get('ntfy_topic') or '').strip()

        if not ntfy_topic:
            self.stdout.write(self.style.ERROR(
                '⚠️  ntfy_topic 설정 누락. WebUI ⚙️에서 토픽을 먼저 설정하세요.'
            ))
            return

        if has_diary and has_opic and not options['force']:
            self.stdout.write(self.style.SUCCESS('✅ 오늘 둘 다 완료. 알림 안 보냄.'))
            return

        if has_diary and has_opic:
            status_line = '✅ 오늘 일기+Opic 둘 다 완료 (강제 알림)'
        elif has_diary:
            status_line = '📝 일기 완료 / 🎤 Opic 미완료'
        elif has_opic:
            status_line = '🎤 Opic 완료 / 📝 일기 미완료'
        else:
            status_line = '☐ 일기 + ☐ Opic 둘 다 미완료'

        title = '🌙 오늘의 영어 시간'
        message = build_notification_body(random.choice(FLAVORS), status_line)

        if options['dry_run']:
            self.stdout.write('=== DRY RUN ===')
            self.stdout.write(f'ntfy topic: {ntfy_topic}')
            self.stdout.write(f'click URL: {site_url}')
            self.stdout.write(f'title: {title}')
            self.stdout.write(f'message: {message}')
            return

        try:
            result = send_via_ntfy(
                topic=ntfy_topic,
                title=title,
                message=message,
                click_url=site_url,
                tags=['books'],
            )
            self.stdout.write(self.style.SUCCESS(
                f'✅ ntfy 발송 완료 → topic={result["topic"]}'
            ))
        except MailerError as e:
            self.stdout.write(self.style.ERROR(f'❌ 발송 실패: {e}'))
