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

    def test_smart_montage_is_default_and_keeps_all_three_modes_on_one_page(self):
        self.assertRegex(
            self.html,
            r'id="smartMode" class="mode-view"(?![^>]*\bhidden\b)',
        )
        self.assertRegex(
            self.html,
            r'id="roughMode" class="mode-view"[^>]*\bhidden\b',
        )
        self.assertRegex(
            self.html,
            r'id="digitalHumanMode" class="mode-view"[^>]*\bhidden\b',
        )
        for marker in ("文案智能成片", "口播粗剪", "数字人口播成片", "智能拆分", "确认并生成"):
            self.assertIn(marker, self.html)
        self.assertIn('maxlength="320"', self.html)
        self.assertIn("完整朗读", self.html)

    def test_digital_human_mode_is_lazy_loaded_and_kept_alive_between_tabs(self):
        self.assertIn('id="digitalHumanModeTab"', self.html)
        self.assertIn('aria-controls="digitalHumanMode"', self.html)
        self.assertIn('id="digitalHumanFrame"', self.html)
        self.assertIn('data-src="digital-human-oneclick.html?embed=1"', self.html)
        self.assertNotRegex(self.html, r'id="digitalHumanFrame"[^>]+\ssrc=')
        self.assertIn("if(frame.getAttribute('src'))return", self.html)
        self.assertIn("frame.setAttribute('src',frame.getAttribute('data-src'))", self.html)
        self.assertIn("'digital-human':{tab:'digitalHumanModeTab'", self.html)
        self.assertIn("initialParams.get('mode')", self.html)
        self.assertNotIn("OpenAI", self.html)

    def test_smart_plan_uses_copy_ratio_and_all_selected_styles(self):
        self.assertIn("'/api/gen/script_to_video/plan'", self.html)
        self.assertIn(
            "JSON.stringify({copy:copy,styles:styles,ratio:ratio})",
            self.html,
        )
        for style in (
            'value="luxe"', 'value="pop"', 'value="clinic"',
            'value="wellness"', 'value="neon"', 'value="editorial"',
        ):
            self.assertIn(style, self.html)
        self.assertIn("duration_seconds", self.html)
        self.assertIn("scene_count", self.html)
        self.assertIn("item.scenes.map(sceneHtml)", self.html)

    def test_six_templates_are_named_but_only_three_are_selected_by_default(self):
        for name in (
            "高奢美学", "潮流快闪", "专业科技",
            "自然疗愈", "未来霓虹", "杂志拼贴",
        ):
            self.assertIn(name, self.html)
        inputs = re.findall(
            r'<input type="checkbox" name="smartStyle" value="([a-z]+)"([^>]*)>',
            self.html,
        )
        self.assertEqual(6, len(inputs))
        selected = {style for style, attributes in inputs if "checked" in attributes}
        self.assertEqual({"luxe", "pop", "clinic"}, selected)
        self.assertIn("MAX_SMART_STYLES=3", self.html)
        self.assertIn(
            "SMART_PLANNER_VERSION='script_video_montage_v3'", self.html,
        )
        self.assertIn("styles.length>MAX_SMART_STYLES", self.html)
        self.assertIn("单次最多 3 种", self.html)

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

    def test_smart_batch_restores_v4_input_slots_keys_and_jobs_after_refresh(self):
        for marker in (
            "version:4",
            "saved.input={copy:",
            "saved.plan=plan",
            "material_slots_by_style",
            "unresolved_submissions",
            "function restoreBatchOnLoad()",
            "$('smartCopy').value=copy",
            "originalVersion===2||originalVersion===3",
            "originalVersion===4",
            "smartBatch.version=4",
            "resumeBatchJobs(plan)",
            "restoreBatchOnLoad();",
        ):
            self.assertIn(marker, self.html)
        restore = self.html.split("function restoreBatchOnLoad()", 1)[1]
        restore = restore.split("function batchJobMatches", 1)[0]
        self.assertLess(
            restore.index("signatureHash(currentBatchSignature())!==saved.signature"),
            restore.index("smartBatch.version=4"),
        )
        self.assertIn("saved.keys", restore)
        self.assertIn("saved.jobs", restore)
        self.assertIn("saved.unresolved_submissions", restore)
        self.assertIn("storedMaterials(saved.input.material_uploads||[],true)", restore)
        self.assertIn("item.expires_at<=now", restore)
        self.assertIn("savedUnresolved?allMaterials", restore)
        self.assertIn("已提交任务继续监控", self.html)

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

    def test_user_material_library_enforces_count_file_and_total_byte_limits(self):
        for marker in (
            'id="smartMaterialInput" type="file"',
            'accept="image/png,image/jpeg,image/webp" multiple',
            "MAX_SMART_MATERIALS=20",
            "MAX_SMART_MATERIAL_BYTES=10*1024*1024",
            "MAX_SMART_TOTAL_BYTES=96*1024*1024",
            "window.crypto.subtle.digest('SHA-256',buffer)",
            "'/api/gen/script_to_video/material-upload'",
            "'X-HQ-Image-SHA256':sha256",
            "data-material-action=\"up\"",
            "data-material-action=\"down\"",
            "data-material-action=\"remove\"",
            "用户素材",
            "AI补图",
        ):
            self.assertIn(marker, self.html)

        choose = self.html.split("function chooseMaterialFiles(fileList)", 1)[1]
        choose = choose.split("function clearUploadFromSlots", 1)[0]
        self.assertIn("smartUploads.length+files.length>MAX_SMART_MATERIALS", choose)
        self.assertIn("files[i].size>MAX_SMART_MATERIAL_BYTES", choose)
        self.assertIn("existingBytes+incomingBytes>MAX_SMART_TOTAL_BYTES", choose)
        self.assertIn("function uploadNext(index)", choose)
        self.assertIn("return uploadNext(index+1)", choose)
        self.assertNotIn("Promise.all", choose)

    def test_first_plan_prefills_each_template_in_library_order(self):
        prefix = self.html.split("function prefixMaterialSlots(plan)", 1)[1]
        prefix = prefix.split("function slotMapHasMaterials", 1)[0]
        self.assertIn("plan.styles.forEach(function(stylePlan)", prefix)
        self.assertIn("slots=new Array(count).fill(null)", prefix)
        self.assertIn("slots[i]=available[i].upload_id", prefix)

        build = self.html.split("function buildPlan()", 1)[1]
        build = build.split("function restoreBatchOnLoad", 1)[0]
        self.assertIn(":prefixMaterialSlots(plan)", build)
        self.assertIn("previousPlan?normalizeMaterialSlots", build)

    def test_each_template_scene_has_an_independent_material_selector(self):
        for marker in (
            'class="scene-material-select"',
            'data-material-style="',
            'data-material-scene="',
            "AI 补图（自动生成）",
            "function changeSceneMaterial(select)",
            "map[style]",
        ):
            self.assertIn(marker, self.html)

        normalize = self.html.split("function normalizeMaterialSlots", 1)[1]
        normalize = normalize.split("function prefixMaterialSlots", 1)[0]
        self.assertIn("plan.styles.forEach(function(stylePlan)", normalize)
        self.assertIn("var used={}", normalize)
        self.assertIn("used[uploadId]", normalize)
        self.assertIn("result[stylePlan.style]=slots", normalize)
        self.assertRegex(
            normalize,
            r"plan\.styles\.forEach\(function\(stylePlan\)\{[\s\S]*?"
            r"var used=\{\}",
        )

        change = self.html.split("function changeSceneMaterial(select)", 1)[1]
        change = change.split("function invalidatePlan", 1)[0]
        self.assertIn("slots.indexOf(uploadId)", change)
        self.assertIn("slots[cleared]=null", change)
        self.assertIn("slots[index]=uploadId||null", change)
        # used is recreated inside each style iteration, so an upload may be reused
        # by another template while duplicates within one template are removed.
        self.assertLess(
            normalize.index("plan.styles.forEach(function(stylePlan)"),
            normalize.index("var used={}"),
        )

    def test_submission_uses_an_immutable_exact_nullable_slot_snapshot(self):
        slots = self.html.split(
            "function materialSlotsForPlan(stylePlan)", 1,
        )[1].split("function submitStyle", 1)[0]
        self.assertIn("new Array(count).fill(null)", slots)
        self.assertIn("smartSubmissionSlotsSnapshot", slots)
        self.assertIn("for(var i=0;i<count;i++)slots[i]=source[i]||null", slots)
        self.assertIn("slots.some(Boolean)?Object.freeze(slots):null", slots)

        snapshot = self.html.split("function immutableSlotSnapshot(plan)", 1)[1]
        snapshot = snapshot.split("function materialById", 1)[0]
        self.assertIn("Object.freeze(normalized[item.style].slice())", snapshot)
        self.assertIn("return Object.freeze(snapshot)", snapshot)

        submit = self.html.split(
            "function submitStyle(copy,ratio,stylePlan)", 1,
        )[1].split("function generateAll()", 1)[0]
        self.assertIn("payload.material_upload_ids=materialSlots", submit)
        self.assertNotIn("source_url", submit)
        self.assertNotIn("base64", submit)

    def test_delete_removes_local_material_only_after_owner_bound_delete_succeeds(self):
        delete_call = self.html.split("function deleteMaterialUpload(uploadId)", 1)[1]
        delete_call = delete_call.split("function chooseMaterialFiles", 1)[0]
        self.assertIn("method:'DELETE'", delete_call)
        self.assertIn("JSON.stringify({upload_id:uploadId})", delete_call)

        removal = self.html.split("function moveOrRemoveMaterial(index,action)", 1)[1]
        removal = removal.split("function normalizeStylePlan", 1)[0]
        self.assertLess(
            removal.index("deleteMaterialUpload(removed.upload_id).then"),
            removal.index("smartUploads.splice(currentIndex,1)"),
        )
        failure = removal.split(".catch(function(error)", 1)[1]
        self.assertNotIn("smartUploads.splice", failure)

    def test_unresolved_submission_keeps_key_and_blocks_new_batch_and_release(self):
        self.assertIn("unresolved_submissions:{}", self.html)

        generate = self.html.split("function generateAll()", 1)[1]
        generate = generate.split("function clearBatchUi", 1)[0]
        self.assertLess(
            generate.index("smartBatch.unresolved_submissions[item.style]=true"),
            generate.index("pendingPlans.map(function(item)"),
        )

        submit = self.html.split("function submitStyle(copy,ratio,stylePlan)", 1)[1]
        submit = submit.split("function generateAll()", 1)[0]
        self.assertIn("delete smartBatch.unresolved_submissions[style]", submit)
        self.assertIn("smartBatch.unresolved_submissions[style]=true", submit)
        self.assertIn("smartBatch.keys[style]=newIdempotencyKey(style)", submit)
        success = submit.split(".catch(function(error)", 1)[0]
        self.assertLess(
            success.index("smartBatch.jobs[style]=data.job_id"),
            success.index("delete smartBatch.unresolved_submissions[style]"),
        )
        self.assertLess(
            success.index("delete smartBatch.unresolved_submissions[style]"),
            success.index("persistBatch()"),
        )

        rejected = submit.split(
            "else if(canRotateRejectedSubmission(error)){", 1,
        )[1].split("}else if(explicitlyTerminal)", 1)[0]
        self.assertIn("delete smartBatch.unresolved_submissions[style]", rejected)
        self.assertIn("smartBatch.keys[style]=newIdempotencyKey(style)", rejected)

        terminal = submit.split("else if(explicitlyTerminal){", 1)[1]
        terminal = terminal.split("}else{", 1)[0]
        self.assertIn("delete smartBatch.unresolved_submissions[style]", terminal)
        self.assertNotIn("newIdempotencyKey", terminal)

        uncertain = submit.split("}else{smartBatch.unresolved_submissions", 1)[1]
        uncertain = uncertain.split("persistBatch()", 1)[0]
        self.assertIn("[style]=true", uncertain)
        self.assertNotIn("newIdempotencyKey", uncertain)

        new_batch = self.html.split("function startNewBatch()", 1)[1]
        new_batch = new_batch.split("$('smartModeTab').onclick", 1)[0]
        self.assertLess(
            new_batch.index("if(hasUnresolvedSubmissions())"),
            new_batch.index("deleteMaterialUpload(item.upload_id)"),
        )

        release = self.html.split("function maybeReleaseCompletedMaterials()", 1)[1]
        release = release.split("function completeJob", 1)[0]
        self.assertLess(
            release.index("hasUnresolvedSubmissions()"),
            release.index("deleteMaterialUpload(item.upload_id)"),
        )

    def test_5xx_no_response_and_in_progress_never_rotate_submission_key(self):
        rotate = self.html.split("function canRotateRejectedSubmission(error)", 1)[1]
        rotate = rotate.split("function jobCardId", 1)[0]
        self.assertIn("if(!error||!error.responseReceived)return false", rotate)
        self.assertIn("if(!status||status>=500)return false", rotate)
        self.assertIn("error.code==='idempotency_in_progress'", rotate)
        self.assertIn("if(error.operationTerminal)return true", rotate)
        self.assertIn("if(error.submissionRef||error.jobId)return false", rotate)
        self.assertLess(
            rotate.index("status>=500"),
            rotate.index("status>=400&&status<500"),
        )

        submit = self.html.split("function submitStyle(copy,ratio,stylePlan)", 1)[1]
        submit = submit.split("function generateAll()", 1)[0]
        self.assertIn("explicitlyTerminal", submit)
        self.assertIn("else if(explicitlyTerminal)", submit)
        self.assertIn("else{smartBatch.unresolved_submissions[style]=true;}", submit)

    def test_material_batch_v4_signature_and_legacy_migration_contract(self):
        self.assertIn("saved.version===2||saved.version===3||saved.version===4", self.html)
        self.assertIn("saved.version!==2&&saved.version!==3&&saved.version!==4", self.html)
        self.assertIn("saved.version=4", self.html)
        self.assertIn("material_uploads:serializedMaterials()", self.html)
        self.assertIn("material_slots_by_style", self.html)
        self.assertIn("unresolved_submissions", self.html)

        serialized = self.html.split(
            "function serializedMaterials()", 1,
        )[1].split("function materialControlsLocked", 1)[0]
        self.assertNotIn("smartUploadPreviews", serialized)
        self.assertNotIn("createObjectURL", serialized)

        batch_signature = self.html.split(
            "function currentBatchSignature()", 1,
        )[1].split("function setSmartStatus", 1)[0]
        self.assertIn("if(!smartUploads.length)return currentSignature()", batch_signature)
        self.assertIn("material_slots_by_style:slots", batch_signature)
        self.assertIn("if(!slotMapHasMaterials(slots))return currentSignature()", batch_signature)
        self.assertIn("material_upload_ids:smartUploads.map", batch_signature)

        restore = self.html.split("function restoreBatchOnLoad()", 1)[1]
        restore = restore.split("function batchJobMatches", 1)[0]
        self.assertIn("originalVersion===2||originalVersion===3", restore)
        self.assertIn("signatureHash(currentBatchSignature())!==saved.signature", restore)
        self.assertIn("originalVersion===4?normalizeMaterialSlots", restore)
        self.assertIn("saved.unresolved_submissions", restore)
        self.assertIn("item.expires_at<=now", restore)

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
