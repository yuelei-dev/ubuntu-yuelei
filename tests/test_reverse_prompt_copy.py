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


class ReversePromptCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node is required for reverse prompt copy contracts")
        html = SCRIPT_HTML.read_text(encoding="utf-8")
        cls.functions = "\n".join(
            _extract_function(html, name)
            for name in (
                "isReversePromptErrorText",
                "validReversePromptText",
                "reverseResultPrompt",
                "reversePromptText",
                "copyText",
                "copyReversePrompt",
                "copyAllVisibleText",
            )
        )

    def _run(self, *, dom_text, state_prompt, clipboard="success", card_present=True):
        clipboard_body = {
            "success": "writes.push(text); return Promise.resolve();",
            "failure": "return Promise.reject(new Error('denied'));",
        }[clipboard]
        harness = f"""
var writes=[];
var toasts=[];
var card={str(card_present).lower()}?{{textContent:{json.dumps(dom_text, ensure_ascii=False)}}}:null;
var document={{
  getElementById:function(id){{return id==='bdReversePromptText'?card:null;}},
  createElement:function(){{throw new Error('fallback not expected');}}
}};
var lastBreakdownReverse={{
  prompt:{json.dumps(state_prompt, ensure_ascii=False)},
  source_url:'https://example.invalid/video'
}};
Object.defineProperty(globalThis,'navigator',{{
  value:{{clipboard:{{writeText:function(text){{{clipboard_body}}}}}}},
  configurable:true
}});
var bdReverseCopyBtn={{textContent:'📋 复制提示词',innerHTML:'📋 复制提示词'}};
var reverseCopyOrig=bdReverseCopyBtn.innerHTML;
var window={{HQ:{{toast:function(message){{toasts.push(message);}}}}}};
var HQ=window.HQ;
function reverseLegacyPrompt(){{return '';}}
function setTimeout(callback){{callback();}}
{self.functions}
copyReversePrompt().then(function(copied){{
  process.stdout.write(JSON.stringify({{
    copied:copied,
    writes:writes,
    toasts:toasts,
    button:bdReverseCopyBtn.innerHTML
  }}));
}});
"""
        result = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(result.stdout)

    def test_copies_exact_visible_prompt(self):
        result = self._run(
            dom_text="页面实际显示的反推提示词",
            state_prompt="旧状态里的其他文本",
        )
        self.assertTrue(result["copied"])
        self.assertEqual(result["writes"], ["页面实际显示的反推提示词"])
        self.assertEqual(result["toasts"], [])

    def test_failure_message_is_never_copied_or_reported_as_success(self):
        result = self._run(
            dom_text="反推失败：模型响应异常 · 已退点",
            state_prompt="旧状态里的有效提示词",
        )
        self.assertFalse(result["copied"])
        self.assertEqual(result["writes"], [])
        self.assertEqual(result["toasts"], ["没有可复制的有效提示词"])
        self.assertEqual(result["button"], "📋 复制提示词")

    def test_missing_current_card_never_falls_back_to_stale_prompt(self):
        result = self._run(
            dom_text="",
            state_prompt="上一条成功任务的旧提示词",
            card_present=False,
        )
        self.assertFalse(result["copied"])
        self.assertEqual(result["writes"], [])
        self.assertEqual(result["toasts"], ["没有可复制的有效提示词"])
        self.assertEqual(result["button"], "📋 复制提示词")

    def test_clipboard_rejection_does_not_report_success(self):
        result = self._run(
            dom_text="页面实际显示的反推提示词",
            state_prompt="",
            clipboard="failure",
        )
        self.assertFalse(result["copied"])
        self.assertEqual(result["writes"], [])
        self.assertEqual(result["toasts"], ["复制失败，请检查浏览器剪贴板权限"])
        self.assertEqual(result["button"], "📋 复制提示词")

    def test_visible_copy_all_rejection_does_not_report_success(self):
        harness = f"""
var toasts=[];
var card={{textContent:'页面实际显示的反推提示词'}};
var document={{
  getElementById:function(id){{return id==='bdReversePromptText'?card:null;}},
  createElement:function(){{throw new Error('fallback denied');}}
}};
var lastBreakdownReverse={{source_url:'https://example.invalid/video'}};
Object.defineProperty(globalThis,'navigator',{{
  value:{{clipboard:{{writeText:function(){{return Promise.reject(new Error('denied'));}}}}}},
  configurable:true
}});
var copyBtn={{textContent:'复制全部',innerHTML:'复制全部'}};
var copyAllOrig=copyBtn.innerHTML;
var currentMode='breakdown';
function isBreakdownReverseTool(){{return true;}}
function getDisplayedScenes(){{return [];}}
function scriptText(){{return '';}}
var window={{HQ:{{toast:function(message){{toasts.push(message);}}}}}};
var HQ=window.HQ;
function setTimeout(callback){{callback();}}
{self.functions}
copyAllVisibleText().then(function(copied){{
  process.stdout.write(JSON.stringify({{
    copied:copied,toasts:toasts,button:copyBtn.innerHTML
  }}));
}});
"""
        result = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        got = json.loads(result.stdout)
        self.assertFalse(got["copied"])
        self.assertEqual(got["toasts"], ["复制失败，请检查浏览器剪贴板权限"])
        self.assertEqual(got["button"], "复制全部")


if __name__ == "__main__":
    unittest.main()
