import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
LEGACY = (ROOT / "site/workbench/text-video.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "site/workbench/script.html").read_text(encoding="utf-8")
HOME = (ROOT / "site/index.html").read_text(encoding="utf-8")


class TextVideoEntryTests(unittest.TestCase):
    def test_old_url_is_a_compatibility_entry_for_digital_human(self):
        self.assertIn("location.replace('/workbench/digital-human-one-click'", LEGACY)
        self.assertNotIn("HQTextVideoEntry", LEGACY)
        self.assertNotIn("script_to_video", LEGACY)
        self.assertNotIn("/api/gen/", LEGACY)

    def test_home_navigation_targets_digital_human(self):
        href = 'href="/workbench/digital-human-one-click"'
        self.assertEqual(HOME.count(href), 2)
        self.assertNotIn('href="/workbench/script?mode=script_to_video"', HOME)

    def test_script_page_replaces_copy_to_video_with_digital_human(self):
        self.assertNotIn('data-mode="script_to_video"', SCRIPT)
        self.assertIn('href="digital-human-one-click.html">🎬 数字人一键生成</a>', SCRIPT)
        self.assertIn("location.replace('digital-human-one-click.html')", SCRIPT)
        self.assertIn('data-active="script"', SCRIPT)

    def test_refresh_recovery_and_material_handoff_remain_on_the_shared_page(self):
        self.assertIn("_resumeActiveVideoJob();", SCRIPT)
        self.assertIn("ACTIVE_VIDEO_JOB_KEY='hq_script_active_video_job'", SCRIPT)
        self.assertIn("if(refImages.length) payload.reference_images=refImages.slice();", SCRIPT)
        self.assertIn("options.endpoint||'/api/gen/script_to_video'", SCRIPT)
        self.assertIn('id="scGenVideo"', SCRIPT)

    def test_unauthenticated_submit_requests_login_without_dropping_recovery(self):
        submit = SCRIPT[SCRIPT.index("function _doGenerate("):]
        login_start = submit.index("if(disposition==='login')")
        login_end = submit.index("if(disposition==='clear')", login_start)
        login_branch = submit[login_start:login_end]
        self.assertIn("HQ.login()", login_branch)
        self.assertNotIn("_clearActiveVideoJob", login_branch)
        self.assertIn("if(x.s===401){ _confirmSubmission(videoPending)", submit)

    def test_one_submit_reuses_idempotency_and_blocks_duplicate_charge(self):
        submit = SCRIPT[SCRIPT.index("function _doGenerate("):]
        active_guard = submit.index("if(_readActiveVideoJob())")
        fetch_call = submit.index("fetch(endpoint")
        self.assertLess(active_guard, fetch_call)
        self.assertIn("videoPending.key", submit)
        self.assertIn("videoPending?videoPending.body:JSON.stringify(payload)", submit)
        self.assertIn("_saveActiveVideoJob({", submit)
        self.assertIn("if(x.s===402){ _confirmSubmission(videoPending)", submit)


if __name__ == "__main__":
    unittest.main()
