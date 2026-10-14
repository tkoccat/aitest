import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db, logic  # noqa: E402
from app.livekit_token import make_token  # noqa: E402

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


class LogicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "t.db"))
        db.init_db(self.conn)
        self.ev = logic.create_event(self.conn, "Konser", "2026-10-16", "2026-10-18")
        self.p5 = logic.create_package(self.conn, self.ev, "5K", 5000, 2, "a\nb")
        self.p7 = logic.create_package(self.conn, self.ev, "7K", 7000, 1, "a\nb\nc", max_discount_pct=5)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_available_and_hold(self):
        self.assertEqual(logic.available_rooms(self.conn, self.p5, NOW), 2)
        h = logic.hold_rooms(self.conn, self.p5, 1, "Ali", "+90500", NOW)
        self.assertEqual(h["rooms"], 1)
        self.assertEqual(logic.available_rooms(self.conn, self.p5, NOW), 1)

    def test_cannot_oversell(self):
        logic.hold_rooms(self.conn, self.p5, 2, "Ali", "1", NOW)
        with self.assertRaises(logic.NotEnoughRooms):
            logic.hold_rooms(self.conn, self.p5, 1, "Veli", "2", NOW)
        # başarısız işlem kilit bırakmamalı
        self.assertEqual(logic.available_rooms(self.conn, self.p7, NOW), 1)
        logic.hold_rooms(self.conn, self.p7, 1, "Ayşe", "3", NOW)

    def test_hold_expires(self):
        logic.hold_rooms(self.conn, self.p5, 2, "Ali", "1", NOW, hours=24)
        later = NOW + timedelta(hours=25)
        self.assertEqual(logic.available_rooms(self.conn, self.p5, later), 2)

    def test_confirmed_hold_never_expires(self):
        h = logic.hold_rooms(self.conn, self.p5, 2, "Ali", "1", NOW, hours=1)
        logic.set_hold_status(self.conn, h["hold_id"], "confirmed")
        self.assertEqual(logic.available_rooms(self.conn, self.p5, NOW + timedelta(days=30)), 0)

    def test_release_frees_rooms(self):
        h = logic.hold_rooms(self.conn, self.p5, 2, "Ali", "1", NOW)
        logic.set_hold_status(self.conn, h["hold_id"], "released")
        self.assertEqual(logic.available_rooms(self.conn, self.p5, NOW), 2)

    def test_book_requested_with_optional_upgrade(self):
        r = logic.check_package(self.conn, self.p5, 1, NOW)
        self.assertEqual(r["recommendation"], "book_requested")
        self.assertEqual(r["optional_upgrade"]["package_id"], self.p7)

    def test_sold_out_offers_upgrade(self):
        logic.hold_rooms(self.conn, self.p5, 2, "Ali", "1", NOW)
        r = logic.check_package(self.conn, self.p5, 1, NOW)
        self.assertEqual(r["recommendation"], "offer_upgrade")
        self.assertEqual(r["alternatives"][0]["package_id"], self.p7)
        self.assertEqual(r["alternatives"][0]["price_usd"], 7000)

    def test_top_package_sold_out_offers_cheaper(self):
        logic.hold_rooms(self.conn, self.p7, 1, "Ali", "1", NOW)
        r = logic.check_package(self.conn, self.p7, 1, NOW)
        self.assertEqual(r["recommendation"], "offer_alternative")
        self.assertEqual(r["alternatives"][0]["package_id"], self.p5)

    def test_everything_sold_out(self):
        logic.hold_rooms(self.conn, self.p5, 2, "A", "1", NOW)
        logic.hold_rooms(self.conn, self.p7, 1, "B", "2", NOW)
        self.assertEqual(logic.check_package(self.conn, self.p5, 1, NOW)["recommendation"], "sold_out")

    def test_group_needs_more_rooms_than_available(self):
        r = logic.check_package(self.conn, self.p5, 2, NOW)
        self.assertEqual(r["recommendation"], "book_requested")
        # 2 oda isteyen için 7K'da yalnızca 1 oda var -> üst paket önerilmemeli
        self.assertIsNone(r["optional_upgrade"])

    def test_list_events_hides_past_and_inactive(self):
        past = logic.create_event(self.conn, "Eski", "2026-01-01", "2026-01-03")
        logic.create_package(self.conn, past, "X", 100, 1)
        names = [e["name"] for e in logic.list_events(self.conn, now=NOW)]
        self.assertEqual(names, ["Konser"])
        logic.update_package(self.conn, self.p7, active=0)
        ev = logic.list_events(self.conn, now=NOW)[0]
        self.assertEqual([p["name"] for p in ev["packages"]], ["5K"])

    def test_validation(self):
        with self.assertRaises(ValueError):
            logic.create_event(self.conn, "x", "2026-10-18", "2026-10-16")
        with self.assertRaises(ValueError):
            logic.create_package(self.conn, self.ev, "x", 0, 1)
        with self.assertRaises(KeyError):
            logic.create_package(self.conn, 9999, "x", 10, 1)

    def test_save_call_upsert_and_outcome_guard(self):
        logic.save_call(self.conn, "room-1", NOW, language="tr")
        c = logic.save_call(self.conn, "room-1", NOW, outcome="positive", summary="İlgilendi",
                            callback_time="Perşembe 15:00")
        self.assertEqual(c["outcome"], "positive")
        self.assertEqual(c["language"], "tr")
        c = logic.save_call(self.conn, "room-1", NOW, outcome="bilinmeyen")
        self.assertEqual(c["outcome"], "info_only")
        self.assertEqual(len(logic.list_calls(self.conn)), 1)

    def test_seed_demo_is_idempotent(self):
        conn2 = db.connect(os.path.join(self.tmp.name, "seed.db"))
        db.init_db(conn2)
        self.assertTrue(logic.seed_demo(conn2, date(2026, 10, 14)))
        self.assertFalse(logic.seed_demo(conn2, date(2026, 10, 14)))
        evs = logic.list_events(conn2, now=NOW)
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0]["check_in"], "2026-10-16")  # 14 Ekim 2026 Çarşamba -> Cuma 16 Ekim
        self.assertEqual(evs[0]["check_out"], "2026-10-18")
        self.assertEqual([p["price_usd"] for p in evs[0]["packages"]], [5000, 7000])
        conn2.close()

    def test_seed_casino_once_and_deactivates_others(self):
        self.assertTrue(logic.seed_casino(self.conn, date(2026, 10, 14)))
        self.assertFalse(logic.seed_casino(self.conn, date(2026, 10, 14)))
        evs = logic.list_events(self.conn, now=NOW)
        self.assertEqual([e["name"] for e in evs], ["VIP Casino Daveti (Demo)"])
        self.assertEqual([p["price_usd"] for p in evs[0]["packages"]], [5000, 10000])
        # hafta içi dolunca hafta sonu önerilir
        wk = evs[0]["packages"][0]["package_id"]
        logic.hold_rooms(self.conn, wk, 20, "A", "1", now=NOW)
        self.assertEqual(logic.check_package(self.conn, wk, 1, now=NOW)["recommendation"], "offer_upgrade")
        st = logic.get_settings(self.conn)
        self.assertIn("SORUMLU OYUN", st["scenario"])
        self.assertTrue(st["greeting"])

    def test_settings_roundtrip_and_event_toggle(self):
        logic.set_settings(self.conn, agency_name=" Firma ", scenario="X", unknown="y")
        st = logic.get_settings(self.conn)
        self.assertEqual((st["agency_name"], st["scenario"], st["greeting"]), ("Firma", "X", ""))
        self.assertNotIn("unknown", st)
        logic.set_event_active(self.conn, self.ev, False)
        self.assertEqual(logic.list_events(self.conn, now=NOW), [])
        with self.assertRaises(KeyError):
            logic.set_event_active(self.conn, 999, True)


class TokenTests(unittest.TestCase):
    def test_jwt_is_valid_hs256(self):
        tok = make_token("key", "secret", "guest-1", "call-abc", now=1_000_000)
        h, p, s = tok.split(".")
        pad = lambda x: x + "=" * (-len(x) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad(p)))
        self.assertEqual(claims["iss"], "key")
        self.assertEqual(claims["video"]["room"], "call-abc")
        self.assertEqual(claims["exp"], 1_000_000 + 900)
        expected = hmac.new(b"secret", f"{h}.{p}".encode(), hashlib.sha256).digest()
        self.assertEqual(base64.urlsafe_b64decode(pad(s)), expected)


if __name__ == "__main__":
    unittest.main()
