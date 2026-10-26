from django.conf import settings
from django.db import models


class GlobalProductPhoto(models.Model):
    """
    Фото товара глобального каталога (main.GlobalProduct).

    Файлы НЕ копируются (на сервере мало места):
      * source=store — ссылка на уже существующий файл фото товара компании
        (image_name = путь относительно MEDIA_ROOT, как в main_productimage.image);
      * source=off   — ссылка (url) на фото в Open Food Facts (CC BY-SA), показывать «Фото: Open Food Facts».
    Строка без image_name и без url — «промах»: проверили, фото нет (checked_at — когда).
    """

    SOURCE_STORE = "store"
    SOURCE_OFF = "off"
    SOURCE_CHOICES = [
        (SOURCE_STORE, "Фото товара из магазина"),
        (SOURCE_OFF, "Open Food Facts"),
    ]

    product = models.OneToOneField(
        "main.GlobalProduct", on_delete=models.CASCADE, related_name="photo",
        verbose_name="Глобальный товар",
    )
    source = models.CharField("Источник", max_length=8, choices=SOURCE_CHOICES)
    image_name = models.CharField("Файл (путь в media)", max_length=500, blank=True, default="")
    url = models.URLField("Ссылка на фото", max_length=500, blank=True, default="")
    source_product_id = models.UUIDField("Товар-источник", null=True, blank=True)
    checked_at = models.DateTimeField("Проверено", auto_now=True, db_index=True)

    class Meta:
        verbose_name = "Фото глобального товара"
        verbose_name_plural = "Фото глобальных товаров"

    def __str__(self):
        return f"{self.product_id} [{self.source}] {self.image_name or self.url or '—'}"

    @property
    def has_photo(self) -> bool:
        return bool(self.image_name or self.url)

    def build_url(self, request=None):
        """Абсолютная ссылка на фото или None."""
        if self.source == self.SOURCE_STORE and self.image_name:
            from django.core.files.storage import default_storage

            try:
                if not default_storage.exists(self.image_name):
                    return None  # файл удалили у компании-источника
            except Exception:
                return None
            path = "/" + settings.MEDIA_URL.lstrip("/") + self.image_name.lstrip("/")
            return request.build_absolute_uri(path) if request is not None else path
        if self.source == self.SOURCE_OFF and self.url:
            return self.url
        return None
