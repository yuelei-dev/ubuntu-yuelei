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

    def test_page_reads_real_server_asset_and_bgm_catalogs(self):
        self.assertIn("/api/gen/video/assets?limit=120", PAGE)
        self.assertIn("/assets/bgm/private-domain-v1/manifest.json", PAGE)
        self.assertIn("credentials:'same-origin'", PAGE)
        self.assertIn("Authorization:'Bearer __cookie__'", PAGE)
        self.assertIn("正式站读取服务器资产", PAGE)

    def test_random_selection_is_without_sort_comparator_bias(self):
        self.assertIn("function shuffle(items)", PAGE)
        self.assertIn("Math.floor(Math.random()*(i+1))", PAGE)
        self.assertNotIn("sort(function(){return Math.random()", PAGE)
        self.assertIn("slice(0,Math.min(4,allAssets.length))", PAGE)

    def test_review_boundary_is_explicit_and_never_fakes_render_success(self):
        self.assertIn("付费渲染接口尚未接入", PAGE)
        self.assertIn("不会伪造“已生成”状态", PAGE)
        self.assertIn("不提交付费任务", PAGE)
        self.assertNotIn("已生成完成", PAGE)

    def test_four_layouts_and_first_frame_protection_are_visible(self):
        for label in ("数据对比·高转化", "同城圈层·招募", "女性成长·温暖", "品质社交·轻奢"):
            self.assertIn(label, PAGE)
        self.assertIn("首帧保护", PAGE)
        self.assertIn("SAFE_FRAME_SECONDS=0.12", PAGE)
        self.assertIn("function protectFirstFrame(video,autoplay)", PAGE)
        self.assertIn("video.currentTime=Math.min(SAFE_FRAME_SECONDS", PAGE)
        self.assertIn("video.poster=canvas.toDataURL('image/jpeg',.82)", PAGE)

    def test_deployed_page_fails_closed_when_server_catalogs_are_unavailable(self):
        self.assertIn("LOCAL_PREVIEW=/^(localhost|127\\.0\\.0\\.1|\\[::1\\])$/", PAGE)
        self.assertIn("bgms=LOCAL_PREVIEW?fallbackBgms.slice():[]", PAGE)
        self.assertIn("allAssets=[];selected=[]", PAGE)
        self.assertIn("if(!bgms.length)return toast('BGM 清单没有可用音乐')", PAGE)

    def test_breakdown_hash_initializes_and_tracks_hash_changes(self):
        self.assertIn("location.hash==='#breakdown'?'breakdown'", SCRIPT)
        self.assertIn("window.addEventListener('hashchange',applyHashMode)", SCRIPT)
        self.assertIn("switchMode(initialHashMode||currentMode)", SCRIPT)

    def test_private_page_loads_agent_with_strict_page_marker(self):
        self.assertIn('<body data-page="private_domain_video">', PAGE)
        self.assertIn('src="script-agent.js?v=b1c3f8c3"', PAGE)


if __name__ == "__main__":
    unittest.main()
