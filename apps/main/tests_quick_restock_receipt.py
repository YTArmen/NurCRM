from decimal import Decimal
import uuid
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.main.models import Client, Product, SupplierReceipt, SupplierReceiptItem
from apps.users.models import Company, SubscriptionPlan

User = get_user_model()


class QuickRestockPurchaseBatchTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        email_owner = f"owner_{uuid.uuid4().hex[:6]}@test.com"
        self.owner = User.objects.create_user(email=email_owner, password="pass", role="owner")
        self.plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
        self.company = Company.objects.create(name="Test Co", owner=self.owner, subscription_plan=self.plan)
        self.owner.company = self.company
        self.owner.save()

        self.client.force_authenticate(user=self.owner)

        # Создаём поставщика
        self.supplier = Client.objects.create(
            full_name="ООО Альфа Снаб",
            type=Client.StatusClient.SUPPLIERS,
            company=self.company,
        )

        # Создаём тестовый товар
        self.product = Product.objects.create(
            name="Печенье Юбилейное",
            company=self.company,
            quantity=Decimal("10"),
            purchase_price=Decimal("40.00"),
            price=Decimal("60.00"),
            markup_percent=Decimal("50.00"),
        )

    def test_quick_restock_single_item_creates_batch_and_returns_id(self):
        """
        Сценарий 1:
        POST /api/main/suppliers/{supplier_id}/receipt/ с одним товаром.
        Проверяет:
        - Ответ содержит поля 'id' и 'receipt_id' верхнего уровня
        - Остаток товара увеличивается на переданный qty
        - Закупочная цена обновляется
        - Партия сохраняется в SupplierReceiptItem
        - История закупок товара (purchase_batches) возвращает эту партию
        - Товар связывается с поставщиком (client и suppliers)
        """
        url = f"/api/main/suppliers/{self.supplier.id}/receipt/"
        payload = {
            "items": [
                {
                    "product": str(self.product.id),
                    "qty": 100,
                    "purchase_price": "50.00",
                }
            ],
            "payment_type": "debt",
        }
        res = self.client.post(url, data=payload, format="json")
        self.assertEqual(res.status_code, 200, res.data)

        # 1. Проверка возврата id и receipt_id
        self.assertIn("id", res.data)
        self.assertIn("receipt_id", res.data)
        self.assertEqual(res.data["id"], res.data["receipt_id"])
        receipt_id = res.data["id"]

        # 2. Проверка остатка и закупочной цены товара
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("110"))  # 10 + 100
        self.assertEqual(self.product.purchase_price, Decimal("50.00"))
        # Цена продажи при оприходовании не меняется (раньше затиралась закупочной)
        self.assertEqual(self.product.price, Decimal("60.00"))
        self.assertEqual(self.product.markup_percent, Decimal("20.00"))  # (60-50)/50

        # 3. Проверка привязки поставщика
        self.assertEqual(self.product.client, self.supplier)
        self.assertTrue(self.product.suppliers.filter(id=self.supplier.id).exists())

        # 4. Проверка создания SupplierReceiptItem
        receipt_item = SupplierReceiptItem.objects.filter(receipt_id=receipt_id, product=self.product).first()
        self.assertIsNotNone(receipt_item)
        self.assertEqual(receipt_item.qty, 100)
        self.assertEqual(receipt_item.purchase_price, Decimal("50.00"))

        # 5. Проверка блока purchase_batches в детальном просмотре товара
        detail_res = self.client.get(f"/api/main/products/{self.product.id}/")
        self.assertEqual(detail_res.status_code, 200)
        batches = detail_res.data.get("purchase_batches", [])
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["receipt_id"], receipt_id)
        self.assertEqual(batches[0]["qty"], 100)
        self.assertEqual(Decimal(str(batches[0]["purchase_price"])), Decimal("50.00"))
        self.assertEqual(batches[0]["supplier_id"], str(self.supplier.id))
        self.assertEqual(batches[0]["supplier_name"], "ООО Альфа Снаб")

        # 6. Проверка эндпоинта /api/main/products/{id}/purchase-batches/
        batches_res = self.client.get(f"/api/main/products/{self.product.id}/purchase-batches/")
        self.assertEqual(batches_res.status_code, 200)
        batch_results = batches_res.data.get("results", [])
        self.assertEqual(len(batch_results), 1)
        self.assertEqual(batch_results[0]["receipt_id"], receipt_id)

    def test_quick_restock_followed_by_partial_patch_preserves_stock_and_cost(self):
        """
        Сценарий 2:
        После оприходования фронт сразу отправляет partial PATCH:
        PATCH /api/main/products/{productId}/ { "price": "<retailPrice>", "client": "<supplierId>" }
        Проверяет:
        - Розничная цена устанавливается в указанную (например 75.00)
        - Наценка пересчитывается от обновлённой закупочной цены
        - Остаток товара (quantity) НЕ сбрасывается и не затирается
        - Закупочная цена (purchase_price) НЕ затирается
        """
        # Сначала делаем приход 100 шт по 50.00
        receipt_url = f"/api/main/suppliers/{self.supplier.id}/receipt/"
        self.client.post(
            receipt_url,
            data={
                "items": [{"product": str(self.product.id), "qty": 100, "purchase_price": "50.00"}],
                "payment_type": "debt",
            },
            format="json",
        )

        # Затем фронт шлёт partial PATCH только с price и client
        patch_url = f"/api/main/products/{self.product.id}/"
        patch_res = self.client.patch(
            patch_url,
            data={"price": "75.00", "client": str(self.supplier.id)},
            format="json",
        )
        self.assertEqual(patch_res.status_code, 200, patch_res.data)

        self.product.refresh_from_db()
        # Остаток и закупка сохранены от прихода
        self.assertEqual(self.product.quantity, Decimal("110"))
        self.assertEqual(self.product.purchase_price, Decimal("50.00"))
        # Розничная цена равна переданной в PATCH
        self.assertEqual(self.product.price, Decimal("75.00"))
        # Наценка: (75 - 50) / 50 * 100 = 50%
        self.assertEqual(self.product.markup_percent, Decimal("50.00"))
        # Поставщик выставлен
        self.assertEqual(self.product.client, self.supplier)

    def test_quick_restock_with_direct_price_in_receipt_items(self):
        """
        Сценарий 3:
        Передача розничной цены непосредственно в строке прихода (items: [{..., "price": "90.00"}]).
        Позволяет фронту оприходовать и сразу установить розничную цену за 1 запрос.
        """
        url = f"/api/main/suppliers/{self.supplier.id}/receipt/"
        payload = {
            "items": [
                {
                    "product": str(self.product.id),
                    "qty": 20,
                    "purchase_price": "60.00",
                    "price": "90.00",
                }
            ],
            "payment_type": "debt",
        }
        res = self.client.post(url, data=payload, format="json")
        self.assertEqual(res.status_code, 200, res.data)

        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("30"))
        self.assertEqual(self.product.purchase_price, Decimal("60.00"))
        self.assertEqual(self.product.price, Decimal("90.00"))
        # Наценка: (90 - 60) / 60 * 100 = 50%
        self.assertEqual(self.product.markup_percent, Decimal("50.00"))

    def test_quick_restock_product_without_initial_supplier(self):
        """
        Сценарий 4:
        Товар изначально создан без поставщика (client=None, suppliers пусто).
        При оприходовании через поставщика он не должен падать с ошибкой
        "Товары не принадлежат выбранному поставщику", а должен успешно оприходоваться
        и связаться с выбранным поставщиком.
        """
        new_prod = Product.objects.create(
            name="Сок Яблочный",
            company=self.company,
            quantity=Decimal("0"),
            purchase_price=Decimal("0.00"),
            price=Decimal("100.00"),
        )
        self.assertIsNone(new_prod.client)
        self.assertEqual(new_prod.suppliers.count(), 0)

        url = f"/api/main/suppliers/{self.supplier.id}/receipt/"
        payload = {
            "items": [
                {
                    "product": str(new_prod.id),
                    "qty": 50,
                    "purchase_price": "70.00",
                }
            ],
            "payment_type": "debt",
        }
        res = self.client.post(url, data=payload, format="json")
        self.assertEqual(res.status_code, 200, res.data)

        new_prod.refresh_from_db()
        self.assertEqual(new_prod.quantity, Decimal("50"))
        self.assertEqual(new_prod.purchase_price, Decimal("70.00"))
        self.assertEqual(new_prod.client, self.supplier)
        self.assertTrue(new_prod.suppliers.filter(id=self.supplier.id).exists())

    def test_restock_without_supplier_via_patch_does_not_create_purchase_batch(self):
        """
        Сценарий 5:
        Добавление товара без поставщика — прямой PATCH на /api/main/products/{id}/.
        Остаток и цены обновляются, но в purchase_batches ничего не добавляется.
        """
        patch_url = f"/api/main/products/{self.product.id}/"
        patch_res = self.client.patch(
            patch_url,
            data={
                "quantity": 25,
                "purchase_price": "45.00",
                "price": "65.00",
            },
            format="json",
        )
        self.assertEqual(patch_res.status_code, 200, patch_res.data)

        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("25"))
        self.assertEqual(self.product.purchase_price, Decimal("45.00"))
        self.assertEqual(self.product.price, Decimal("65.00"))

        # Проверяем, что в истории закупок партий нет
        self.assertEqual(len(patch_res.data.get("purchase_batches", [])), 0)
        self.assertEqual(SupplierReceiptItem.objects.filter(product=self.product).count(), 0)
