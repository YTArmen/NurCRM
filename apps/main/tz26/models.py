"""Модели всех кусков tz26 — импортируются в конце apps/main/models.py."""
from apps.main.tz26.hw_units.models import *  # noqa: F401,F403
from apps.main.tz26.hw_prices.models import *  # noqa: F401,F403
from apps.main.tz26.hw_catalog.models import *  # noqa: F401,F403
from apps.main.tz26.cust_orders.models import *  # noqa: F401,F403
from apps.main.tz26.staff.models import *  # noqa: F401,F403
from apps.main.tz26.ai_stock.models import *  # noqa: F401,F403
from apps.main.tz26.pos_sync.models import *  # noqa: F401,F403
from apps.main.tz26.pos_print.models import *  # noqa: F401,F403
from apps.main.tz26.hw_batches.models import *  # noqa: F401,F403
