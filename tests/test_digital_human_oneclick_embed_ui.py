from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
VIDEO_PAGE = ROOT / "site" / "workbench" / "video.html"
ONECLICK_PAGE = ROOT / "site" / "workbench" / "digital-human-oneclick.html"
SCRIPT_PAGE = ROOT / "site" / "workbench" / "script.html"


class DigitalHumanOneClickEmbedUiTests(unittest.TestCase):
    def test_video_workspace_no_longer_owns_the_oneclick_module(self):
        page = VIDEO_PAGE.read_text(encoding="utf-8")
        for marker in ("oneclickTab", "oneclickPanel", "oneclickFrame", "oneclick-active"):
            self.assertNotIn(marker, page)
        self.assertIn("var videoFunction='talking'", page)
        self.assertIn("updateFunction('talking')", page)

    def test_director_workspace_owns_the_precision_entry(self):
        page = SCRIPT_PAGE.read_text(encoding="utf-8")
        self.assertIn('href="digital-human-oneclick.html">🎬 数字人一键生成</a>', page)
        self.assertNotIn('data-mode="script_to_video"', page)
        self.assertIn("location.replace('digital-human-oneclick.html')", page)

    def test_oneclick_page_supports_embedded_workspace_mode(self):
        page = ONECLICK_PAGE.read_text(encoding="utf-8")
        self.assertIn("window.HQ_DIGITAL_HUMAN_EMBEDDED", page)
        self.assertIn("document.documentElement.classList.add('dh-embedded')", page)
        self.assertIn("if(!window.HQ_DIGITAL_HUMAN_EMBEDDED)", page)
        self.assertIn("hq:digital-human-oneclick:resize", page)
        self.assertIn("/workbench/digital-human-oneclick.html", page)
        self.assertIn('data-dh-mode="photo">数字人一键生成</button>', page)
        self.assertIn('data-dh-mode="video">真人视频 Precision</button>', page)
        self.assertNotIn("照片数字人", page)

    def test_embedded_401_redirects_the_top_level_workspace(self):
        page = ONECLICK_PAGE.read_text(encoding="utf-8")
        self.assertIn(
            "if(window.HQ_DIGITAL_HUMAN_EMBEDDED&&window.top!==window){window.top.location.href=target;return;}",
            page,
        )

    def test_standalone_401_redirects_the_current_page(self):
        page = ONECLICK_PAGE.read_text(encoding="utf-8")
        self.assertIn("window.location.href=target;", page)
        self.assertIn("var target='/login.html?next='+encodeURIComponent('/workbench/digital-human-oneclick.html')", page)

    def test_only_401_responses_invoke_the_login_redirect(self):
        page = ONECLICK_PAGE.read_text(encoding="utf-8")
        self.assertEqual(page.count("redirectToLogin();"), 1)
        self.assertIn("if(response.status===401){redirectToLogin();throw new Error('登录已过期');}", page)
        self.assertNotIn("if(response.status===401){location.href=", page)


if __name__ == "__main__":
    unittest.main()
