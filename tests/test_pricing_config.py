# -*- coding: utf-8 -*-
from contextlib import closing
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import pricing_config


class PricingConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("PRICING_DB")
        self.old_timeout = os.environ.get("PRICING_DB_TIMEOUT")
        os.environ["PRICING_DB"] = str(Path(self.tmp.name) / "pricing.db")
        os.environ["PRICING_DB_TIMEOUT"] = "0.05"
        pricing_config._clear_cache_for_tests()

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("PRICING_DB", None)
        else:
            os.environ["PRICING_DB"] = self.old_db
        if self.old_timeout is None:
            os.environ.pop("PRICING_DB_TIMEOUT", None)
        else:
            os.environ["PRICING_DB_TIMEOUT"] = self.old_timeout
        pricing_config._clear_cache_for_tests()
        self.tmp.cleanup()

    def save(self, key, points, version=0, action="set"):
        return pricing_config.save("ops-admin", {
            "key": key, "points": points, "version": version,
            "action": action, "reason": "测试调整",
        })

    def test_defaults_and_override_are_immediately_visible(self):
        self.assertEqual(pricing_config.get_price("copy"), 3)
        changed = self.save("copy", 17)
        self.assertEqual(pricing_config.get_price("copy"), 17)
        self.assertGreater(changed["version"], 0)
        item = next(x for x in pricing_config.admin_catalog()["items"] if x["key"] == "copy")
        self.assertTrue(item["configured"])
        self.assertEqual(item["points"], 17)
        self.assertEqual(item["updated_by"], "ops-admin")

    def test_pre_revision_schema_migrates_without_losing_override(self):
        with closing(sqlite3.connect(os.environ["PRICING_DB"])) as conn:
            conn.execute("""CREATE TABLE admin_pricing_config(
                pricing_key TEXT PRIMARY KEY,
                points INTEGER NOT NULL CHECK(points > 0),
                updated_by TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )""")
            conn.execute(
                "INSERT INTO admin_pricing_config VALUES(?,?,?,?)",
                ("copy", 17, "old-admin", 123),
            )
            conn.commit()
        pricing_config._clear_cache_for_tests()
        self.assertEqual(pricing_config.get_price("copy"), 17)
        item = next(x for x in pricing_config.admin_catalog()["items"] if x["key"] == "copy")
        self.assertEqual(item["version"], 123)
        changed = self.save("copy", 18, version=item["version"])
        self.assertGreater(changed["version"], item["version"])

    def test_catalog_keys_are_unique_and_only_expose_accepted_runtime_models(self):
        keys = [item["key"] for item in pricing_config.CATALOG]
        self.assertEqual(len(keys), len(set(keys)))
        for required in ("grok_video.v1_5.1080p.per_sec", "banana.pro.hd",
                         "breakdown.local_upload"):
            self.assertIn(required, keys)
        self.assertFalse(any(key.startswith("sora.") for key in keys))

    def test_stale_admin_page_cannot_overwrite_newer_price(self):
        first = self.save("audio", 21)
        self.save("audio", 22, version=first["version"])
        with self.assertRaises(pricing_config.PricingConflict):
            self.save("audio", 23, version=first["version"])
        self.assertEqual(pricing_config.get_price("audio"), 22)

    def test_revisions_are_strictly_monotonic_with_fixed_clock(self):
        with mock.patch.object(pricing_config.time, "time", return_value=1234.5):
            first = self.save("audio", 21)
            second = self.save("audio", 22, version=first["version"])
            third = self.save("audio", 23, version=second["version"])
        self.assertLess(first["version"], second["version"])
        self.assertLess(second["version"], third["version"])
        self.assertEqual(first["updated_at"], second["updated_at"])
        with self.assertRaises(pricing_config.PricingConflict):
            self.save("audio", 24, version=first["version"])

    def test_concurrent_same_version_allows_exactly_one_writer(self):
        first = self.save("copy", 11)
        barrier = threading.Barrier(2)
        saved, rejected = [], []

        def writer(points):
            barrier.wait()
            try:
                saved.append(self.save("copy", points, version=first["version"]))
            except pricing_config.PricingConflict as exc:
                rejected.append(exc)

        threads = [threading.Thread(target=writer, args=(points,)) for points in (12, 13)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(saved), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(pricing_config.get_price("copy"), saved[0]["points"])

    def test_reset_restores_code_default_and_keeps_audit(self):
        changed = self.save("search", 9)
        reset = self.save("search", 0, version=changed["version"], action="reset")
        self.assertGreater(reset["version"], changed["version"])
        self.assertEqual(pricing_config.get_price("search"), 1)
        item = next(x for x in pricing_config.admin_catalog()["items"] if x["key"] == "search")
        self.assertFalse(item["configured"])
        self.assertEqual(item["version"], reset["version"])
        audit = pricing_config.admin_catalog()["audit"]
        self.assertEqual([row["action"] for row in audit[:2]], ["reset", "set"])
        self.assertEqual(audit[0]["before_points"], 9)
        self.assertEqual(audit[0]["after_points"], 1)

    def test_reset_tombstone_blocks_pre_change_page_aba_overwrite(self):
        changed = self.save("search", 9, version=0)
        reset = self.save("search", 0, version=changed["version"], action="reset")
        with self.assertRaises(pricing_config.PricingConflict):
            self.save("search", 7, version=0)
        current = self.save("search", 7, version=reset["version"])
        self.assertGreater(current["version"], reset["version"])

    def test_free_fractional_unknown_and_missing_reason_are_rejected(self):
        for points in (0, -1, 1.5, "2.5", 100001, True):
            with self.subTest(points=points), self.assertRaises(ValueError):
                self.save("copy", points)
        with self.assertRaises(ValueError):
            pricing_config.save("ops-admin", {"key": "no-such-price", "points": 2,
                                               "version": 0, "reason": "测试"})
        with self.assertRaises(ValueError):
            pricing_config.save("ops-admin", {"key": "copy", "points": 2,
                                               "version": 0, "reason": ""})

    def test_public_catalog_exposes_prices_without_audit_metadata(self):
        self.save("leads", 44)
        public = pricing_config.public_catalog()
        self.assertEqual(public["values"]["leads"], 44)
        self.assertNotIn("actor", str(public))
        self.assertNotIn("reason", str(public))

    def test_read_failure_uses_trusted_last_known_price_but_cold_start_fails_closed(self):
        self.save("copy", 17)
        with mock.patch.object(pricing_config, "_connect", side_effect=OSError("unreadable")):
            self.assertEqual(pricing_config.get_price("copy"), 17)
            pricing_config._clear_cache_for_tests()
            with self.assertRaises(pricing_config.PricingUnavailable):
                pricing_config.get_price("copy")

    def test_locked_database_uses_last_known_price_and_never_code_default(self):
        self.save("copy", 17)
        lock = sqlite3.connect(os.environ["PRICING_DB"], timeout=0.05)
        try:
            lock.execute("BEGIN EXCLUSIVE")
            self.assertEqual(pricing_config.get_price("copy"), 17)
            pricing_config._clear_cache_for_tests()
            with self.assertRaises(pricing_config.PricingUnavailable):
                pricing_config.get_price("copy")
        finally:
            lock.rollback()
            lock.close()

    def test_corrupt_database_uses_last_known_price_and_cold_start_fails_closed(self):
        self.save("copy", 17)
        Path(os.environ["PRICING_DB"]).write_bytes(b"not-a-sqlite-database")
        self.assertEqual(pricing_config.get_price("copy"), 17)
        pricing_config._clear_cache_for_tests()
        with self.assertRaises(pricing_config.PricingUnavailable):
            pricing_config.get_price("copy")


class RuntimePricingIntegrationTests(PricingConfigTests):
    @classmethod
    def setUpClass(cls):
        import imggen_api
        import leadgen_api
        from content_domains import audio, local_reverse_upload, points, video
        cls.imggen = imggen_api
        cls.leadgen = leadgen_api
        cls.audio = audio
        cls.local_reverse = local_reverse_upload
        cls.points = points
        cls.video = video

    def test_content_and_leadgen_use_same_live_business_prices(self):
        self.save("collect.main", 41)
        self.save("collect.transcript", 7)
        self.save("leads", 53)
        self.assertEqual(self.points.cost_of("collect", {"want": ["comments"]}), 41)
        self.assertEqual(self.leadgen.cost_of("collect", {"want": ["video"]}), 41)
        self.assertEqual(self.points.cost_of("collect", {"want": ["transcript"]}), 7)
        self.assertEqual(self.leadgen.cost_of("collect", {"want": ["transcript"]}), 7)
        self.assertEqual(self.points.cost_of("leads", {}), 53)
        self.assertEqual(self.leadgen.cost_of("leads", {"count": 30, "pages": 3}), 53)

    def test_image_banana_reverse_and_voice_slot_use_live_prices(self):
        self.save("image.openai.hd", 37)
        self.save("banana.pro.hd", 46)
        self.save("image.reverse", 8)
        self.save("voice_slot", 66)
        self.assertEqual(self.points.cost_of("image", {"provider": "openai", "quality": "hd", "count": 2}), 74)
        self.assertEqual(self.imggen.banana_cost("pro", "hd", 2), 92)
        self.assertEqual(self.imggen.reverse_cost(), 8)
        self.assertEqual(self.audio.voice_slot_cost(), 66)

    def test_video_composed_prices_change_without_restart(self):
        self.save("talking.per_sec", 13)
        self.save("cinematic.motion.per_sec", 34)
        self.save("xiaole_video.per_sec", 32)
        self.save("tryon.single", 27)
        self.save("tryon.combo", 45)
        self.assertEqual(self.video.video_cost({"text": "一二三四"}), 13)
        self.assertEqual(self.video.cinematic_rate("motion"), 34)
        self.assertEqual(self.points.cost_of("xiaole_video", {
            "channel": "micro", "duration": 3,
        }), 96)
        self.assertEqual(self.points.cost_of("tryon", {"clothes_data": "x"}), 27)
        self.assertEqual(self.points.cost_of("tryon", {"clothes_data": "x", "background_data": "y"}), 45)

    def test_each_grok_admin_price_reaches_real_acceptance_cost_entry(self):
        contracts = (
            ("grok_video.v1.480p.per_sec", "grok-imagine-video", "480p", 31),
            ("grok_video.v1.720p.per_sec", "grok-imagine-video", "720p", 32),
            ("grok_video.v1_5.480p.per_sec", "grok-imagine-video-1.5", "480p", 33),
            ("grok_video.v1_5.720p.per_sec", "grok-imagine-video-1.5", "720p", 34),
            ("grok_video.v1_5.1080p.per_sec", "grok-imagine-video-1.5", "1080p", 35),
        )
        for key, model, resolution, rate in contracts:
            with self.subTest(key=key):
                self.save(key, rate)
                cost = self.points.cost_of("xiaole_video", {
                    "channel": "grok", "model": model,
                    "resolution": resolution, "duration": 2,
                })
                self.assertEqual(cost, rate * 2)

    def test_unpriced_grok_combination_is_rejected_not_undercharged(self):
        with self.assertRaises(ValueError):
            self.points.cost_of("xiaole_video", {
                "channel": "grok", "model": "grok-imagine-video",
                "resolution": "1080p", "duration": 2,
            })

    def test_batch_refund_uses_frozen_job_price_after_admin_change(self):
        self.save("breakdown.per_link", 25)
        charged = self.points.cost_of("breakdown", {"urls": ["a", "b", "c", "d"]})
        changed = next(x for x in pricing_config.admin_catalog()["items"] if x["key"] == "breakdown.per_link")
        self.save("breakdown.per_link", 40, version=changed["version"])
        self.assertEqual(charged, 100)
        self.assertEqual(self.points.breakdown_batch_refund(charged, 4, 2), 50)


if __name__ == "__main__":
    unittest.main()
