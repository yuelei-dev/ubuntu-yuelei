import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
LEGACY = (ROOT / "site/workbench/text-video.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "site/workbench/script.html").read_text(encoding="utf-8")
HOME = (ROOT / "site/index.html").read_text(encoding="utf-8")


class TextVideoEntryTests(unittest.TestCase):
    def test_entry_helper_behavior_in_node(self):
        result = subprocess.run(
            ["node", "tests/test_text_video_entry.js"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("10 assertions passed", result.stdout)

    def test_old_url_is_a_compatibility_entry_without_duplicate_business_logic(self):
        self.assertIn("HQTextVideoEntry.canonicalTarget(location.search,location.hash)", LEGACY)
        self.assertIn("location.replace(target)", LEGACY)
        self.assertIn("/workbench/script?mode=script_to_video", LEGACY)
        self.assertNotIn("/api/gen/", LEGACY)

    def test_home_navigation_targets_the_canonical_mode(self):
        href = 'href="/workbench/script?mode=script_to_video"'
        self.assertEqual(HOME.count(href), 2)
        self.assertNotIn(
            '<a href="/workbench/one-click-video"><b>文案成片</b>',
            HOME,
        )

    def test_script_page_opens_and_keeps_script_to_video_mode(self):
        self.assertIn('data-mode="script_to_video"', SCRIPT)
        self.assertIn("HQTextVideoEntry.modeFromSearch(location.search)", SCRIPT)
        self.assertIn("currentMode=entryMode", SCRIPT)
        self.assertIn("switchMode(currentMode)", SCRIPT)
        self.assertIn("HQTextVideoEntry.keepModeAfterWrite(currentMode)", SCRIPT)
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
