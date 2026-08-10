# -*- coding: utf-8 -*-
"""首页旧月球 Hero 被视频叙事替换后的回归哨兵。

本文件保留原测试入口，避免通过删除测试制造通过；断言同步到本次明确的
产品替换：旧 WebGL 月球不再挂载，双视频 Hero 与全页粒子叙事必须存在。
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LegacyMoonReplacementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "site/index.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "site/homepage.css").read_text(encoding="utf-8")

    def test_legacy_webgl_moon_is_not_mounted(self):
        self.assertNotIn("hero-moon", self.html)
        self.assertNotIn("moon-3d", self.html)
        self.assertNotIn("moon3d.js", self.html)

    def test_video_hero_replaces_the_moon(self):
        self.assertIn('<div class="hero-media" aria-hidden="true">', self.html)
        self.assertIn("hero-banner-monochrome-eye.mp4", self.html)
        self.assertIn("hero-banner-ancient-courtyard.mp4", self.html)

    def test_scroll_driven_particle_story_remains_available(self):
        self.assertIn("data-particle-story", self.html)
        self.assertIn("homepage-particles.js", self.html)
        self.assertIn(".page-particle-stage{position:fixed", self.css)


if __name__ == "__main__":
    unittest.main()
