import json
import pathlib
import shutil
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_HTML = ROOT / "site" / "workbench" / "script.html"


def _extract_function(source, name):
    marker = f"function {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


class ReversePromptRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node is required for reverse prompt rendering contracts")
        html = SCRIPT_HTML.read_text(encoding="utf-8")
        cls.functions = "\n".join(
            _extract_function(html, name)
            for name in ("reverseLegacyDisplaySections", "renderBreakdownReverse")
        )

    def _render(self, payload):
        harness = f"""
{self.functions}
var lastBreakdownReverse=null;
var bdEditing=false;
var bdEditBtn=null;
var bdEditCancelBtn=null;
var meta={{innerHTML:''}};
var scenes={{innerHTML:''}};
var bdResultMeta=null;
var bdSourceLink=null;
function esc(value){{return String(value);}}
function platformText(value){{return value||'';}}
function fmtDur(value){{return value||'';}}
function setBreakdownAnalysis(){{}}
function renderSceneStats(){{}}
function setBreakdownStoryboard(){{}}
function syncModeUi(){{}}
renderBreakdownReverse({json.dumps(payload, ensure_ascii=False)});
process.stdout.write(JSON.stringify({{html:scenes.innerHTML}}));
"""
        result = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(result.stdout)["html"]

    def test_task_3251_realtime_audit_sections_show_real_prompt_not_empty_cards(self):
        html = self._render(
            {
                "type": "breakdown_reverse",
                "prompt": "3251 REAL REVERSE PROMPT",
                "sections": {
                    "reverse_audit": {
                        "reference_thumbnail_indices": [1, 2, 3, 4],
                        "frame_manifest": [{"global_frame_number": 1}],
                    }
                },
            }
        )
        self.assertIn("3251 REAL REVERSE PROMPT", html)
        self.assertIn('id="bdReversePromptText"', html)
        self.assertIn("display:block", html)
        self.assertEqual(html.count('class="sc-card"'), 1)

    def test_task_3255_history_restore_audit_sections_show_persisted_prompt(self):
        history_meta = {
            "type": "breakdown_reverse",
            "prompt": "3255 PERSISTED REVERSE PROMPT",
            "frame_thumbnails": ["ref-1", "ref-2", "ref-3", "ref-4"],
            "reference_thumbnail_indices": [1, 2, 3, 4],
            "sections": {
                "reverse_audit": {
                    "reference_thumbnail_indices": [1, 2, 3, 4],
                    "audit_thumbnail_indices": [1, 2, 3, 4, 5, 6, 7, 8],
                }
            },
        }
        restored = {
            "type": "breakdown_reverse",
            "prompt": history_meta["prompt"],
            "frame_thumbnails": history_meta["frame_thumbnails"],
            "reference_thumbnail_indices": history_meta["reference_thumbnail_indices"],
            "sections": history_meta["sections"],
        }
        html = self._render(restored)
        self.assertIn("3255 PERSISTED REVERSE PROMPT", html)
        self.assertIn("display:block", html)
        self.assertEqual(html.count('class="sc-card"'), 1)

    def test_task_3268_gemini_audit_result_keeps_generated_prompt_visible(self):
        html = self._render(
            {
                "type": "breakdown_reverse",
                "prompt": "3268 GENERATED GEMINI PROMPT",
                "sections": {
                    "reverse_audit": {
                        "model_provider": "google",
                        "model_id": "gemini-3.1-pro-preview",
                        "model_attempts": 2,
                        "segment_evidence": [
                            {
                                "omitted_unsupported_fields": [
                                    {"field": "sound", "reason": "no_segment_asr"}
                                ]
                            }
                        ],
                    }
                },
            }
        )
        self.assertIn("3268 GENERATED GEMINI PROMPT", html)
        self.assertIn('id="bdReversePromptText"', html)
        self.assertIn("display:block", html)
        self.assertEqual(html.count('class="sc-card"'), 1)

    def test_legacy_display_sections_render_cards_without_hiding_prompt(self):
        html = self._render(
            {
                "prompt": "LEGACY FULL PROMPT",
                "sections": {
                    "subject": "legacy subject",
                    "scene": "legacy scene",
                    "composition": "legacy composition",
                    "action": "legacy action",
                    "lighting": "legacy lighting",
                    "style": "legacy style",
                    "parameters": "legacy parameters",
                },
            }
        )
        self.assertIn("legacy subject", html)
        self.assertIn("legacy parameters", html)
        self.assertIn("LEGACY FULL PROMPT", html)
        self.assertIn('id="bdReversePromptText"', html)
        self.assertNotIn("display:none", html)
        self.assertIn("display:block", html)
        self.assertEqual(html.count('class="sc-card"'), 8)

    def test_empty_or_abnormal_sections_fall_back_to_prompt(self):
        for sections in ({}, [], "invalid", {"subject": "", "scene": None}):
            with self.subTest(sections=sections):
                html = self._render(
                    {"prompt": "SAFE FALLBACK PROMPT", "sections": sections}
                )
                self.assertIn("SAFE FALLBACK PROMPT", html)
                self.assertIn("display:block", html)
                self.assertEqual(html.count('class="sc-card"'), 1)


if __name__ == "__main__":
    unittest.main()
