from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'

    def ready(self):
        try:
            from . import scheduler
            scheduler.start()
        except Exception:
            import logging
            logging.getLogger(__name__).exception('notify scheduler 시작 실패')
