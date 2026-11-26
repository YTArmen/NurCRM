"""Инспекторы drf-yasg: чтобы схема OpenAPI (swagger/?format=openapi) генерировалась.

ТЗ 31.10 п.15: в проекте много сериализаторов с одинаковыми именами в разных
приложениях (например PayoutSerializer в barber и main). drf-yasg требует
уникальный ref_name и падает с SwaggerGenerationError. Здесь при коллизии имени
к ref_name добавляется префикс приложения.
"""
from drf_yasg.inspectors import ReferencingSerializerInspector
from drf_yasg.utils import get_serializer_ref_name


class UniqueRefNameSerializerInspector(ReferencingSerializerInspector):
    # базовое имя -> класс, который его занял первым
    _owners = {}

    def get_serializer_ref_name(self, serializer):
        ref_name = get_serializer_ref_name(serializer)
        if not ref_name:
            return ref_name
        meta = getattr(serializer, 'Meta', None)
        if hasattr(meta, 'ref_name'):
            return ref_name  # явное имя не трогаем
        cls = type(serializer)
        owner = self._owners.setdefault(ref_name, cls)
        if owner is cls:
            return ref_name
        parts = cls.__module__.split('.')
        prefix = '_'.join(p for p in parts if p not in ('apps', 'serializers', 'api'))
        return f'{prefix}_{ref_name}'


def use_unique_ref_names():
    """Ставит инспектор вместо стандартного ReferencingSerializerInspector (вызывается из core/urls.py)."""
    from drf_yasg.inspectors import ReferencingSerializerInspector as _Base, ViewInspector

    ViewInspector.field_inspectors = [
        UniqueRefNameSerializerInspector if i is _Base else i for i in ViewInspector.field_inspectors
    ]
