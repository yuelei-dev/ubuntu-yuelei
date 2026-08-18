import unittest
from pathlib import Path


class DigitalHumanV2UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (Path(__file__).resolve().parents[1] / "site" / "workbench" /
                    "digital-human-oneclick.html").read_text(encoding="utf-8")

    def test_page_offers_text_voice_and_full_audio_modes(self):
        self.assertIn('name="narrationMode" value="text"', self.page)
        self.assertIn('name="narrationMode" value="audio"', self.page)
        self.assertIn('id="driveAudio"', self.page)
        self.assertIn("/api/gen/digital-human-v2/audio-upload", self.page)
        self.assertIn("不需要再输入文案或选择音色", self.page)

    def test_gesture_candidates_are_not_presenter_segment_count(self):
        self.assertIn("手势形象数量", self.page)
        self.assertIn("只决定候选形象，不决定视频时长", self.page)
        self.assertIn("plan.gestures", self.page)
        self.assertIn("item.gesture_index", self.page)

    def test_duration_driven_presenter_and_material_contract_is_visible(self):
        self.assertIn("每隔约 20–30 秒全屏真人出镜", self.page)
        self.assertIn("开头、结尾和每隔 20–30 秒真人全屏", self.page)
        self.assertIn("digital_human_material_v2", self.page)
        self.assertIn("/api/gen/digital-human-v2/plan", self.page)
        self.assertIn("/api/gen/digital-human-v2/consent", self.page)
        self.assertIn("/api/gen/digital-human-v2/material-resolve", self.page)

    def test_material_priority_and_no_visible_source_label(self):
        self.assertIn("客户素材 → 飞书素材库 → 全网公开可用素材 → AI 补缺", self.page)
        self.assertIn("最终视频不显示素材来源标签", self.page)
        self.assertNotIn("CONCEPT / AI FILL", self.page)


if __name__ == "__main__":
    unittest.main()
