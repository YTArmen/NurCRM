from django.apps import AppConfig


class RepairConfig(AppConfig):
    """П2-1 (15.11.2026, p2115): «Ремонт и сервис» и «Trade-in / скупка»."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.repair"
    verbose_name = "Ремонт и trade-in"

    def ready(self):
        from apps.repair import checkout_hook

        checkout_hook.install()
