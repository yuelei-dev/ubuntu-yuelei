from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
VIDEO_PAGE = ROOT / "site" / "workbench" / "video.html"
ONECLICK_PAGE = ROOT / "site" / "workbench" / "digital-human-oneclick.html"


class DigitalHumanOneClickEmbedUiTests(unittest.TestCase):
    def test_oneclick_tab_opens_inside_the_video_workspace(self):
        page = VIDEO_PAGE.read_text(encoding="utf-8")
        self.assertIn('class="function-tab on" href="digital-human-oneclick.html" id="oneclickTab"', page)
        self.assertIn('data-oneclick-tab="true"', page)
        self.assertIn('id="oneclickPanel" class="function-panel oneclick-panel"', page)
        self.assertIn('id="oneclickFrame"', page)
        self.assertIn('src="digital-human-oneclick.html?embed=1"', page)
        self.assertIn("var videoFunction='oneclick'", page)
        self.assertIn("event.preventDefault();updateFunction('oneclick')", page)
        self.assertIn("updateFunction('oneclick')", page)

    def test_oneclick_page_supports_embedded_workspace_mode(self):
        page = ONECLICK_PAGE.read_text(encoding="utf-8")
        self.assertIn("window.HQ_DIGITAL_HUMAN_EMBEDDED", page)
        self.assertIn("document.documentElement.classList.add('dh-embedded')", page)
        self.assertIn("if(!window.HQ_DIGITAL_HUMAN_EMBEDDED)", page)
        self.assertIn("hq:digital-human-oneclick:resize", page)
        self.assertIn("/workbench/video.html?function=oneclick", page)

    def test_embedded_401_redirects_the_top_level_workspace(self):
        page = ONECLICK_PAGE.read_text(encoding="utf-8")
        self.assertIn(
            "if(window.HQ_DIGITAL_HUMAN_EMBEDDED&&window.top!==window){window.top.location.href=target;return;}",
            page,
        )

    def test_standalone_401_redirects_the_current_page(self):
        page = ONECLICK_PAGE.read_text(encoding="utf-8")
        self.assertIn("window.location.href=target;", page)
        self.assertIn("var target='/login.html?next='+encodeURIComponent('/workbench/video.html?function=oneclick')", page)

    def test_only_401_responses_invoke_the_login_redirect(self):
        page = ONECLICK_PAGE.read_text(encoding="utf-8")
        self.assertEqual(page.count("redirectToLogin();"), 1)
        self.assertIn("if(response.status===401){redirectToLogin();throw new Error('登录已过期');}", page)
        self.assertNotIn("if(response.status===401){location.href=", page)


if __name__ == "__main__":
    unittest.main()
