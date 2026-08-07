# -*- coding: utf-8 -*-

import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "site" / "workbench" / "one-click-video.html"


class OneClickVideoUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_has_complete_review_and_render_flow(self):
        for marker in (
            "/api/gen/video/assets?limit=60",
            "/api/gen/video-compose/projects",
            "/analyze-source",
            "/edit-decisions",
            "/render",
            "开始分析",
            "确认粗剪",
            "渲染真实 MP4",
            "下载成片 MP4",
        ):
            self.assertIn(marker, self.html)

    def test_uses_asset_ids_and_never_accepts_arbitrary_source_urls(self):
        self.assertIn("source_asset_id:selected.id", self.html)
        self.assertNotIn("source_url:", self.html)
        self.assertNotIn("video_url:selected", self.html)

    def test_output_and_private_assets_are_fetched_with_authentication(self):
        self.assertIn("Authorization:'Bearer '+token", self.html)
        self.assertIn("protectedUrl(d.output_url)", self.html)

    def test_mobile_shell_can_scroll_to_review_and_render_panels(self):
        self.assertIn('@media(max-width:900px){.hq-app{overflow:auto!important}}', self.html)

    def test_smart_montage_is_default_and_keeps_rough_cut_as_a_second_mode(self):
        self.assertRegex(
            self.html,
            r'id="smartMode" class="mode-view"(?![^>]*\bhidden\b)',
        )
        self.assertRegex(
            self.html,
            r'id="roughMode" class="mode-view"[^>]*\bhidden\b',
        )
        for marker in ("文案智能成片", "口播粗剪", "智能拆分", "确认并生成"):
            self.assertIn(marker, self.html)
        self.assertIn('maxlength="320"', self.html)
        self.assertIn("完整朗读", self.html)

    def test_smart_plan_uses_copy_ratio_and_all_selected_styles(self):
        self.assertIn("'/api/gen/script_to_video/plan'", self.html)
        self.assertIn(
            "JSON.stringify({copy:copy,styles:styles,ratio:ratio})",
            self.html,
        )
        for style in ('value="luxe"', 'value="pop"', 'value="clinic"'):
            self.assertIn(style, self.html)
        self.assertIn("duration_seconds", self.html)
        self.assertIn("scene_count", self.html)
        self.assertIn("item.scenes.map(sceneHtml)", self.html)

    def test_each_style_submits_an_independent_real_job(self):
        self.assertIn("pipeline:'smart_montage'", self.html)
        self.assertNotIn("plan:stylePlan", self.html)
        self.assertIn("plan_digest:stylePlan.plan_digest", self.html)
        self.assertIn("idempotencyKey=stableIdempotencyKey(style)", self.html)
        self.assertIn("'Idempotency-Key':idempotencyKey", self.html)
        self.assertIn("smartStorageKey='hq-smart-montage-batch-v2'", self.html)
        self.assertIn("sessionStorage.setItem(smartStorageKey", self.html)
        self.assertIn("pendingPlans=activePlan.styles.filter", self.html)
        self.assertIn("markStyleRetryable(style,binding)", self.html)
        self.assertIn("canRotateRejectedSubmission(error)", self.html)
        self.assertIn("error&&error.status===404", self.html)
        self.assertIn("'/api/gen/script_to_video'", self.html)
        self.assertIn("'/api/gen/job/'+encodeURIComponent(jobId)", self.html)
        self.assertIn("pendingPlans.map", self.html)

    def test_smart_batch_restores_full_input_plan_and_jobs_after_refresh(self):
        for marker in (
            "saved.input={copy:",
            "saved.plan=plan",
            "function restoreBatchOnLoad()",
            "$('smartCopy').value=copy",
            "activePlan=plan",
            "resumeBatchJobs(plan)",
            "restoreBatchOnLoad();",
        ):
            self.assertIn(marker, self.html)

    def test_smart_polling_is_bound_to_one_batch_job_and_submission_key(self):
        for marker in (
            "smartPolls={}",
            "function batchJobMatches(binding)",
            "smartBatch.signature===binding.signature",
            "String(smartBatch.jobs[binding.style]||'')===binding.jobId",
            "String(smartBatch.keys[binding.style]||'')===binding.key",
            "function pollIsCurrent(binding)",
            "smartPolls[binding.style]===binding",
            "function startJobPoll(style,jobId)",
            "if(!pollIsCurrent(binding))return",
            "markStyleRetryable(style,binding)",
            "if(!batchJobMatches(binding))return false",
        ):
            self.assertIn(marker, self.html)

        start_poll = self.html.split("function startJobPoll(style,jobId)", 1)[1]
        start_poll = start_poll.split("function resumeBatchJobs(plan)", 1)[0]
        self.assertIn("existing.signature===signature", start_poll)
        self.assertIn("existing.jobId===jobId", start_poll)
        self.assertIn("existing.key===key", start_poll)
        self.assertIn("return existing", start_poll)

        retryable = self.html.split(
            "function markStyleRetryable(style,binding)", 1
        )[1].split("function canRotateRejectedSubmission", 1)[0]
        self.assertLess(
            retryable.index("if(!batchJobMatches(binding))return false"),
            retryable.index("delete smartBatch.jobs[style]"),
        )
        self.assertNotIn("pollJob(item.style,jobId,0)", self.html)
        self.assertNotIn("pollJob(style,data.job_id,0)", self.html)

    def test_stale_submission_response_cannot_overwrite_a_new_batch(self):
        submit = self.html.split("function submitStyle(copy,ratio,stylePlan)", 1)[1]
        submit = submit.split("function generateAll()", 1)[0]
        guard = (
            "if(smartBatch.signature!==submissionSignature||"
            "smartBatch.keys[style]!==idempotencyKey)return false"
        )
        self.assertEqual(2, submit.count(guard))
        self.assertLess(
            submit.index(guard),
            submit.index("smartBatch.jobs[style]=data.job_id"),
        )

    def test_refund_pending_never_enables_a_second_paid_submission(self):
        self.assertIn("job.refund_state", self.html)
        self.assertIn("refundState==='pending'", self.html)
        self.assertIn("退款确认完成前不会重复提交", self.html)
        pending_branch = self.html.split("if(refundState==='pending')", 1)[1].split(
            "renderJobCard(style,'失败'", 1
        )[0]
        self.assertNotIn("markStyleRetryable(", pending_branch)

    def test_smart_mode_does_not_advertise_a_fixed_duration_or_asset_count(self):
        self.assertIn("时长与素材数由文案决定", self.html)
        self.assertNotIn("固定 30 秒", self.html)
        self.assertNotIn("固定 6 张", self.html)

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_inline_javascript_syntax(self):
        scripts = re.findall(r"<script(?: [^>]*)?>(.*?)</script>", self.html, re.S)
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "inline.js"
            target.write_text("\n".join(scripts), encoding="utf-8")
            subprocess.run(["node", "--check", str(target)], check=True,
                           capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
