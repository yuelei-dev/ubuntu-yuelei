# -*- coding: utf-8 -*-

import hashlib
import json
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import script_video_montage as montage
from content_domains import script_video_render as renderer


class ScriptVideoRenderTests(unittest.TestCase):
    def test_renderer_template_version_matches_bundled_metadata(self):
        meta = json.loads((
            ROOT / "site/assets/one-click/templates/smart-montage-v1/meta.json"
        ).read_text(encoding="utf-8"))
        manifest = json.loads((
            ROOT
            / "site/assets/one-click/templates/smart-montage-v1/template-manifest.json"
        ).read_text(encoding="utf-8"))
        package = json.loads((
            ROOT / "site/assets/one-click/templates/smart-montage-v1/package.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(meta["version"], renderer.TEMPLATE_VERSION)
        self.assertEqual(manifest["template_version"], renderer.TEMPLATE_VERSION)
        self.assertEqual(meta["hyperframesVersion"], renderer.HYPERFRAMES_VERSION)
        self.assertEqual(
            manifest["verification"]["hyperframes_version"],
            renderer.HYPERFRAMES_VERSION,
        )
        self.assertTrue(all(
            "hyperframes@" + renderer.HYPERFRAMES_VERSION in command
            for command in package["scripts"].values()
        ))
        expected_styles = list(montage.STYLE_PROFILES)
        self.assertEqual(expected_styles, meta["styles"])
        self.assertEqual(expected_styles, manifest["input_contract"]["style"])
        self.assertEqual(set(expected_styles), set(renderer._STYLE_LABELS))
        self.assertEqual(set(expected_styles), set(renderer._STYLE_EYEBROWS))
        self.assertEqual(set(expected_styles), set(renderer._STYLE_METRICS))
        self.assertEqual(3, manifest["input_contract"]["max_selected_styles"])
        expected_variants = {
            "%s/%s" % (style, ratio)
            for style in expected_styles
            for ratio in ("16:9", "9:16")
        }
        actual_variants = manifest["verification"]["variants"]
        self.assertEqual(expected_variants, set(actual_variants))
        self.assertEqual(len(expected_variants), len(actual_variants))
        template = (
            ROOT / "site/assets/one-click/templates/smart-montage-v1/index.template.txt"
        ).read_text(encoding="utf-8")
        for style in expected_styles:
            self.assertIn("body.style-" + style, template)
        for style in ("wellness", "neon", "editorial"):
            self.assertIn(".style-%s .photo-stage{overflow:visible}" % style, template)
        self.assertIn(
            ".style-neon .badge{left:auto;right:5%;top:6%", template,
        )
        self.assertIn(
            "body.ratio-portrait.style-neon .clinical-bars{left:6%;bottom:8%",
            template,
        )
        self.assertIn(
            "body.ratio-portrait.style-wellness .content{left:8%;right:8%;"
            "top:64%;bottom:9%",
            template,
        )
        self.assertIn(
            ".style-neon .metric{display:flex;right:5%;bottom:12%;"
            "padding:14px 18px;background:rgba(7,17,29,.94);"
            "border:2px solid #29d3ff",
            template,
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.images = []
        for index in range(3):
            path = self.root / ("material-%d.jpg" % index)
            path.write_bytes(b"image-%d" % index)
            self.images.append(path)

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, style="luxe", ratio="16:9"):
        return {
            "style": style,
            "ratio": ratio,
            "duration_seconds": 12,
            "scenes": [
                {
                    "start_seconds": 0,
                    "duration_seconds": 4,
                    "headline": "第一幕 焕新",
                    "supporting_copy": "从真实需求出发，让美自然发生。",
                },
                {
                    "start_seconds": 4,
                    "duration_seconds": 4,
                    "headline": "第二幕 看见改变",
                    "supporting_copy": "清晰呈现每一个细节与过程。",
                },
                {
                    "start_seconds": 8,
                    "duration_seconds": 4,
                    "headline": "第三幕 自信登场",
                    "supporting_copy": "把理想状态，留在此刻。",
                },
            ],
        }

    def test_normalizes_supported_style_ratio_and_contiguous_timing(self):
        data = renderer.normalize_plan(self.plan("clinic", "9:16"), self.images)
        self.assertEqual("clinic", data["style"])
        self.assertEqual((1080, 1920), (data["width"], data["height"]))
        self.assertEqual([0.0, 4.0, 8.0], [
            scene["start_seconds"] for scene in data["scenes"]
        ])
        self.assertEqual(12, sum(
            scene["duration_seconds"] for scene in data["scenes"]
        ))
        for style in montage.STYLE_PROFILES:
            for ratio, dimensions in (("16:9", (1920, 1080)), ("9:16", (1080, 1920))):
                with self.subTest(style=style, ratio=ratio):
                    variant = renderer.normalize_plan(self.plan(style, ratio), self.images)
                    self.assertEqual(style, variant["style"])
                    self.assertEqual(dimensions, (variant["width"], variant["height"]))

    def test_rejects_bad_plan_bounds_and_timeline(self):
        bad = self.plan()
        bad["duration_seconds"] = 2.9
        with self.assertRaisesRegex(renderer.RenderError, "3-90"):
            renderer.normalize_plan(bad, self.images)

        bad = self.plan()
        bad["scenes"][1]["start_seconds"] = 3.5
        with self.assertRaisesRegex(renderer.RenderError, "连续"):
            renderer.normalize_plan(bad, self.images)

        bad = self.plan()
        bad["style"] = "<script>"
        with self.assertRaisesRegex(renderer.RenderError, "风格"):
            renderer.normalize_plan(bad, self.images)

        bad = self.plan()
        bad["scenes"] = bad["scenes"][:2]
        with self.assertRaisesRegex(renderer.RenderError, "3-20"):
            renderer.normalize_plan(bad, self.images[:2])

    def test_requires_one_distinct_local_image_per_scene(self):
        with self.assertRaisesRegex(renderer.RenderError, "数量"):
            renderer.normalize_plan(self.plan(), self.images[:2])
        with self.assertRaisesRegex(renderer.RenderError, "不同"):
            renderer.normalize_plan(
                self.plan(), [self.images[0], self.images[0], self.images[2]]
            )
        duplicate = self.root / "same-content.jpg"
        duplicate.write_bytes(self.images[0].read_bytes())
        with self.assertRaisesRegex(renderer.RenderError, "不同"):
            renderer.normalize_plan(
                self.plan(), [self.images[0], self.images[1], duplicate]
            )
        with self.assertRaisesRegex(renderer.RenderError, "本地"):
            renderer.normalize_plan(
                self.plan(), [self.images[0], self.images[1], "https://bad.test/a.jpg"]
            )
        with self.assertRaisesRegex(renderer.RenderError, "顺序"):
            renderer.normalize_plan(self.plan(), [
                {"scene_index": 1, "file": self.images[0]},
                {"scene_index": 0, "file": self.images[1]},
                {"scene_index": 2, "file": self.images[2]},
            ])

    def test_prepares_safe_direct_child_clips_and_copies_assets(self):
        voiceover = self.root / "voice.mp3"
        voiceover.write_bytes(b"voice")
        bgm = self.root / "music.mp3"
        bgm.write_bytes(b"music")
        plan = self.plan("luxe")
        plan["scenes"][0]["headline"] = '</h1><script>window.pwned="yes"</script>'
        workspace = self.root / "project"

        data = renderer.prepare_workspace(
            plan, self.images, workspace, voiceover=voiceover, bgm=bgm
        )
        markup = (workspace / "index.html").read_text(encoding="utf-8")
        script = markup.split("const tl =", 1)[1]

        self.assertEqual("luxe", data["style"])
        self.assertIn('body class="style-luxe ratio-landscape"', markup)
        self.assertEqual(3, markup.count('class="clip scene"'))
        self.assertEqual(3, markup.count('\n    <section id="scene-'))
        self.assertIn("&lt;/h1&gt;&lt;script&gt;window.pwned=", markup)
        self.assertNotIn("window.pwned", script)
        self.assertEqual(1, markup.count("gsap.timeline({paused:true})"))
        self.assertIn('window.__timelines["main"] = tl;', markup)
        self.assertIn(
            'tl.set("#scene-02-photo-stage",{opacity:0,x:72},4.000);', markup
        )
        self.assertIn(
            'tl.set("#scene-02-support",{opacity:0,y:24},4.000);', markup
        )
        self.assertNotIn('fromTo("#scene-02-photo-stage"', markup)
        self.assertNotRegex(markup, r"__[A-Z0-9_]+__")
        self.assertFalse((workspace / "index.template.txt").exists())
        self.assertTrue((workspace / "assets/materials/scene-01.jpg").is_file())
        self.assertTrue((workspace / "assets/audio/voiceover.mp3").is_file())
        self.assertTrue((workspace / "assets/audio/bgm.mp3").is_file())
        self.assertTrue((workspace / "assets/fonts/noto-sans-sc-variable.ttf").is_file())

    def test_template_is_self_contained_without_the_legacy_template(self):
        templates = self.root / "templates"
        source = ROOT / "site/assets/one-click/templates/smart-montage-v1"
        shutil.copytree(source, templates / "smart-montage-v1")
        workspace = self.root / "isolated-project"

        with mock.patch.object(renderer, "TEMPLATE_ROOT", templates):
            renderer.prepare_workspace(self.plan(), self.images, workspace)

        self.assertTrue(
            (workspace / "assets/fonts/noto-sans-sc-variable.ttf").is_file()
        )

    def test_bundled_font_is_pinned_variable_font_with_extended_cjk_coverage(self):
        font = (
            ROOT / "site/assets/one-click/templates/smart-montage-v1/assets/fonts"
            / "noto-sans-sc-variable.ttf"
        )
        payload = font.read_bytes()
        self.assertEqual(17772300, len(payload))
        self.assertEqual(
            "a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da",
            hashlib.sha256(payload).hexdigest(),
        )

        table_count = struct.unpack_from(">H", payload, 4)[0]
        tables = {}
        for index in range(table_count):
            offset = 12 + index * 16
            tag, _, table_offset, length = struct.unpack_from(">4sIII", payload, offset)
            tables[tag.decode("ascii")] = (table_offset, length)
        self.assertIn("fvar", tables)
        self.assertIn("cmap", tables)

        cmap_offset, _ = tables["cmap"]
        cmap_count = struct.unpack_from(">H", payload, cmap_offset + 2)[0]
        format12_groups = []
        for index in range(cmap_count):
            record = cmap_offset + 4 + index * 8
            _, _, relative = struct.unpack_from(">HHI", payload, record)
            subtable = cmap_offset + relative
            if struct.unpack_from(">H", payload, subtable)[0] != 12:
                continue
            group_count = struct.unpack_from(">I", payload, subtable + 12)[0]
            format12_groups.extend(
                struct.unpack_from(">III", payload, subtable + 16 + group * 12)[:2]
                for group in range(group_count)
            )
        self.assertTrue(format12_groups)
        for character in "焕颜抗衰祛斑痘肌水光针玻尿酸胶原蛋白颧皱褶莹润":
            codepoint = ord(character)
            self.assertTrue(
                any(start <= codepoint <= end for start, end in format12_groups),
                "bundled font misses U+%04X %s" % (codepoint, character),
            )

    def test_style_motion_is_distinct_and_last_scene_has_no_exit_cover(self):
        expectations = {
            "luxe": ('ease:"sine.inOut"', 'scaleX:0'),
            "pop": ('ease:"back.out(1.65)"', "x:1920"),
            "clinic": ('ease:"circ.out"', '"#scene-01-clinical-bars i"'),
            "wellness": ('scale:.96', 'scale:1.65'),
            "neon": ('x:108', 'ease:"steps(5)"'),
            "editorial": ('rotation:2', 'x:-1920'),
        }
        timelines = set()
        for style, needles in expectations.items():
            with self.subTest(style=style):
                workspace = self.root / ("project-" + style)
                renderer.prepare_workspace(self.plan(style), self.images, workspace)
                markup = (workspace / "index.html").read_text(encoding="utf-8")
                self.assertIn('body class="style-%s ratio-landscape"' % style, markup)
                self.assertIn(needles[0], markup)
                self.assertIn(needles[1], markup)
                self.assertNotIn('"#scene-03-transition"', markup)
                self.assertIn('"#scene-03-image"', markup)
                self.assertIn(
                    'id="scene-03" class="clip scene" data-scene-parity="odd" '
                    'data-start="8.000" '
                    'data-duration="4.033"', markup
                )
                self.assertIn('window.__timelines["main"] = tl;', markup)
                if style == "wellness":
                    self.assertIn(
                        'tl.to("#scene-01-photo-stage",{opacity:1,y:0,scale:1,'
                        'duration:0.480,ease:"sine.out"},0.180);', markup,
                    )
                    self.assertIn(
                        'tl.to("#scene-01-transition",{opacity:1,scale:1.65,'
                        'duration:0.580,ease:"sine.inOut"},3.420);', markup,
                    )
                if style == "neon":
                    self.assertIn(
                        'tl.set("#scene-01-metric",{y:18},0.000);', markup,
                    )
                    self.assertNotIn(
                        '"#scene-01-metric",{opacity:0', markup,
                    )
                self.assertTrue(
                    (workspace / "assets/audio/program-silence.wav").stat().st_size > 44
                )
                data = renderer.normalize_plan(self.plan(style), self.images)
                timelines.add(renderer._timeline_script(data))
        self.assertEqual(len(expectations), len(timelines))

    def test_render_checks_then_renders_with_fixed_cli_and_probes_output(self):
        calls = []
        output = self.root / "finished.mp4"

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if "render" in command:
                target = pathlib.Path(command[command.index("--output") + 1])
                target.write_bytes(b"rendered")
            return subprocess.CompletedProcess(command, 0, b"ok", b"")

        with mock.patch.object(
            renderer.time, "monotonic", side_effect=(100.0, 100.0, 110.0),
        ), mock.patch.object(renderer.media, "probe_media", return_value={
            "duration_ms": 12000,
            "width": 1920,
            "height": 1080,
            "has_audio": True,
            "video_codec": "h264",
            "audio_codec": "aac",
        }) as probe:
            result = renderer.render(
                self.plan(), self.images, output, timeout=77, runner=runner
            )

        self.assertEqual(2, len(calls))
        self.assertEqual("check", calls[0][0][3])
        self.assertEqual("render", calls[1][0][3])
        for command, kwargs in calls:
            self.assertEqual("hyperframes@0.7.101", command[2])
            self.assertTrue(kwargs["check"])
            if pathlib.Path(renderer._DEFAULT_NPX).is_absolute():
                self.assertEqual(
                    str(pathlib.Path(renderer._DEFAULT_NPX).parent),
                    kwargs["env"]["PATH"].split(renderer.os.pathsep, 1)[0],
                )
        self.assertEqual(77, calls[0][1]["timeout"])
        self.assertEqual(67, calls[1][1]["timeout"])
        render_command = calls[1][0]
        self.assertIn("--workers", render_command)
        self.assertEqual("1", render_command[render_command.index("--workers") + 1])
        self.assertIn("--strict", render_command)
        self.assertEqual("h264", result["output"]["video_codec"])
        self.assertEqual("smart-montage-v1", result["template_id"])
        probe.assert_called_once_with(output.resolve())

    def test_check_and_render_share_one_absolute_timeout_budget(self):
        output = self.root / "budget.mp4"
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, b"ok", b"")

        with mock.patch.object(
            renderer.time, "monotonic", side_effect=(100.0, 100.0, 178.0),
        ):
            with self.assertRaisesRegex(renderer.RenderError, "渲染超时"):
                renderer.render(
                    self.plan(), self.images, output, timeout=77, runner=runner,
                )
        self.assertEqual(1, len(calls))
        self.assertIn("check", calls[0][0])

    def test_render_rejects_codec_audio_and_duration_contracts(self):
        output = self.root / "bad.mp4"

        def runner(command, **_kwargs):
            if "render" in command:
                pathlib.Path(command[command.index("--output") + 1]).write_bytes(b"bad")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        reports = (
            ({
                "duration_ms": 12000, "video_codec": "vp9", "has_audio": True,
                "width": 1920, "height": 1080,
            }, "编码"),
            ({
                "duration_ms": 12000, "video_codec": "h264", "has_audio": False,
                "width": 1920, "height": 1080,
            }, "编码"),
            ({
                "duration_ms": 12000, "video_codec": "h264", "has_audio": True,
                "width": 1080, "height": 1920,
            }, "尺寸"),
            ({
                "duration_ms": 20000, "video_codec": "h264", "has_audio": True,
                "width": 1920, "height": 1080,
            }, "时长"),
        )
        for report, message in reports:
            with self.subTest(report=report):
                with mock.patch.object(renderer.media, "probe_media", return_value=report):
                    with self.assertRaisesRegex(renderer.RenderError, message):
                        renderer.render(self.plan(), self.images, output, runner=runner)

    def test_render_rejects_audio_video_stream_start_or_duration_drift(self):
        output = self.root / "desynced.mp4"

        def runner(command, **_kwargs):
            if "render" in command:
                pathlib.Path(command[command.index("--output") + 1]).write_bytes(b"bad")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        base = {
            "duration_ms": 12000, "video_codec": "h264", "audio_codec": "aac",
            "has_audio": True, "width": 1920, "height": 1080,
            "video_start_ms": 0, "audio_start_ms": 0,
            "video_duration_ms": 12000, "audio_duration_ms": 12000,
        }
        cases = (
            ({**base, "audio_start_ms": 51}, "音画起点不同步"),
            ({**base, "audio_duration_ms": 12081}, "音画时长不同步"),
        )
        for report, message in cases:
            with self.subTest(message=message), mock.patch.object(
                renderer.media, "probe_media", return_value=report,
            ):
                with self.assertRaisesRegex(renderer.RenderError, message):
                    renderer.render(self.plan(), self.images, output, runner=runner)

    def test_long_render_cannot_hide_a_truncated_closing_scene_in_tolerance(self):
        output = self.root / "truncated.mp4"
        plan = self.plan()
        plan["duration_seconds"] = 90
        for index, scene in enumerate(plan["scenes"]):
            scene["start_seconds"] = index * 30
            scene["duration_seconds"] = 30

        def runner(command, **_kwargs):
            if "render" in command:
                pathlib.Path(command[command.index("--output") + 1]).write_bytes(b"bad")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with mock.patch.object(renderer.media, "probe_media", return_value={
            "duration_ms": 86100,
            "video_codec": "h264",
            "has_audio": True,
            "width": 1920,
            "height": 1080,
        }):
            with self.assertRaisesRegex(renderer.RenderError, "时长"):
                renderer.render(plan, self.images, output, runner=runner)

    def test_public_render_error_never_includes_runtime_paths(self):
        output = self.root / "private-output.mp4"
        leaked = str(self.root / "private-user-project" / "index.html")

        def runner(command, **_kwargs):
            raise subprocess.CalledProcessError(
                1, command, output=b"", stderr=("failed at " + leaked).encode()
            )

        with self.assertRaises(renderer.RenderError) as rejected:
            renderer.render(self.plan(), self.images, output, runner=runner)
        self.assertNotIn(leaked, str(rejected.exception))
        self.assertEqual("文案成片模板检查未通过", str(rejected.exception))

    def test_render_requires_mp4_output(self):
        with self.assertRaisesRegex(renderer.RenderError, "MP4"):
            renderer.render(self.plan(), self.images, self.root / "finished.webm")


if __name__ == "__main__":
    unittest.main()
