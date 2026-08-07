# -*- coding: utf-8 -*-
import importlib
import json
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

montage = importlib.import_module("content_domains.script_video_montage")


SHORT_COPY = "从清洁开始，让肌肤回到细腻透亮的状态。"
MEDIUM_COPY = (
    "真正高级的美，不是千篇一律的模板，而是看见自己的独特。"
    "从专业评估、温和护理到居家管理，每一步都尊重肌肤当下的需要。"
    "给自己一点时间，让改变自然发生，也让镜子里的你更从容、更自信。"
)


class ScriptVideoMontageTests(unittest.TestCase):
    def plan(self, **overrides):
        payload = {"copy": MEDIUM_COPY, "style": "luxe", "ratio": "16:9"}
        payload.update(overrides)
        return montage.plan_script_video(payload)

    def assert_valid_timeline(self, plan):
        scenes = plan["scenes"]
        self.assertEqual(len(scenes), plan["scene_count"])
        self.assertGreaterEqual(len(scenes), montage.MIN_SCENES)
        self.assertLessEqual(len(scenes), montage.MAX_SCENES)
        cursor = 0.0
        for expected_index, scene in enumerate(scenes, start=1):
            self.assertEqual(
                set(scene),
                {
                    "index", "start_seconds", "duration_seconds", "headline",
                    "supporting_copy", "image_prompt",
                },
            )
            self.assertEqual(scene["index"], expected_index)
            self.assertTrue(math.isclose(scene["start_seconds"], cursor, abs_tol=1e-9))
            self.assertGreater(scene["duration_seconds"], 0)
            self.assertTrue(scene["headline"])
            self.assertTrue(scene["supporting_copy"])
            self.assertTrue(scene["image_prompt"])
            cursor = round(cursor + scene["duration_seconds"], 1)
        self.assertTrue(math.isclose(cursor, plan["duration_seconds"], abs_tol=1e-9))

    def test_single_style_contract_and_timeline(self):
        plan = self.plan()
        self.assertEqual(plan["planner_version"], montage.PLANNER_VERSION)
        self.assertEqual(plan["style"], "luxe")
        self.assertEqual(plan["ratio"], "16:9")
        self.assertNotIn("styles", plan)
        self.assert_valid_timeline(plan)

    def test_aggregate_contract_preserves_requested_style_order(self):
        plan = montage.plan_script_video({
            "copy": MEDIUM_COPY,
            "styles": ["pop", "clinic", "luxe"],
            "ratio": "9:16",
        })
        self.assertEqual(plan["ratio"], "9:16")
        self.assertEqual([item["style"] for item in plan["styles"]], ["pop", "clinic", "luxe"])
        self.assertNotIn("style", plan)
        for variant in plan["styles"]:
            self.assert_valid_timeline({
                **variant, "duration_seconds": plan["duration_seconds"]
            })

    def test_copy_aliases_are_supported_and_copy_wins(self):
        script_plan = montage.plan_script_video({"script": MEDIUM_COPY, "style": "luxe"})
        text_plan = montage.plan_script_video({"text": MEDIUM_COPY, "style": "luxe"})
        preferred = montage.plan_script_video({
            "copy": SHORT_COPY,
            "script": MEDIUM_COPY,
            "style": "luxe",
        })
        self.assertEqual(script_plan["copy"], MEDIUM_COPY)
        self.assertEqual(text_plan["copy"], MEDIUM_COPY)
        self.assertEqual(preferred["copy"], SHORT_COPY)

    def test_duration_is_driven_by_copy_and_clamped(self):
        short = self.plan(copy=SHORT_COPY)["duration_seconds"]
        medium = self.plan(copy=MEDIUM_COPY * 2)["duration_seconds"]
        long_copy = ("专业护理让肌肤状态稳定而自然。" * 30)[:montage.MAX_COPY_CHARACTERS]
        long = self.plan(copy=long_copy)["duration_seconds"]
        self.assertEqual(short, montage.MIN_DURATION_SECONDS)
        self.assertGreater(medium, short)
        self.assertEqual(long, montage.MAX_DURATION_SECONDS)

    def test_style_pacing_changes_asset_count(self):
        plan = montage.plan_script_video({
            "copy": MEDIUM_COPY * 2,
            "styles": ["luxe", "pop", "clinic"],
            "ratio": "16:9",
        })
        counts = {item["style"]: item["scene_count"] for item in plan["styles"]}
        self.assertGreater(counts["pop"], counts["clinic"])
        self.assertGreater(counts["clinic"], counts["luxe"])

    def test_long_copy_never_exceeds_twenty_assets(self):
        copy = ("专业评估温和护理持续管理让美丽有据可循" * 30)[:montage.MAX_COPY_CHARACTERS]
        plan = self.plan(copy=copy, style="pop")
        self.assertEqual(plan["duration_seconds"], montage.MAX_DURATION_SECONDS)
        self.assertEqual(plan["scene_count"], montage.MAX_SCENES)
        self.assert_valid_timeline(plan)

    def test_image_prompts_are_unique_and_style_specific(self):
        plans = {
            style: self.plan(style=style) for style in ("luxe", "pop", "clinic")
        }
        for plan in plans.values():
            prompts = [scene["image_prompt"] for scene in plan["scenes"]]
            self.assertEqual(len(prompts), len(set(prompts)))
            self.assertTrue(all("不含文字" in prompt for prompt in prompts))
        self.assertIn("香槟金", plans["luxe"]["scenes"][0]["image_prompt"])
        self.assertIn("高饱和", plans["pop"]["scenes"][0]["image_prompt"])
        self.assertIn("医疗蓝", plans["clinic"]["scenes"][0]["image_prompt"])

    def test_planner_is_deterministic_and_does_not_mutate_input(self):
        payload = {"copy": MEDIUM_COPY, "styles": ["luxe", "pop"], "ratio": "16:9"}
        snapshot = json.loads(json.dumps(payload, ensure_ascii=False))
        first = montage.plan_script_video(payload)
        second = montage.plan_script_video(payload)
        self.assertEqual(first, second)
        self.assertEqual(payload, snapshot)

    def test_canonical_json_is_deterministic_and_script_safe(self):
        plan = self.plan(copy="护理前先了解肌肤<script>alert(1)</script>的真实状态。")
        first = montage.canonical_plan_json(plan)
        second = montage.canonical_plan_json(dict(reversed(list(plan.items()))))
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first), plan)
        self.assertNotIn("</script>", first.lower())
        with self.assertRaises(montage.MontagePlanError):
            montage.canonical_plan_json({"duration": float("nan")})

    def test_plan_digest_is_stable_and_ignores_digest_metadata(self):
        plan = self.plan()
        digest = montage.plan_digest(plan)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(digest, montage.plan_digest({**plan, "plan_digest": digest}))
        changed = json.loads(json.dumps(plan, ensure_ascii=False))
        changed["scenes"][0]["headline"] += "更新"
        self.assertNotEqual(digest, montage.plan_digest(changed))

    def test_invalid_payloads_are_rejected(self):
        invalid = [
            None,
            [],
            {"style": "luxe"},
            {"copy": 123, "style": "luxe"},
            {"copy": "beauty only text", "style": "luxe"},
            {"copy": "太短", "style": "luxe"},
            {"copy": MEDIUM_COPY, "style": "unknown"},
            {"copy": MEDIUM_COPY, "style": "luxe", "styles": ["pop"]},
            {"copy": MEDIUM_COPY, "styles": []},
            {"copy": MEDIUM_COPY, "styles": ["pop", "pop"]},
            {"copy": MEDIUM_COPY, "style": "luxe", "ratio": "4:3"},
            {"copy": MEDIUM_COPY, "style": "luxe", "unexpected": True},
            {"copy": "护理\x00方案更安心", "style": "luxe"},
            {"copy": "护理让状态更自然。" * 500, "style": "luxe"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(montage.MontagePlanError):
                    montage.plan_script_video(payload)


if __name__ == "__main__":
    unittest.main()
