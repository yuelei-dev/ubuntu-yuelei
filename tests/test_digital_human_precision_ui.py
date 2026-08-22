import pathlib
import shutil
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = (ROOT / "site/workbench/digital-human-one-click.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "site/workbench/digital-human-one-click.js").read_text(encoding="utf-8")
DIRECTOR = (ROOT / "site/workbench/script.html").read_text(encoding="utf-8")
VIDEO = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")


class DigitalHumanPrecisionUiTests(unittest.TestCase):
    def test_module_lives_under_director_and_replaces_copy_to_video(self):
        self.assertIn('data-active="script"', PAGE)
        self.assertIn('href="script.html"', PAGE)
        self.assertIn('aria-current="page"', PAGE)
        self.assertIn('href="digital-human-one-click.html">🎬 数字人一键生成</a>', DIRECTOR)
        self.assertNotIn('data-mode="script_to_video"', DIRECTOR)
        self.assertNotIn("oneclickPanel", VIDEO)

    def test_real_precision_pipeline_is_wired_end_to_end(self):
        for marker in (
            "/api/gen/video/lipsync-import", "/api/gen/audio", "/api/gen/video",
            "/api/gen/video-compose/projects", "/analyze-source",
            "/edit-decisions", "/render",
        ):
            self.assertIn(marker, SCRIPT)
        self.assertIn("lipsync_mode:'precision'", SCRIPT)
        self.assertIn("dynamic_duration:true", SCRIPT)
        self.assertIn("'Idempotency-Key'", SCRIPT)
        self.assertIn("data.output_url||waitRender()", SCRIPT)
        self.assertIn("state.project.id+'/output'", SCRIPT)

    def test_three_templates_have_visible_ten_second_examples(self):
        expected = {
            "viral-talking-head-v1": "high-frequency-10s.mp4",
            "professional-explainer-v1": "professional-explainer-10s.mp4",
            "clean-talking-v1": "clean-talking-10s.mp4",
        }
        for template_id, filename in expected.items():
            self.assertIn('data-template="%s"' % template_id, PAGE)
            self.assertIn(filename, PAGE)
            preview = ROOT / "site/assets/one-click/previews" / filename
            self.assertGreater(preview.stat().st_size, 100000)
        self.assertEqual(3, PAGE.count("<video muted loop playsinline"))

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_javascript_parses(self):
        completed = subprocess.run(
            ["node", "--check", str(ROOT / "site/workbench/digital-human-one-click.js")],
            capture_output=True, text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
