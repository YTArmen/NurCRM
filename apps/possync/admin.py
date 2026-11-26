from django.contrib import admin

from .models import StockAdjustment, StockAdjustmentLine


class StockAdjustmentLineInline(admin.TabularInline):
    model = StockAdjustmentLine
    extra = 0
    readonly_fields = ("product", "variant", "delta", "before", "after", "purchase_price")
    can_delete = False


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(admin.ModelAdmin):
    list_display = ("number", "company", "kind", "source", "created_by", "created_at")
    list_filter = ("kind", "source")
    search_fields = ("seq", "reason", "idempotency_key")
    readonly_fields = ("seq", "idempotency_key", "created_at")
    inlines = [StockAdjustmentLineInline]
