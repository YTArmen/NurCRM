"""ТЗ 31.10 п.15: рендерер схемы отдельно от инспекторов — иначе круговой импорт с drf_yasg.inspectors
(SWAGGER_SETTINGS грузит рендереры раньше инспекторов)."""
from drf_yasg.renderers import OpenAPIRenderer


class OpenAPIJsonRenderer(OpenAPIRenderer):
    """`?format=openapi` с заголовком Accept: application/json (иначе 406)."""
    media_type = 'application/json'
    format = 'openapi'
