# -*- coding: utf-8 -*-
"""口播「网感·高级红」字幕模板（wg_red）：
原文切句对齐 / 模板 ASS / 参数校验 / ffmpeg·ASR 缺失时优雅跳过（任务照常成功用原片）。"""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from content_domains import video


class WgAlignTests(unittest.TestCase):
    """text 模式：ASR 只借时间轴，字幕文本换口播原文（按句切、按序配对）。"""

    def test_split_sentences_keeps_end_punct(self):
        self.assertEqual(
            video._wg_split_sentences("大家好，今天讲三件事。第一免费！\n第二名额有限？"),
            ["大家好，今天讲三件事。", "第一免费！", "第二名额有限？"])

    def test_align_equal_counts_pairs_in_order(self):
        segs = [(0, 1000, "识别一"), (1000, 2000, "识别二"), (2000, 3000, "识别三")]
        out = video._wg_align_script(segs, "原文第一句。原文第二句。原文第三句。")
        self.assertEqual([t for _, _, t in out], ["原文第一句。", "原文第二句。", "原文第三句。"])
        self.assertEqual([(s, e) for s, e, _ in out], [(0, 1000), (1000, 2000), (2000, 3000)])  # 时间轴不动

    def test_align_more_sentences_distributes_whole_sentences(self):
        # 句多段少：按各段识别字数比例分配整句（不切半句），最后一段吃掉剩余，原文不丢字
        segs = [(0, 1000, "abcd"), (1000, 2000, "abcd")]
        out = video._wg_align_script(segs, "第一句。第二句。第三句。第四句。")
        self.assertEqual([t for _, _, t in out], ["第一句。第二句。", "第三句。第四句。"])

    def test_align_more_segments_merges_tail_timeline(self):
        # 段多句少：多余的尾部时间轴归并进最后一段——ASR 只借时间轴，错字文本永不上屏；
        # 原文不丢字，碎段语音区间仍有正确字幕（E2E 实测末句被切成 5:4 的场景）
        segs = [(0, 1000, "识别甲"), (1000, 2000, "识别乙"), (2000, 3000, "识别丙")]
        out = video._wg_align_script(segs, "只有一句原文。")
        self.assertEqual(out, [(0, 3000, "只有一句原文。")])

    def test_align_whisper_split_last_sentence_merges(self):
        # E2E 回归：whisper 把末句切成两段（5 段:4 句），归并后每句原文一段、时间轴覆盖到尾
        segs = [(0, 1000, "一"), (1000, 2000, "二"), (2000, 3000, "三"), (3000, 4000, "四甲"), (4000, 5000, "四乙")]
        out = video._wg_align_script(segs, "第一句。第二句。第三句。第四句。")
        self.assertEqual([t for _, _, t in out], ["第一句。", "第二句。", "第三句。", "第四句。"])
        self.assertEqual(out[-1][:2], (3000, 5000))   # 末段 = 原第4段 start → 原最后段 end

    def test_align_no_segment_starves_when_sentences_enough(self):
        # E2E 回归：长短不一的段按比例分句时，短段也不能被饿到 0 句（0 句会退回 ASR 错字上屏）
        segs = [(0, 1000, "a"), (1000, 2000, "bbbbbbbbbbbb"), (2000, 3000, "a")]
        out = video._wg_align_script(segs, "第一句。第二句。第三句。第四句。")
        self.assertEqual([t for _, _, t in out], ["第一句。", "第二句。第三句。", "第四句。"])

    def test_align_total_failure_keeps_asr_text(self):
        segs = [(0, 1000, "识别文本")]
        self.assertEqual(video._wg_align_script(segs, ""), segs)       # 空原文
        self.assertEqual(video._wg_align_script(segs, "  \n "), segs)  # 全空白
        self.assertEqual(video._wg_align_script([], "有原文。"), [])    # 空时间轴


class WgAssTests(unittest.TestCase):
    """模板 ASS：颜色/字体/位置必须等于 demo 拍板值（1080x1920 基准）。"""

    def test_style_lines_match_template_config(self):
        ass = video._wg_build_ass([(0, 1000, "你好")], 1080, 1920, "标题一", "标题二")
        self.assertIn("PlayResX: 1080", ass)
        self.assertIn("PlayResY: 1920", ass)
        F = video._WG_FONT
        # demo 拍板值：TitleTop 白字红粗描边 / TitleBox 白字红底条 / Sub 白字黑边底锚
        self.assertIn("Style: TitleTop,%s,84,&H00FFFFFF,&H000000FF,&H000000FF,&H00000000,1,0,0,0,100,100,2,0,1,5,0,8,60,60,130,1" % F, ass)
        self.assertIn("Style: TitleBox,%s,72,&H00FFFFFF,&H000000FF,&H00000000,&H000000FF,1,0,0,0,100,100,2,0,3,14,0,8,60,60,268,1" % F, ass)
        self.assertIn("Style: Sub,%s,56,&H00FFFFFF,&H000000FF,&H00141414,&H00000000,1,0,0,0,100,100,1,0,1,3.2,0,2,50,50,120,1" % F, ass)

    def test_title_events_span_full_duration_and_box_is_optional(self):
        ass = video._wg_build_ass([(0, 1000, "你好")], 1080, 1920, "科技焕肤体验官", "0元招募")
        # 标题全程展示：0 → 最后一段 end+300ms
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.30,TitleTop,,0,0,0,,科技焕肤体验官", ass)
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.30,TitleBox,,0,0,0,,0元招募", ass)
        # 第二行可选：没有就不渲染，不留空标题条
        ass_no_box = video._wg_build_ass([(0, 1000, "你好")], 1080, 1920, "只要一行", "")
        self.assertIn("TitleTop", ass_no_box)
        self.assertNotIn("Dialogue: 0,0:00:00.00,0:00:01.30,TitleBox", ass_no_box)

    def test_keyword_inline_red(self):
        ass = video._wg_build_ass([(0, 1000, "今天免费体验活动开始了")], 1080, 1920, "", "")
        self.assertIn("{\\c&H00303BFF&}免费{\\c&H00FFFFFF&}", ass)  # 高级红 FF3B30

    def test_over_13_chars_splits_two_lines_at_mid_punct(self):
        ass = video._wg_build_ass([(0, 1000, "今天我们的活动力度非常大，大家一定要来现场看看")], 1080, 1920, "", "")
        self.assertIn("非常大，\\N大家一定要来现场看看", ass)   # 标点留在上行尾

    def test_short_text_stays_one_line(self):
        ass = video._wg_build_ass([(0, 1000, "短短一句话")], 1080, 1920, "", "")
        self.assertNotIn("\\N", ass)

    def test_ass_injection_is_escaped_but_own_tags_survive(self):
        # 用户文案里的 {} 必须先转义（防 ASS 覆盖块注入），我们自己的标红 tag 在转义后插入
        ass = video._wg_build_ass([(0, 1000, "免费{偷梁换柱}哦")], 1080, 1920, "", "")
        self.assertIn("(偷梁换柱)", ass)
        self.assertIn("{\\c&H00303BFF&}免费{\\c&H00FFFFFF&}", ass)

    def test_sub_event_ends_exactly_no_tail(self):
        # 结束即走下句即来：不留尾延，防相邻两句短暂同屏
        ass = video._wg_build_ass([(0, 1000, "你好")], 1080, 1920, "", "")
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.00,Sub,,0,0,0,,你好", ass)


class WgTitlesTests(unittest.TestCase):
    def test_title_top_distilled_from_first_sentence_max_20(self):
        top, box = video._wg_titles("科技焕肤体验官招募活动现在开始啦。0元招募限20名。", [])
        self.assertEqual(top, "科技焕肤体验官招募活动现在开始啦")   # 去标点，≤20 字不截断
        self.assertEqual(box, "0元招募限20名")           # 次句 ≤10 字 → 第二行

    def test_title_skips_greeting_sentence(self):
        # 首句是寒暄（姐妹们好呀～）要跳过，标题要信息句；次句 >10 字截断难看，宁可不渲染
        top, box = video._wg_titles(
            "姐妹们好呀～仙颜美容本月科技焕肤体验官招募正式开启！首批 20 个名额，0 元体验德国进口冷光嫩肤。", [])
        self.assertEqual(top, "仙颜美容本月科技焕肤体验官招募正式开启")
        self.assertEqual(box, "")

    def test_title_top_splits_two_lines_when_over_10(self):
        ass = video._wg_build_ass([(0, 1000, "你好")], 1080, 1920, "仙颜美容本月科技焕肤体验官招募正式开启", "")
        self.assertIn("仙颜美容本月科技焕肤\\N体验官招募正式开启", ass)   # 长标题硬劈双行，字号不变

    def test_title_box_absent_when_single_sentence(self):
        top, box = video._wg_titles("只有一句话。", [])
        self.assertEqual((top, box), ("只有一句话", ""))

    def test_audio_mode_falls_back_to_asr_text(self):
        top, box = video._wg_titles(None, [(0, 1000, "识别的第一句。"), (1000, 2000, "识别的第二句。")])
        self.assertEqual((top, box), ("识别的第一句", "识别的第二句"))


class WgStyleRegistryTests(unittest.TestCase):
    def test_wg_red_accepted_and_old_three_styles_untouched(self):
        # gen_video 的 subtitle_style 校验走 _SUB_STYLES 词表：wg_red 必须在册
        self.assertIn("wg_red", video._SUB_STYLES)
        # 旧三样式一个字都不许动（change-detector，防误伤旧行为）
        self.assertEqual(video._SUB_STYLES["white"], {"fs": 0.052, "primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H00000000", "border": 1, "ow": 3.0, "shadow": 1, "mv": 0.060})
        self.assertEqual(video._SUB_STYLES["variety"], {"fs": 0.066, "primary": "&H0000E5FF", "outline": "&H00202020", "back": "&H00000000", "border": 1, "ow": 4.0, "shadow": 1, "mv": 0.072})
        self.assertEqual(video._SUB_STYLES["bar"], {"fs": 0.050, "primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H80101010", "border": 3, "ow": 8.0, "shadow": 0, "mv": 0.050})


class WgAsrSubprocessTests(unittest.TestCase):
    def _fake_run_ok(self, segs):
        def fake_run(cmd, **kwargs):
            pathlib.Path(cmd[-1]).write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
            return None
        return fake_run

    def test_subprocess_wraps_systemd_scope_and_parses_json(self):
        with tempfile.TemporaryDirectory() as td:
            out_json = pathlib.Path(td) / "asr.json"
            with patch.object(video.os, "geteuid", return_value=0), \
                 patch.object(video.shutil, "which", side_effect=lambda n: "/usr/bin/systemd-run" if n == "systemd-run" else None), \
                 patch.object(video.subprocess, "run", side_effect=self._fake_run_ok(
                     [{"start": 0, "end": 1000, "text": "你好"}, {"start": 1000, "end": 2000, "text": "  "}])) as run:
                segs = video._run_talking_asr(pathlib.Path(td) / "a.wav", out_json)
        self.assertEqual(segs, [(0, 1000, "你好")])   # 空文本段被丢掉
        cmd, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(cmd[:4], ["systemd-run", "--scope", "-q", "-p"])
        self.assertIn("MemoryMax=1500M", cmd)         # 3.4GB 小机必须限内存
        self.assertIn("content_domains.talking_asr_cli", cmd)
        self.assertEqual(kwargs["env"]["OMP_NUM_THREADS"], "2")
        self.assertTrue(kwargs["cwd"].endswith("server"))  # python -m 靠 cwd 找到 content_domains 包

    def test_subprocess_falls_back_to_nice_without_systemd(self):
        with tempfile.TemporaryDirectory() as td:
            out_json = pathlib.Path(td) / "asr.json"
            with patch.object(video.os, "geteuid", return_value=1000), \
                 patch.object(video.shutil, "which", side_effect=lambda n: "/usr/bin/nice" if n == "nice" else None), \
                 patch.object(video.subprocess, "run", side_effect=self._fake_run_ok([])) as run:
                video._run_talking_asr(pathlib.Path(td) / "a.wav", out_json)
        self.assertEqual(run.call_args.args[0][:3], ["nice", "-n", "19"])

    def test_subprocess_non_root_uses_nice_even_with_systemd(self):
        # 线上 E2E 实测：content 服务跑在 ubuntu 用户下，systemd-run --scope 要 polkit 交互认证必挂
        # （"Interactive authentication required"）；非 root 即使有 systemd-run 也必须走 nice。
        with tempfile.TemporaryDirectory() as td:
            out_json = pathlib.Path(td) / "asr.json"
            with patch.object(video.os, "geteuid", return_value=1000), \
                 patch.object(video.shutil, "which", side_effect=lambda n: "/usr/bin/" + n if n in ("systemd-run", "nice") else None), \
                 patch.object(video.subprocess, "run", side_effect=self._fake_run_ok([])) as run:
                video._run_talking_asr(pathlib.Path(td) / "a.wav", out_json)
        self.assertEqual(run.call_args.args[0][:3], ["nice", "-n", "19"])

    def test_asr_python_missing_raises_valueerror(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(video.subprocess, "run", side_effect=FileNotFoundError("no such python")):
                with self.assertRaisesRegex(ValueError, "ASR 解释器不存在"):
                    video._run_talking_asr(pathlib.Path(td) / "a.wav", pathlib.Path(td) / "asr.json")


class WgGracefulSkipTests(unittest.TestCase):
    """ffmpeg/ASR 缺失：字幕失败绝不让口播任务失败 —— 记 subtitle_error、返回未加模板的原片。"""

    def test_burn_subtitle_ffmpeg_missing_raises_valueerror(self):
        with patch.object(video, "_resolve_out_file", return_value=pathlib.Path("fake.mp4")), \
             patch.object(video.subprocess, "run", side_effect=FileNotFoundError("ffmpeg")):
            with self.assertRaisesRegex(ValueError, "ffmpeg"):
                video.burn_subtitle("video/fake.mp4", style_key="wg_red")

    def test_burn_subtitle_asr_missing_raises_valueerror(self):
        with patch.object(video, "_resolve_out_file", return_value=pathlib.Path("fake.mp4")), \
             patch.object(video.subprocess, "run", side_effect=[None, FileNotFoundError("no python")]):
            with self.assertRaisesRegex(ValueError, "ASR 解释器不存在"):
                video.burn_subtitle("video/fake.mp4", style_key="wg_red")

    def test_gen_video_returns_plain_video_when_burn_fails(self):
        with patch.object(video, "HEYGEN_API_KEY", "k"), \
             patch.object(video, "_save_data_file", return_value="video/img.jpg"), \
             patch.object(video, "gen_audio", return_value={"file": "audio/a.mp3", "url": "/api/gen/file/audio/a.mp3"}), \
             patch.object(video, "generate_heygen_video", return_value={
                 "video_file": "video/plain.mp4", "video_url": "/api/gen/file/video/plain.mp4", "video_id": "v1"}), \
             patch.object(video, "_resolve_out_file", return_value=pathlib.Path("fake.mp4")), \
             patch.object(video.subprocess, "run", side_effect=FileNotFoundError("ffmpeg")):
            result = video.gen_video({
                "mode": "text", "text": "大家好，今天讲三件事。", "voice": "v",
                "image_data": "data:image/png;base64,eA==",
                "subtitle": True, "subtitle_style": "wg_red",
            })
        self.assertEqual(result["video_file"], "video/plain.mp4")   # 原片照常交付
        self.assertFalse(result["subtitle"])
        self.assertIn("ffmpeg", result["subtitle_error"])


class WgFrontendTests(unittest.TestCase):
    VIDEO_HTML = (pathlib.Path(__file__).resolve().parents[1] / "site/workbench/video.html").read_text(encoding="utf-8")

    def test_video_html_has_wg_red_button(self):
        self.assertIn('data-substyle="wg_red"', self.VIDEO_HTML)
        self.assertIn("网感·高级红", self.VIDEO_HTML)

    def test_substyle_cards_are_image_picker(self):
        # 选择器图片化（开拍形态）：4 个样式各一张真实渲染的预览缩略图，按钮仍是 .seg button 沿用既有绑定
        import re
        for key in ("white", "variety", "bar", "wg_red"):
            self.assertRegex(self.VIDEO_HTML,
                             r'<button class="substyle-card[^"]*" data-substyle="%s"><img src="assets/tpl-preview/%s\.png"' % (key, key),
                             "缺样式卡片或预览图: " + key)
            img = pathlib.Path(__file__).resolve().parents[1] / ("site/workbench/assets/tpl-preview/%s.png" % key)
            self.assertTrue(img.is_file() and img.stat().st_size > 10000, "预览图缺失或过小: " + str(img))
        style_row = self.VIDEO_HTML.split('id="subtitleStyleRow"')[1][:600]
        self.assertIn("seg substyle-cards", style_row)
        self.assertIn(".seg button.substyle-card", self.VIDEO_HTML)   # 卡片样式覆盖 .seg button 基础样式


if __name__ == "__main__":
    unittest.main()
