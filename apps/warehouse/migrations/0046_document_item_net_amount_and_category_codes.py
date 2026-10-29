from decimal import Decimal

from django.db import migrations, models


def backfill_net_amount(apps, schema_editor):
    Document = apps.get_model("warehouse", "Document")
    DocumentItem = apps.get_model("warehouse", "DocumentItem")
    cent = Decimal("0.01")

    doc_ids = Document.objects.filter(items__isnull=False).values_list("id", flat=True).distinct()
    for doc_id in doc_ids.iterator(chunk_size=2000):
        doc = Document.objects.only("id", "discount_amount").get(pk=doc_id)
        items = list(DocumentItem.objects.filter(document_id=doc_id).only("id", "line_total", "net_amount"))
        lines = {i.pk: Decimal(i.line_total or 0) for i in items}
        subtotal = sum(lines.values(), Decimal("0.00"))
        doc_discount = Decimal(doc.discount_amount or 0)
        target = max(Decimal("0.00"), (subtotal - doc_discount).quantize(cent))
        if subtotal <= 0 or doc_discount <= 0:
            nets = {pk: v.quantize(cent) for pk, v in lines.items()}
        else:
            nets = {pk: (v - doc_discount * v / subtotal).quantize(cent) for pk, v in lines.items()}
        diff = target - sum(nets.values(), Decimal("0.00"))
        if diff != 0:
            biggest = max(lines, key=lambda pk: lines[pk])
            nets[biggest] = (nets[biggest] + diff).quantize(cent)
        for item in items:
            if item.net_amount != nets[item.pk]:
                DocumentItem.objects.filter(pk=item.pk).update(net_amount=nets[item.pk])


class Migration(migrations.Migration):

    dependencies = [
        ("warehouse", "0045_warehouseproductalternatebarcode_quantity"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentitem",
            name="net_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="line_total минус доля скидки документа; Σ по документу == Document.total.",
                max_digits=18,
                verbose_name="Чистая сумма строки",
            ),
        ),
        migrations.AlterField(
            model_name="paymentcategory",
            name="system_code",
            field=models.CharField(
                blank=True,
                choices=[
                    ("sale", "Продажа"),
                    ("debt", "Долги"),
                    ("incassation", "Инкассация"),
                    ("purchase", "Закупка"),
                    ("sale_return", "Возврат покупателю"),
                    ("purchase_return", "Возврат от поставщика"),
                ],
                help_text="Если задан — встроенная категория (нельзя удалить или переименовать через API).",
                max_length=16,
                null=True,
                verbose_name="Системный код",
            ),
        ),
        migrations.RunPython(backfill_net_amount, migrations.RunPython.noop),
    ]
