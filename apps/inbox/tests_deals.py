"""Б-82: воронка магазина."""
from apps.inbox.models import Conversation, DealExt
from apps.inbox.tests import BASE, InboxBase
from apps.inbox.tests_core import Mixin
from apps.main.models import Client, Deal


def cards(n=50):
    return [{"id": f"c{i}", "name": f"Клиент {i}", "phone": "0700111222" if i == 0 else "", "amount": 1000 + i,
             "note": "заметка", "stage": ["new", "work", "agreed", "paid", "lost"][i % 5], "source": "telegram",
             "instagram": "", "telegram_username": f"u{i}", "chat_id": str(1000 + i),
             "created_at": "2026-09-01T10:00:00+06:00", "updated_at": "2026-09-02T10:00:00+06:00"} for i in range(n)]


class DealTests(Mixin, InboxBase):
    def test_import_50_twice_gives_50(self):
        cl = Client.objects.create(company=self.company, full_name="Асан", phone="0700111222")
        r1 = self.api.post(f"{BASE}/deals/import/", cards(), format="json")
        self.assertEqual(r1.status_code, 200, r1.content)
        self.assertEqual((r1.json()["created"], r1.json()["updated"], r1.json()["errors"]), (50, 0, []))
        r2 = self.api.post(f"{BASE}/deals/import/", {"cards": cards()}, format="json")
        self.assertEqual((r2.json()["created"], r2.json()["updated"]), (0, 50))
        self.assertEqual(Deal.objects.count(), 50)
        self.assertEqual(DealExt.objects.count(), 50)
        first = DealExt.objects.get(external_id="c0")
        self.assertEqual(first.client_id, cl.id)  # по телефону
        self.assertEqual(first.deal.created_at.year, 2026)
        self.assertEqual(first.deal.created_at.month, 9)

    def test_bad_cards_reported(self):
        r = self.api.post(f"{BASE}/deals/import/", [{"id": "x", "name": "A", "stage": "weird"}, {"name": "no id"}], format="json")
        self.assertEqual(r.json()["created"], 0)
        self.assertEqual(len(r.json()["errors"]), 2)

    def test_list_filter_move_and_permissions(self):
        self.api.post(f"{BASE}/deals/import/", cards(10), format="json")
        lst = self.api.get(f"{BASE}/deals/?stage=paid").json()
        self.assertEqual(lst["count"], 2)
        deal_id = lst["results"][0]["id"]
        r = self.api.post(f"{BASE}/deals/{deal_id}/move/", {"stage": "lost"}, format="json")
        self.assertEqual((r.json()["stage"], r.json()["status"]), ("lost", "lost"))
        self.assertEqual(self.api.post(f"{BASE}/deals/{deal_id}/move/", {"stage": "zzz"}, format="json").status_code, 400)
        u = self.mk_user(can_view_clients=False)
        self.assertEqual(self.client_for(u).get(f"{BASE}/deals/").status_code, 403)

    def test_deal_from_conversation(self):
        ch = self.mk_channel()
        self.inbound(ch, chat="777", text="Хочу оптом")
        conv = Conversation.objects.get()
        r = self.api.post(f"{BASE}/conversations/{conv.id}/deal/", {"amount": 5000}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["source"], r.json()["conversation"]), ("telegram", conv.id))
        conv.refresh_from_db()
        self.assertEqual(str(conv.deal_id), r.json()["id"])
        self.assertEqual(self.api.get(f"{BASE}/conversations/{conv.id}/").json()["deal"], r.json()["id"])
        self.assertEqual(self.api.post(f"{BASE}/conversations/{conv.id}/deal/", {}, format="json").status_code, 409)
        self.assertEqual(self.api.get(f"{BASE}/deals/?conversation={conv.id}").json()["count"], 1)
