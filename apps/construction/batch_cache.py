"""
ТЗ 31.10 п.5: кэш на одну пачку операций (оплата долга сразу по многим взносам).
Внутри `with batch_cache():` одинаковые поиски (касса, статья, получатели уведомлений) делаются один раз.
Вне блока memo() просто вызывает функцию — поведение как раньше.
"""
import contextvars
from contextlib import contextmanager

_cache = contextvars.ContextVar("nur_batch_cache", default=None)


@contextmanager
def batch_cache():
    token = _cache.set({})
    try:
        yield
    finally:
        _cache.reset(token)


def memo(key, fn):
    c = _cache.get()
    if c is None:
        return fn()
    if key not in c:
        c[key] = fn()
    return c[key]


def ref(v):
    """Ключ из значения: у моделей — pk."""
    return str(getattr(v, "pk", v))
