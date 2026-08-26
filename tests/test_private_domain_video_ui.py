import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "site/workbench/script.html").read_text(encoding="utf-8")
PAGE = (ROOT / "site/workbench/private-domain-video.html").read_text(encoding="utf-8")


class PrivateDomainVideoUiTests(unittest.TestCase):
    def test_director_entry_is_immediately_after_digital_human(self):
        digital = 'href="digital-human-oneclick.html">🎬 数字人一键生成</a>'
        private = 'href="private-domain-video.html">🎞️ 私域批量成片</a>'
        self.assertIn(digital, SCRIPT)
        self.assertIn(private, SCRIPT)
        self.assertLess(SCRIPT.index(digital), SCRIPT.index(private))
        between = SCRIPT[SCRIPT.index(digital) + len(digital):SCRIPT.index(private)]
        self.assertEqual('\n    <a class="sc-mode-tab" ', between)

    def test_page_reads_fixed_test_server_material_catalog(self):
        self.assertIn("/api/gen/private-domain/materials?limit=300", PAGE)
        self.assertIn("credentials:'same-origin'", PAGE)
        self.assertIn("Authorization:'Bearer __cookie__'", PAGE)
        self.assertIn("/home/ubuntu/material-libraries/huangque-media/", PAGE)
        self.assertNotIn("/api/gen/video/assets?limit=120", PAGE)
        self.assertNotIn("/assets/bgm/private-domain-v1/manifest.json", PAGE)

    def test_random_selection_is_without_sort_comparator_bias(self):
        self.assertIn("function shuffle(items)", PAGE)
        self.assertIn("Math.floor(Math.random()*(i+1))", PAGE)
        self.assertNotIn("sort(function(){return Math.random()", PAGE)
        self.assertIn("function previewBundle()", PAGE)
        self.assertIn("shuffle(imagePool()).slice(0,2)", PAGE)
        self.assertIn("shuffle(videoPool()).slice(0,1)", PAGE)
        self.assertIn("return imagePool().slice(0,6).concat(videoPool().slice(0,6))", PAGE)

    def test_materials_use_same_origin_range_streaming_and_lazy_attachment(self):
        self.assertIn("parsed.origin===location.origin||parsed.protocol==='https:'", PAGE)
        self.assertIn("new IntersectionObserver", PAGE)
        self.assertIn("video.setAttribute('data-src',url)", PAGE)
        self.assertNotIn("response.blob()", PAGE)
        self.assertNotIn("URL.createObjectURL", PAGE)
        self.assertIn("<img loading=\"lazy\"", PAGE)

    def test_review_boundary_is_explicit_and_never_fakes_render_success(self):
        self.assertIn("当前只生成可审计工作流方案", PAGE)
        self.assertIn("不扣点、不自动发布", PAGE)
        self.assertIn("render_and_publish:false", PAGE)
        self.assertNotIn("已生成完成", PAGE)

    def test_four_layouts_and_first_frame_protection_are_visible(self):
        for label in ("数据对比·高转化", "同城圈层·招募", "女性成长·温暖", "品质社交·轻奢"):
            self.assertIn(label, PAGE)
        self.assertIn("图片首帧", PAGE)
        self.assertIn('<img id="previewImage"', PAGE)
        self.assertIn("first_frame:{media_type:'image'", PAGE)
        self.assertNotIn("SAFE_FRAME_SECONDS", PAGE)
        self.assertNotIn("function protectFirstFrame", PAGE)

    def test_deployed_page_fails_closed_when_server_catalogs_are_unavailable(self):
        self.assertIn("LOCAL_PREVIEW=/^(localhost|127\\.0\\.0\\.1|\\[::1\\])$/", PAGE)
        self.assertIn("if(LOCAL_PREVIEW){allAssets=previewAssets.slice()", PAGE)
        self.assertIn("else{allAssets=[];bgms=[];selected=[]", PAGE)
        self.assertIn("已停止生成方案，不会改用其他素材", PAGE)

    def test_skill_batch_contract_is_encoded_in_the_page(self):
        self.assertIn("function splitExact(copy)", PAGE)
        self.assertIn("problem:parts[0]", PAGE)
        self.assertIn("comparison:parts[1]", PAGE)
        self.assertIn("cta:parts[parts.length-1]", PAGE)
        self.assertIn("keyword_scale:'1.18-1.35'", PAGE)
        self.assertIn("images.length<list.length*2||videos.length<list.length", PAGE)
        self.assertIn("usedPaths[item.relative_path]=true", PAGE)
        self.assertIn("usedHashes[item.sha256]=true", PAGE)
        self.assertIn("musicDeck=shuffle(uniqueMaterials(bgms))", PAGE)
        self.assertIn("musicDeck.length<list.length", PAGE)
        self.assertIn("materialAudit(imageA,'problem')", PAGE)
        self.assertIn("materialAudit(video,'comparison+judgment')", PAGE)
        self.assertIn("materialAudit(imageB,'cta')", PAGE)
        self.assertIn("private-domain-batch-material-map.json", PAGE)

    def test_breakdown_hash_initializes_and_tracks_hash_changes(self):
        self.assertIn("location.hash==='#breakdown'?'breakdown'", SCRIPT)
        self.assertIn("window.addEventListener('hashchange',applyHashMode)", SCRIPT)
        self.assertIn("switchMode(initialHashMode||currentMode)", SCRIPT)

    def test_private_page_loads_agent_with_strict_page_marker(self):
        self.assertIn('<body data-page="private_domain_video">', PAGE)
        self.assertIn('src="script-agent.js?v=b1c3f8c3"', PAGE)


if __name__ == "__main__":
    unittest.main()
