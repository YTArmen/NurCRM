"""30.10.2026: подпись покупок Ed25519, открытые ключи, ротация и отзыв."""
import base64
import json
import os
import tempfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.main.tests_tz1029_purchases import mk
from apps.users.models import FeatureActivationKey


def write_ring(d, active, kids, revoked=()):
    for kid in kids:
        path = os.path.join(d, f"{kid}.pem")
        if not os.path.exists(path):
            pem = Ed25519PrivateKey.generate().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
            open(path, "wb").write(pem)
    json.dump({"active": active, "keys": [{"kid": k, "not_before": "2026-10-30", "not_after": None} for k in kids],
               "revoked_kids": list(revoked)}, open(os.path.join(d, "keys.json"), "w"))


def verify(body, public_b64):
    b = dict(body)
    sig = base64.b64decode(b.pop("signature"))
    raw = json.dumps(b, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64)).verify(sig, raw)  # бросает при ошибке
    return True


class Ed25519Tests(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ctx = override_settings(PURCHASES_KEYS_DIR=self.dir)
        self.ctx.enable()
        self.company, self.api = mk()

    def tearDown(self):
        self.ctx.disable()

    def keys(self):
        return APIClient().get("/api/main/purchases/keys/").json()

    def test_list_signed_and_verifiable(self):
        write_ring(self.dir, "k1", ["k1"])
        b = self.api.get("/api/main/purchases/").json()
        self.assertEqual((b["alg"], b["kid"]), ("Ed25519", "k1"))
        self.assertEqual(len(base64.b64decode(b["signature"])), 64)
        self.assertIn("signed_at", b)
        pub = self.keys()["keys"][0]["public_key"]
        self.assertEqual(len(base64.b64decode(pub)), 32)
        self.assertTrue(verify(b, pub))
        b["company"] = "00000000-0000-0000-0000-000000000000"
        with self.assertRaises(Exception):
            verify(b, pub)

    def test_activate_signed_including_activated(self):
        write_ring(self.dir, "k1", ["k1"])
        key = FeatureActivationKey.objects.create(code="ai", days=30)
        r = self.api.post("/api/main/purchases/activate/", {"key": key.key}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        b = r.json()
        self.assertEqual(b["activated"]["feature"], "ai")
        self.assertTrue(verify(b, self.keys()["keys"][0]["public_key"]))

    def test_rotation_and_revocation(self):
        write_ring(self.dir, "k1", ["k1", "k2"])
        ks = self.keys()
        self.assertEqual([(k["kid"], k["active"]) for k in ks["keys"]], [("k1", True), ("k2", False)])
        write_ring(self.dir, "k2", ["k1", "k2"], revoked=["k1"])
        ks = self.keys()
        self.assertEqual([k["kid"] for k in ks["keys"]], ["k2"])
        self.assertEqual(ks["revoked_kids"], ["k1"])
        b = self.api.get("/api/main/purchases/").json()
        self.assertEqual(b["kid"], "k2")
        self.assertTrue(verify(b, ks["keys"][0]["public_key"]))

    def test_no_keys_falls_back_to_hs256(self):
        b = self.api.get("/api/main/purchases/").json()
        self.assertEqual(b["alg"], "HS256")
        self.assertEqual(self.keys(), {"keys": [], "revoked_kids": []})
