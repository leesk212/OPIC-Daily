#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'opic_daily.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django를 import할 수 없어요. venv가 활성화됐는지 확인하세요. "
            "pip install -r requirements.txt 먼저 실행하세요."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
