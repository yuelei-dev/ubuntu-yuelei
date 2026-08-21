import unittest
from pathlib import Path


class DigitalHumanV2UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (Path(__file__).resolve().parents[1] / "site" / "workbench" /
                    "digital-human-oneclick.html").read_text(encoding="utf-8")

    def test_page_offers_text_voice_and_full_audio_modes(self):
        self.assertIn('name="narrationMode" value="text"', self.page)
        self.assertIn('name="narrationMode" value="audio"', self.page)
        self.assertIn('id="driveAudio"', self.page)
        self.assertIn("/api/gen/digital-human-v2/audio-upload", self.page)
        self.assertIn("不需要再输入文案或选择音色", self.page)

    def test_authorized_portrait_directly_drives_presenter_segments(self):
        self.assertIn("原图将直接用于全部真人出镜片段", self.page)
        self.assertIn("reference_images:photoData?[photoData]:[]", self.page)
        self.assertIn("使用原图生成数字人口播", self.page)
        self.assertNotIn("手势形象数量", self.page)
        self.assertNotIn("plan.gestures", self.page)
        self.assertNotIn("item.gesture_index", self.page)
        self.assertNotIn("function generateImages", self.page)
        self.assertNotIn('data-step="gestures"', self.page)

    def test_duration_driven_presenter_and_material_contract_is_visible(self):
        self.assertIn("每隔约 20–30 秒全屏真人出镜", self.page)
        self.assertIn("开头、结尾和每隔 20–30 秒真人全屏", self.page)
        self.assertIn("digital_human_material_v2", self.page)
        self.assertIn("/api/gen/digital-human-v2/plan", self.page)
        self.assertIn("/api/gen/digital-human-v2/consent", self.page)
        self.assertIn("/api/gen/digital-human-v2/material-resolve", self.page)

    def test_material_resolve_retries_only_network_disconnects(self):
        self.assertIn("function materialResolveNetworkError(error)", self.page)
        self.assertIn("delays=[1000,3000]", self.page)
        self.assertIn(
            "if(url==='/api/gen/digital-human-v2/material-resolve')"
            "return requestMaterialResolve(url,options)",
            self.page,
        )
        self.assertIn("素材匹配网络重试", self.page)
        self.assertIn(
            "素材匹配网络连接中断，系统自动重试后仍未恢复，"
            "请点击继续上次未完成的生成",
            self.page,
        )


    def test_material_priority_and_no_visible_source_label(self):
        self.assertIn(
            "客户参考图（最高） → 飞书素材库 → 全网公开可用素材 → AI 补缺",
            self.page,
        )
        self.assertIn("有参考图的镜头将优先按参考图生成，不再查询飞书或全网素材", self.page)
        self.assertIn("if(customerUploads[index])return aiFallback(item,index)", self.page)
        self.assertIn("body.reference_upload_ids=[customerUploads[index].upload_id]", self.page)
        self.assertIn("最终视频不显示素材来源标签", self.page)
        self.assertNotIn("CONCEPT / AI FILL", self.page)

    def test_ai_material_fallback_uses_seedream_standard(self):
        materials = self.page[
            self.page.index("function generateMaterials(epoch)"):
            self.page.index("function generateTalking(voiceKey,epoch)")
        ]
        for marker in (
            "provider:'seedream'", "variant:'std'", "quality:'std'",
            "count:1", "ratio:'9:16'",
        ):
            self.assertIn(marker, materials)
        self.assertNotIn("provider:'banana'", materials)
        self.assertNotIn("model:'nb2'", materials)


    def test_completed_video_resets_atomically_before_next_analysis(self):
        for marker in (
            "function prepareNextRun(options)",
            "function analyze(){prepareNextRun();",
            "$('analyze').onclick=function(){prepareNextRun();if(DigitalHumanMaterialState.canAnalyze",
            "$('photo').onchange=function(){prepareNextRun();",
            "createLatestVoiceRequestGuard",
            "prepareNextRun({refreshVoices:false});voiceLoadEpoch++;voiceRequestGuard.invalidate();renderVoiceMode();loadVoiceSources()",
            "if(options.refreshVoices!==false)loadVoiceSources()",
            "$('script').oninput=function(){prepareNextRun();",
            "state.phase='complete';state.plan=null;state.consent=null;plan=null",
            "photoData='';customerUploads=[];customerMaterialBusy=false",
            "$('result').className='result'",
            "$('analyze').disabled=false;$('start').disabled=true",
            "state.voiceMode='existing';state.voiceKey=previousVoiceKey",
            "成片已完成；重新上传或修改资料后可继续分析下一条",
        ):
            self.assertIn(marker, self.page)
if __name__ == "__main__":
    unittest.main()
