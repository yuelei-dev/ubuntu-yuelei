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


if __name__ == "__main__":
    unittest.main()
