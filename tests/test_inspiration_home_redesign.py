import re
import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "site/workbench/inspiration.html").read_text(encoding="utf-8")
CSS = (ROOT / "site/workbench/inspiration-home.css").read_text(encoding="utf-8")
SHELL = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
DOC = (ROOT / "docs/workbench-home-visual-redesign-20260808.md").read_text(encoding="utf-8")


def webp_size(path):
    data = path.read_bytes()[:32]
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise AssertionError("hero asset is not WebP")
    if data[12:16] == b"VP8X":
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    if data[12:16] == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            raise AssertionError("invalid VP8 header")
        width, height = struct.unpack_from("<HH", data, 26)
        return width & 0x3FFF, height & 0x3FFF
    raise AssertionError("unsupported WebP encoding")


class WorkbenchHomeStructureTests(unittest.TestCase):
    def test_logged_in_home_route_remains_inspiration(self):
        self.assertIn('data-active="inspiration"', PAGE)
        self.assertIn('cloud-shell.js', PAGE)

    def test_primary_hero_has_one_heading_and_real_route(self):
        self.assertIn('<h1 class="ip12-title">', PAGE)
        self.assertIn('href="ip12.html" class="ip12-hero', PAGE)
        self.assertIn('开始建立品牌 IP', PAGE)

    def test_four_shortcuts_use_existing_workbench_routes(self):
        shortcuts = re.findall(r'<a href="([^"]+)" class="cap-card', PAGE)
        self.assertEqual(shortcuts, ["banana.html", "audio.html", "script.html", "video.html"])

    def test_search_categories_and_gallery_contracts_remain(self):
        for token in ('id="caseSearch"', 'id="chips"', 'id="caseGrid"', 'id="caseTotal"'):
            self.assertIn(token, PAGE)

    def test_keyboard_skip_link_targets_gallery(self):
        self.assertIn('class="workbench-skip" href="#caseGrid"', PAGE)


class WorkbenchHomeVisualTests(unittest.TestCase):
    def test_original_hero_asset_is_optimized_webp(self):
        path = ROOT / "site/assets/home/huangque-creator-hero.webp"
        self.assertTrue(path.is_file())
        self.assertEqual(webp_size(path), (1536, 1024))
        self.assertLess(path.stat().st_size, 300_000)

    def test_responsive_breakpoints_cover_tablet_and_mobile(self):
        self.assertIn('@media (max-width:1200px)', CSS)
        self.assertIn('@media (max-width:899px)', CSS)
        self.assertIn('@media (max-width:620px)', CSS)
        self.assertIn('.masonry{column-count:1', CSS)

    def test_motion_and_resource_fallbacks_are_present(self):
        self.assertIn('prefers-reduced-motion:reduce', CSS)
        self.assertIn('data-save-data="true"', CSS)
        self.assertIn('data-low-performance="true"', CSS)
        self.assertIn("navigator.deviceMemory", PAGE)
        self.assertIn("navigator.hardwareConcurrency", PAGE)

    def test_focus_states_cover_primary_interactions(self):
        for selector in ('.ip12-hero:focus-visible', '.cap-card:focus-visible', '.chip:focus-visible'):
            self.assertIn(selector, CSS)


class WorkbenchShellContractTests(unittest.TestCase):
    def test_current_navigation_item_is_semantic(self):
        self.assertIn('aria-current="\'+(on?\'page\':\'false\')+\'"', SHELL)

    def test_topbar_uses_real_product_channel_count_not_reference_number(self):
        self.assertIn('13</span> 个创作渠道', SHELL)
        self.assertNotIn('34</span> 个 Bot 在线', SHELL)

    def test_mobile_topbar_has_stable_selectors(self):
        for selector in ('hq-top-actions', 'hq-top-points', 'hq-top-detail'):
            self.assertIn(selector, SHELL)
            self.assertIn(selector, CSS)

    def test_mobile_drawer_reports_state_and_closes_with_escape(self):
        self.assertIn("burger.setAttribute('aria-label',open?'关闭导航':'打开导航')", SHELL)
        self.assertIn("if(open&&event.key==='Escape')", SHELL)
        self.assertIn('if(firstNavLink) firstNavLink.focus()', SHELL)

    def test_design_record_covers_license_boundaries(self):
        for marker in ('AGPL-3.0', 'Apache-2.0', '未复制代码', '不新增框架'):
            self.assertIn(marker, DOC)


if __name__ == "__main__":
    unittest.main()
