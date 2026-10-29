# Только для makemigrations/проверки цепочек: как settings_test_sqlite, но миграции включены
from core.settings_test_sqlite import *  # noqa
MIGRATION_MODULES = {}
