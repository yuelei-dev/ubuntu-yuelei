# -*- coding: utf-8 -*-

import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "site" / "workbench" / "one-click-video.html"


class OneClickVideoUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_has_complete_review_and_render_flow(self):
        for marker in (
            "/api/gen/video/assets?limit=60",
            "/api/gen/video-compose/projects",
            "/analyze-source",
            "/edit-decisions",
            "/render",
            "开始分析",
            "确认粗剪",
            "渲染真实 MP4",
            "下载成片 MP4",
        ):
            self.assertIn(marker, self.html)

    def test_uses_asset_ids_and_never_accepts_arbitrary_source_urls(self):
        self.assertIn("source_asset_id:selected.id", self.html)
        self.assertNotIn("source_url:", self.html)
        self.assertNotIn("video_url:selected", self.html)

    def test_output_and_private_assets_are_fetched_with_authentication(self):
        self.assertIn("Authorization:'Bearer '+token", self.html)
        self.assertIn("protectedUrl(d.output_url)", self.html)

    def test_mobile_shell_can_scroll_to_review_and_render_panels(self):
        self.assertIn('@media(max-width:900px){.hq-app{overflow:auto!important}}', self.html)

    def test_smart_montage_is_default_and_keeps_rough_cut_as_a_second_mode(self):
        self.assertRegex(
            self.html,
            r'id="smartMode" class="mode-view"(?![^>]*\bhidden\b)',
        )
        self.assertRegex(
            self.html,
            r'id="roughMode" class="mode-view"[^>]*\bhidden\b',
        )
        for marker in ("文案智能成片", "口播粗剪", "智能拆分", "确认并生成"):
            self.assertIn(marker, self.html)
        self.assertIn('maxlength="320"', self.html)
        self.assertIn("完整朗读", self.html)

    def test_smart_plan_uses_copy_ratio_and_all_selected_styles(self):
        self.assertIn("'/api/gen/script_to_video/plan'", self.html)
        self.assertIn(
            "JSON.stringify({copy:copy,styles:styles,ratio:ratio})",
            self.html,
        )
        for style in ('value="luxe"', 'value="pop"', 'value="clinic"'):
            self.assertIn(style, self.html)
        self.assertIn("duration_seconds", self.html)
        self.assertIn("scene_count", self.html)
        self.assertIn("item.scenes.map(sceneHtml)", self.html)

    def test_each_style_submits_an_independent_real_job(self):
        self.assertIn("pipeline:'smart_montage'", self.html)
        self.assertNotIn("plan:stylePlan", self.html)
        self.assertIn("var payload={pipeline:'smart_montage',copy:copy,style:style,ratio:ratio}", self.html)
        self.assertIn("'Idempotency-Key':stableIdempotencyKey(style)", self.html)
        self.assertIn("sessionStorage.setItem('hq-smart-montage-batch-v1'", self.html)
        self.assertIn("pendingPlans=activePlan.styles.filter", self.html)
        self.assertIn("markStyleRetryable(style)", self.html)
        self.assertIn("canRotateRejectedSubmission(error)", self.html)
        self.assertIn("error&&error.status===404", self.html)
        self.assertIn("'/api/gen/script_to_video'", self.html)
        self.assertIn("'/api/gen/job/'+encodeURIComponent(jobId)", self.html)
        self.assertIn("pendingPlans.map", self.html)

    def test_smart_mode_does_not_advertise_a_fixed_duration_or_asset_count(self):
        self.assertIn("时长与素材数由文案决定", self.html)
        self.assertNotIn("固定 30 秒", self.html)
        self.assertNotIn("固定 6 张", self.html)

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_inline_javascript_syntax(self):
        scripts = re.findall(r"<script(?: [^>]*)?>(.*?)</script>", self.html, re.S)
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "inline.js"
            target.write_text("\n".join(scripts), encoding="utf-8")
            subprocess.run(["node", "--check", str(target)], check=True,
                           capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
