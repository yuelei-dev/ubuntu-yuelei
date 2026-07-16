import pathlib
import unittest


VIDEO_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/video.html"


class VideoTalkingPrefillUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = VIDEO_HTML.read_text(encoding="utf-8")

    def test_talking_prefill_reads_session_storage_before_falling_back_to_query(self):
        self.assertIn("sessionStorage.getItem('hq_script_to_talking')||''", self.html)
        self.assertIn("sessionStorage.removeItem('hq_script_to_talking')", self.html)
        self.assertIn("var talkingPrefill=q.get('prompt')||'';", self.html)
        self.assertIn("if(!talkingPrefill) return;", self.html)
        self.assertIn("updateFunction('talking');", self.html)


if __name__ == "__main__":
    unittest.main()
