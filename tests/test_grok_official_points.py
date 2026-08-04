import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import points, video


class XiaolePointsTests(unittest.TestCase):
    """Grok 按后台配置的型号/分辨率单价计费，非 Grok 渠道保留共享单价。"""

    def test_generation_uses_selected_model_resolution_rate(self):
        self.assertEqual(points.cost_of("xiaole_video", {"duration": 5}), 5 * 12)
        self.assertEqual(points.cost_of("xiaole_video", {
            "duration": 5, "model": "grok-imagine-video", "resolution": "480p",
        }), 5 * 10)
        self.assertEqual(points.cost_of("xiaole_video", {
            "duration": 5, "model": "grok-imagine-video-1.5", "resolution": "1080p",
        }), 5 * 44)

    def test_edit_uses_source_duration_ceil(self):
        # 编辑固定 Grok 1.0 / 720p，source_duration 向上取整后乘实时单价。
        self.assertEqual(points.cost_of("xiaole_video", {"operation": "edit", "source_duration": 8.7}), 9 * 12)
        self.assertEqual(points.cost_of("xiaole_video", {"operation": "edit", "source_duration": 3.0}), 3 * 12)

    def test_non_grok_channels_keep_shared_rate(self):
        for channel in ("micro", "omni"):
            self.assertEqual(points.cost_of("xiaole_video", {
                "channel": channel, "duration": 10,
            }), 10 * 30)

    def test_duration_is_capped_at_15(self):
        self.assertEqual(points.cost_of("xiaole_video", {"duration": 999}), 15 * 12)

    def test_default_duration_when_missing(self):
        self.assertEqual(points.cost_of("xiaole_video", {}), 10 * 12)

    def test_unpriced_model_resolution_fails_closed(self):
        with self.assertRaises(ValueError):
            points.cost_of("xiaole_video", {
                "model": "grok-imagine-video", "resolution": "1080p", "duration": 5,
            })

    def test_submit_path_and_validator_stay_wired_together(self):
        core_src = (Path(video.__file__).with_name("core.py")).read_text(encoding="utf-8")
        self.assertIn('elif kind == "xiaole_video":', core_src)
        self.assertIn('validate_xiaole_video_payload(body, user["username"])', core_src)
        self.assertIn("except video_domain.SeedanceReferenceUnavailable", core_src)
        self.assertTrue(callable(video.validate_xiaole_video_payload))


if __name__ == "__main__":
    unittest.main()
