from django.apps import AppConfig


class MainConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.main'

    def ready(self):
        import apps.main.signals
        import apps.main.change_feed  # ТЗ Н-31
