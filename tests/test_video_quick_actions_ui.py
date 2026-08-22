from pathlib import Path
import unittest


VIDEO_PAGE = Path(__file__).resolve().parents[1] / "site" / "workbench" / "video.html"


class VideoQuickActionsUiTests(unittest.TestCase):
    def test_oneclick_entries_are_removed_from_video_generation(self):
        page = VIDEO_PAGE.read_text(encoding="utf-8")
        self.assertNotIn('class="video-quick-actions"', page)
        self.assertNotIn('href="one-click-video.html">一键成片</a>', page)
        self.assertNotIn('href="digital-human-oneclick.html"', page)

    def test_digital_ip_tab_matches_the_production_entry(self):
        page = VIDEO_PAGE.read_text(encoding="utf-8")
        tabs_start = page.index('<div class="function-tabs"')
        tabs_end = page.index("</div>", tabs_start)
        tabs = page[tabs_start:tabs_end]

        self.assertIn(
            '<button class="function-tab on" type="button" data-function="talking">数字化 IP</button>',
            tabs,
        )
        self.assertLess(tabs.index("数字化 IP"), tabs.index("电影化身"))
        self.assertIn("$('talkingPanel').classList.toggle('hidden',videoFunction!=='talking')", page)

    def test_video_header_stacks_without_overflow_on_small_screens(self):
        page = VIDEO_PAGE.read_text(encoding="utf-8")
        self.assertIn("@media (max-width:640px)", page)
        self.assertIn(".video-header-row{align-items:stretch;flex-direction:column}", page)
        self.assertNotIn(".video-quick-actions{width:100%}", page)


if __name__ == "__main__":
    unittest.main()
