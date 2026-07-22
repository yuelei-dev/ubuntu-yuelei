import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "site/workbench/video.html"
NODE_BEHAVIOR_PATH = ROOT / "tests/test_talking_motion_prompt_frontend_node.js"


class TalkingMotionPromptFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML_PATH.read_text(encoding="utf-8")
        scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", cls.html)
        cls.inline_script = scripts[-1]

    def test_optional_controls_and_editable_preview_exist(self):
        self.assertIn('id="talkingMotionPrompt"', self.html)
        self.assertIn('maxlength="500"', self.html)
        self.assertIn('id="talkingMotionPromptCount"', self.html)
        self.assertIn('id="talkingMotionOptimize"', self.html)
        self.assertIn('AI 优化', self.html)
        self.assertIn('id="talkingMotionOptimized"', self.html)
        self.assertIn('id="talkingMotionOptimizedWrap"', self.html)
        self.assertIn('id="talkingMotionState"', self.html)
        optimized_tag = re.search(r'<textarea\s+id="talkingMotionOptimized"[^>]*>', self.html)
        self.assertIsNotNone(optimized_tag)
        self.assertNotIn("readonly", optimized_tag.group(0).lower())

    def test_optimizer_calls_authenticated_endpoint_without_generating_video(self):
        start = self.inline_script.index("function optimizeTalkingMotion")
        end = self.inline_script.index("$('talkingMotionPrompt').addEventListener", start)
        block = self.inline_script[start:end]
        self.assertIn("fetch('/api/gen/video/motion-prompt-optimize'", block)
        self.assertIn("Authorization:'Bearer '+token", block)
        self.assertIn("JSON.stringify({prompt:original.trim()})", block)
        self.assertNotIn("submitVideo(", block)
        self.assertNotIn("/api/gen/video'", block)

    def test_shared_single_and_batch_payload_contains_original_and_confirmed_prompt(self):
        start = self.inline_script.index("function talkingPayload")
        end = self.inline_script.index("function submitVideoBatch", start)
        block = self.inline_script[start:end]
        self.assertIn("talkingMotionFields()", block)
        self.assertIn("motion_prompt_original", block)
        self.assertIn("motion_prompt", block)
        self.assertIn("avatarIv?'1080p':selectedResolution", block)
        self.assertIn("avatarIv?'9:16':selectedRatio", block)
        self.assertIn("var body=talkingPayload({});", self.inline_script)
        self.assertIn("var body=talkingPayload({image_data:imageData});", self.inline_script)
        self.assertIn("body:JSON.stringify(body)", self.inline_script)

    def test_stale_optimized_preview_is_not_sent(self):
        start = self.inline_script.index("function talkingMotionFields")
        end = self.inline_script.index("function setTalkingMotionState", start)
        block = self.inline_script[start:end]
        self.assertIn("talkingMotionOptimizedSource===originalRaw", block)
        self.assertIn("optimizedValid?optimized:original", block)
        self.assertIn("优化结果已失效", self.inline_script)

    def test_failed_optimization_preserves_original_and_reports_backend_error(self):
        start = self.inline_script.index("function optimizeTalkingMotion")
        end = self.inline_script.index("$('talkingMotionPrompt').addEventListener", start)
        block = self.inline_script[start:end]
        catch_start = block.rindex(".catch(function(e){")
        catch_end = block.index("}).finally", catch_start)
        failure_handler = block[catch_start:catch_end]
        self.assertIn("setTalkingMotionState(e.message||'动作提示词优化失败','error')", failure_handler)
        self.assertNotRegex(
            failure_handler,
            r"\$\('talkingMotionPrompt'\)\.value\s*=(?!=)",
            "优化失败处理不得清空或改写原始提示词",
        )
        self.assertIn("res.data.detail", block)
        self.assertIn("res.status===429", block)

    def test_optimize_and_submit_buttons_share_locks(self):
        start = self.inline_script.index("function syncVideoGenerateButtons")
        end = self.inline_script.index("function ", start + 20)
        block = self.inline_script[start:end]
        self.assertIn("motionOptimizeBusy", block)
        self.assertIn("videoSubmitLocks.talking", block)
        self.assertIn("applyButtonState('talkingMotionOptimize'", block)
        self.assertIn("applyButtonState('generateBtn'", block)

    def test_inline_javascript_parses(self):
        checked = subprocess.run(
            ["node", "--check", "-"], input=self.inline_script, text=True,
            encoding="utf-8", capture_output=True,
        )
        self.assertEqual(0, checked.returncode, checked.stderr)
        behavior = subprocess.run(
            ["node", str(NODE_BEHAVIOR_PATH)], text=True,
            encoding="utf-8", capture_output=True,
        )
        self.assertEqual(0, behavior.returncode, behavior.stderr or behavior.stdout)


if __name__ == "__main__":
    unittest.main()
