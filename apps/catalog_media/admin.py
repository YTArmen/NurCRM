from django.contrib import admin

from .models import GlobalProductPhoto


@admin.register(GlobalProductPhoto)
class GlobalProductPhotoAdmin(admin.ModelAdmin):
    list_display = ("product", "source", "image_name", "url", "checked_at")
    list_filter = ("source",)
    search_fields = ("product__barcode", "product__name")
    raw_id_fields = ("product",)
