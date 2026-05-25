"""Seed Expression model from data/expressions.json (curated from Notion OPIC DB).

Usage:
    python manage.py seed_expressions          # upsert from file
    python manage.py seed_expressions --wipe   # wipe table first
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from api.models import Expression


class Command(BaseCommand):
    help = 'Seed Expression rows from data/expressions.json'

    def add_arguments(self, parser):
        parser.add_argument('--wipe', action='store_true', help='Delete all existing Expression rows first')
        parser.add_argument('--path', default=None, help='Override JSON path')

    def handle(self, *args, **opts):
        path = Path(opts['path'] or (Path(settings.BASE_DIR) / 'data' / 'expressions.json'))
        if not path.is_file():
            self.stderr.write(self.style.ERROR(f'Seed file not found: {path}'))
            return

        if opts['wipe']:
            n = Expression.objects.all().count()
            Expression.objects.all().delete()
            self.stdout.write(f'Wiped {n} existing expressions.')

        data = json.loads(path.read_text(encoding='utf-8'))
        created, updated = 0, 0
        for row in data:
            en = (row.get('en') or '').strip()
            if not en:
                continue
            obj, was_created = Expression.objects.update_or_create(
                en=en,
                defaults={
                    'ko':       (row.get('ko') or '').strip(),
                    'example':  (row.get('example') or '').strip(),
                    'tip':      (row.get('tip') or '').strip(),
                    'category': (row.get('category') or '').strip(),
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1
        self.stdout.write(self.style.SUCCESS(f'Seeded expressions — created: {created}, updated: {updated}'))
