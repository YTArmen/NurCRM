# Тесты куска pos_print: python manage.py test apps.main.tz26.pos_print --settings=core.settings_test_sqlite
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.main.models import HeldCart, PrintJob, ScannerEvent, ScannerSession, Sale
from apps.main.tz26.pos_print import services
from apps.possync.tests import PossyncBase
from apps.users.models import Company

User = get_user_model()
DEV_A = "device-aaaaaaaa"
DEV_B = "device-bbbbbbbb"
API = "/api/main/pos/"


class PrintBase(PossyncBase):
    def checkout(self, key=None, **extra):
        body = {"items": [{"product": str(self.product.id), "qty": "1", "price": "100.00"}],
                "payment": {"method": "cash", "received": "1000.00"}}
        body.update(extra)
        return self.api.post(API + "checkout/", body, format="json",
                             HTTP_IDEMPOTENCY_KEY=key or f"k-{uuid.uuid4().hex}")

    def sale(self):
        self.open_shift()
        r = self.checkout()
        self.assertEqual(r.status_code, 201, r.data)
        return Sale.objects.get(pk=r.data["id"])

    def other_api(self):
        other = User.objects.create_user(email=f"x{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        co = Company.objects.create(name="Other", owner=other, is_active=True, subscription_plan=self.plan)
        other.company = co
        other.save()
        api = APIClient()
        api.force_authenticate(other)
        return api


class PrintJobTests(PrintBase):
    def test_checkout_creates_job_once(self):
        self.open_shift()
        key = "pj-" + uuid.uuid4().hex
        r = self.checkout(key=key, print_on={"cashbox": str(self.cashbox.id)})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(PrintJob.objects.count(), 1)
        job = PrintJob.objects.get()
        self.assertEqual(job.status, "pending")
        self.assertEqual(str(job.sale_id), r.data["id"])
        self.assertEqual(job.target_cashbox_id, self.cashbox.id)
        r2 = self.checkout(key=key, print_on={"cashbox": str(self.cashbox.id)})
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.data.get("replayed"))
        self.assertEqual(PrintJob.objects.count(), 1)

    def test_checkout_without_print_on_or_bad_print_on(self):
        self.open_shift()
        self.assertEqual(self.checkout().status_code, 201)
        self.assertEqual(self.checkout(print_on={"device_key": "x"}).status_code, 201)  # плохой ключ: продажа цела
        self.assertEqual(self.checkout(print_on={"cashbox": str(uuid.uuid4())}).status_code, 201)
        self.assertEqual(PrintJob.objects.count(), 0)

    def test_create_with_key_and_list_for_device(self):
        sale = self.sale()
        body = {"kind": "sale_receipt", "sale": str(sale.id), "target_device_key": DEV_A, "copies": 2}
        r = self.api.post(API + "print-jobs/", body, format="json", HTTP_IDEMPOTENCY_KEY="a" * 20)
        self.assertEqual(r.status_code, 201, r.data)
        self.api.post(API + "print-jobs/", body, format="json", HTTP_IDEMPOTENCY_KEY="a" * 20)
        self.assertEqual(PrintJob.objects.count(), 1)
        self.assertEqual(self.api.post(API + "print-jobs/", body, format="json").data["code"],
                         "idempotency_key_required")
        r = self.api.get(API + f"print-jobs/?device_key={DEV_A}&status=pending")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)
        row = r.data["results"][0]
        self.assertEqual(row["sale"]["id"], str(sale.id))
        self.assertEqual(row["copies"], 2)
        r = self.api.get(API + f"print-jobs/?device_key={DEV_B}&status=pending")
        self.assertEqual(r.data["count"], 0)
        r = self.api.get(API + f"print-jobs/?device_key={DEV_A}&status=pending")
        r304 = self.api.get(API + f"print-jobs/?device_key={DEV_A}&status=pending", HTTP_IF_NONE_MATCH=r["ETag"])
        self.assertEqual(r304.status_code, 304)

    def test_cashbox_job_visible_to_device_of_cashbox(self):
        sale = self.sale()
        job = PrintJob.objects.create(company=self.company, sale=sale, target_cashbox=self.cashbox)
        with mock.patch("apps.main.tz26.pos_print.services.get_device_cashbox_id", return_value=self.cashbox.id):
            r = self.api.get(API + f"print-jobs/?device_key={DEV_A}&status=pending")
            self.assertEqual([x["id"] for x in r.data["results"]], [str(job.id)])
            r = self.api.post(API + f"print-jobs/{job.id}/claim/", {"device_key": DEV_A}, format="json")
            self.assertEqual(r.status_code, 200)
        job2 = PrintJob.objects.create(company=self.company, sale=sale, target_cashbox=self.cashbox)
        r = self.api.post(API + f"print-jobs/{job2.id}/claim/", {"device_key": DEV_B}, format="json")
        self.assertEqual(r.status_code, 403)  # без Device касса устройства неизвестна

    def test_claim_conflict_done_and_replay(self):
        sale = self.sale()
        job = PrintJob.objects.create(company=self.company, sale=sale, target_cashbox=self.cashbox)
        url = API + f"print-jobs/{job.id}/"
        with mock.patch("apps.main.tz26.pos_print.services.get_device_cashbox_id", return_value=self.cashbox.id):
            r = self.api.post(url + "claim/", {"device_key": DEV_A}, format="json")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data["status"], "claimed")
            self.assertEqual(r.data["sale"]["id"], str(sale.id))
            r = self.api.post(url + "claim/", {"device_key": DEV_B}, format="json")
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.data["code"], "already_claimed")
        for _ in range(2):
            r = self.api.post(url + "done/", {"device_key": DEV_A}, format="json")
            self.assertEqual((r.status_code, r.data["status"]), (200, "printed"))

    def test_lease_expiry_requeues_then_fails_after_three(self):
        sale = self.sale()
        job = PrintJob.objects.create(company=self.company, sale=sale, target_device_key=DEV_A)
        url = API + f"print-jobs/{job.id}/claim/"
        for n in (1, 2):
            self.assertEqual(self.api.post(url, {"device_key": DEV_A}, format="json").status_code, 200)
            PrintJob.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
            r = self.api.get(API + f"print-jobs/?device_key={DEV_A}&status=pending")
            self.assertEqual(r.data["results"][0]["attempts"], n)
        self.assertEqual(self.api.post(url, {"device_key": DEV_A}, format="json").status_code, 200)
        PrintJob.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        r = self.api.get(API + "print-jobs/?status=failed")
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["attempts"], 3)

    def test_fail_and_cancel(self):
        sale = self.sale()
        job = PrintJob.objects.create(company=self.company, sale=sale, target_device_key=DEV_A)
        r = self.api.post(API + f"print-jobs/{job.id}/fail/", {"error": "нет бумаги"}, format="json")
        self.assertEqual((r.data["status"], r.data["error"]), ("failed", "нет бумаги"))
        job2 = PrintJob.objects.create(company=self.company, sale=sale, target_device_key=DEV_A)
        r = self.api.post(API + f"print-jobs/{job2.id}/cancel/", {}, format="json")
        self.assertEqual(r.data["status"], "canceled")

    def test_other_company_cannot_touch(self):
        sale = self.sale()
        job = PrintJob.objects.create(company=self.company, sale=sale, target_device_key=DEV_A)
        api = self.other_api()
        r = api.post(API + f"print-jobs/{job.id}/claim/", {"device_key": DEV_A}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(api.get(API + f"print-jobs/?device_key={DEV_A}").data["count"], 0)

    def test_purge_old_jobs(self):
        sale = self.sale()
        old = PrintJob.objects.create(company=self.company, sale=sale, target_device_key=DEV_A)
        PrintJob.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=8))
        PrintJob.objects.create(company=self.company, sale=sale, target_device_key=DEV_A)
        self.assertEqual(services.purge_old_jobs(), 1)
        self.assertEqual(PrintJob.objects.count(), 1)

    def test_change_feed_event(self):
        sale = self.sale()
        with mock.patch("apps.main.change_feed.record_change") as rc:
            job = PrintJob.objects.create(company=self.company, sale=sale, target_device_key=DEV_A)
            services._emit_created(job, actor_id=self.owner.pk)
        self.assertEqual(rc.call_args[0][1], "print_job_created")
