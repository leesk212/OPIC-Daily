"""
오늘 일기/Opic 안 했으면 Slack 알림 발송.

설정 우선순위:
  1. data/tunnel_url.txt (cloudflared가 띄운 URL — site_url로 사용)
  2. DB Preference (admin 페이지에서 저장)
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

from api.mailer import send_via_slack, MailerError
from api.models import Entry, User
from api.views import get_settings, build_notification, build_status_line, resolve_user_label


FLAVORS = [
    '오늘 영어 한 줄, 내일의 나에게 선물 ✨',
    '5문장만 쓰면 오늘도 streak 살아있어요 🔥',
    '1분만 투자하면 끝나요. 지금 가요 🚀',
    '잠들기 전 영어 한 입 🍪',
    '오늘 빠지면 내일 두 배. 지금이 편해요 😉',
    '탁월은 매일 하는 사람의 것 🌟',
    'Done is better than perfect. 일단 가요!',
]

# 일기·Opic 둘 다 완료한 날에도 영어 끈을 놓지 않도록 보내는 리마인드 (복습/추가 연습 권유).
DONE_FLAVORS = [
    '오늘 일기·Opic 다 했어요! 👏 아래 표현 한 번 더 곱씹어볼까요?',
    '완료! 🎉 여기서 한 발 더 — 오늘 표현으로 한 문장 더 말해보기',
    '오늘 목표 달성 ✅ 표현 복습이 진짜 실력으로 굳혀줘요',
    '둘 다 끝! 🌟 자기 전에 표현 소리 내어 읽으면 머리에 콱 박혀요',
    '훌륭해요 💪 추가 Opic 한 콤보 더? 부담되면 표현만 훑어도 좋아요',
    'AL은 매일 조금씩 쌓는 사람의 것. 오늘도 잘했어요 ✨',
]


class Command(BaseCommand):
    help = "Send 'come learn now' Slack notification"

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='이미 완료해도 강제 전송')
        parser.add_argument('--dry-run', action='store_true', help='실제 전송 없이 미리보기')
        parser.add_argument('--user', dest='user_override', default=None,
                            help='username (override settings.notify_user). '
                                 '대시보드 "지금 실행" 버튼이 현재 로그인된 user로 호출.')

    def handle(self, *args, **options):
        today_str = date.today().isoformat()
        s = get_settings()
        # --user 인자가 있으면 그게 우선, 없으면 settings.notify_user
        notify_user = (options.get('user_override')
                       or s.get('notify_user') or '').strip()
        entries = Entry.objects.filter(date=today_str)
        if notify_user:
            entries = entries.filter(user__username=notify_user)
            self.stdout.write(f'👤 user 필터: {notify_user} ({entries.count()}개 entry)')
        else:
            self.stdout.write(f'👥 user 필터 없음 (전체 {entries.count()}개 entry 카운트)')
        has_diary = entries.filter(mode='diary').exists()
        has_opic = entries.filter(mode='opic').exists()

        # tunnel URL 파일이 있으면 우선
        tunnel_file = Path(django_settings.BASE_DIR) / 'data' / 'tunnel_url.txt'
        if tunnel_file.exists():
            url = tunnel_file.read_text().strip()
            if url:
                s['site_url'] = url
                self.stdout.write(f'🌐 tunnel URL 사용: {url}')

        site_url = (s.get('site_url') or 'http://localhost:8000').rstrip('/')
        webhook_url = (s.get('slack_webhook_url') or '').strip()
        mention_user_id = (s.get('slack_mention_user_id') or '').strip()

        if not webhook_url:
            self.stdout.write(self.style.ERROR(
                '⚠️  slack_webhook_url 설정 누락. admin 페이지에서 webhook URL을 먼저 등록하세요.'
            ))
            return

        # 둘 다 완료여도 알림은 계속 보냄 — 영어 끈을 놓지 않도록 리마인드.
        # 완료 상태면 격려·복습 톤(DONE_FLAVORS), 미완료면 독려 톤(FLAVORS).
        all_done = has_diary and has_opic

        user_label = resolve_user_label(notify_user)
        status_line = build_status_line(user_label, has_diary, has_opic)

        # 사용자별 feedback 표현 우선 픽을 위해 User 객체 resolve
        user_obj = None
        if notify_user:
            try:
                user_obj = User.objects.get(username=notify_user)
            except User.DoesNotExist:
                pass

        # title은 표현 1개로 자동 생성(push 미리보기에 표현 노출). 완료/미완료는 flavor·진척도로 전달.
        flavor = random.choice(DONE_FLAVORS if all_done else FLAVORS)
        title, message = build_notification(user=user_obj, status_line=status_line, flavor=flavor)

        if options['dry_run']:
            self.stdout.write('=== DRY RUN ===')
            masked = webhook_url[:36] + '…' if len(webhook_url) > 36 else webhook_url
            self.stdout.write(f'slack webhook: {masked}')
            self.stdout.write(f'mention id: {mention_user_id or "(없음)"}')
            self.stdout.write(f'click URL: {site_url}')
            self.stdout.write(f'title: {title}')
            self.stdout.write(f'message: {message}')
            return

        try:
            result = send_via_slack(
                webhook_url=webhook_url,
                title=title,
                message=message,
                click_url=site_url,
                mention_user_id=mention_user_id or None,
            )
            self.stdout.write(self.style.SUCCESS(
                f'✅ Slack 발송 완료 → {result["host"]}'
            ))
        except MailerError as e:
            self.stdout.write(self.style.ERROR(f'❌ 발송 실패: {e}'))
