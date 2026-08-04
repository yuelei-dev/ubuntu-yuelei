# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import pricing_config


class PricingConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("PRICING_DB")
        os.environ["PRICING_DB"] = str(Path(self.tmp.name) / "pricing.db")

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("PRICING_DB", None)
        else:
            os.environ["PRICING_DB"] = self.old_db
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

    def test_catalog_keys_are_unique_and_cover_runtime_overlay_models(self):
        keys = [item["key"] for item in pricing_config.CATALOG]
        self.assertEqual(len(keys), len(set(keys)))
        for required in ("grok_video.v1_5.1080p.per_sec", "sora.sora_2_pro.1080p.per_sec",
                         "banana.pro.hd", "breakdown.local_upload"):
            self.assertIn(required, keys)

    def test_stale_admin_page_cannot_overwrite_newer_price(self):
        first = self.save("audio", 21)
        self.save("audio", 22, version=first["version"])
        with self.assertRaises(pricing_config.PricingConflict):
            self.save("audio", 23, version=first["version"])
        self.assertEqual(pricing_config.get_price("audio"), 22)

    def test_reset_restores_code_default_and_keeps_audit(self):
        changed = self.save("search", 9)
        reset = self.save("search", 0, version=changed["version"], action="reset")
        self.assertEqual(reset["version"], 0)
        self.assertEqual(pricing_config.get_price("search"), 1)
        audit = pricing_config.admin_catalog()["audit"]
        self.assertEqual([row["action"] for row in audit[:2]], ["reset", "set"])
        self.assertEqual(audit[0]["before_points"], 9)
        self.assertEqual(audit[0]["after_points"], 1)

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
        self.assertEqual(self.points.cost_of("xiaole_video", {"duration": 3}), 96)
        self.assertEqual(self.points.cost_of("tryon", {"clothes_data": "x"}), 27)
        self.assertEqual(self.points.cost_of("tryon", {"clothes_data": "x", "background_data": "y"}), 45)

    def test_batch_refund_uses_frozen_job_price_after_admin_change(self):
        self.save("breakdown.per_link", 25)
        charged = self.points.cost_of("breakdown", {"urls": ["a", "b", "c", "d"]})
        changed = next(x for x in pricing_config.admin_catalog()["items"] if x["key"] == "breakdown.per_link")
        self.save("breakdown.per_link", 40, version=changed["version"])
        self.assertEqual(charged, 100)
        self.assertEqual(self.points.breakdown_batch_refund(charged, 4, 2), 50)


if __name__ == "__main__":
    unittest.main()
