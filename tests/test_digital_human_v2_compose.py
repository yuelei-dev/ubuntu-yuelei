# -*- coding: utf-8 -*-
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


@unittest.skipUnless(os.name == "posix", "real FFmpeg compositor regression runs on Linux")
class DigitalHumanV2ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(pathlib.Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import core, digital_human_oneclick, digital_human_v2, video
        cls.core = core
        cls.legacy = digital_human_oneclick
        cls.domain = digital_human_v2
        cls.video = video

    @staticmethod
    def _run(arguments):
        subprocess.run(
            arguments, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=180,
        )

    def test_real_mixed_media_compose_has_opening_ending_audio_and_no_black_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            media = root / "fixtures"
            output = root / "videos"
            media.mkdir()
            presenter = media / "presenter.mp4"
            image = media / "material.png"
            clip = media / "material.mp4"
            self._run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=270x480:rate=10:duration=12",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(presenter),
            ])
            self._run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x276749:s=540x960",
                "-frames:v", "1", str(image),
            ])
            self._run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "testsrc=size=270x480:rate=10:duration=4",
                "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                str(clip),
            ])
            payload = {
                "_job_id": 901, "segment_count": 1, "material_count": 2,
                "gesture_count": 1, "copy": "真实合成检查，开头结尾真人出镜，中间混合图片和视频素材。",
                "video_files": [presenter.relative_to(root).as_posix()],
                "material_files": [image.relative_to(root).as_posix(), clip.relative_to(root).as_posix()],
                "material_types": ["image", "video"],
                "video_job_ids": [101], "material_job_ids": [0, 0],
            }
            patches = (
                mock.patch.object(self.domain, "OUT_DIR", root),
                mock.patch.object(self.legacy, "OUT_DIR", root),
                mock.patch.object(self.core, "OUT_DIR", root),
                mock.patch.object(self.video, "OUT_DIR", root),
                mock.patch.object(self.video, "VIDEO_OUT_DIR", output),
                mock.patch.object(self.video, "burn_subtitle", side_effect=lambda rel, **_kwargs: rel),
            )
            for patcher in patches:
                patcher.start()
            try:
                result = self.domain.compose(payload)
            finally:
                for patcher in reversed(patches):
                    patcher.stop()
            final = root / result["video_file"]
            self.assertTrue(final.is_file())
            self.assertEqual((result["width"], result["height"]), (1080, 1920))
            self.assertAlmostEqual(result["duration"], 12.0, delta=0.75)
            windows = result["presenter_windows"]
            self.assertEqual(len(windows), 2)
            self.assertEqual(windows[0], [0.0, 3.0])
            self.assertAlmostEqual(windows[1][0], result["duration"] - 3.0, delta=0.08)
            self.assertAlmostEqual(windows[1][1], result["duration"], delta=0.08)
            self.assertEqual(result["verification"]["audio_source"], "continuous_presenter_narration")
            self.assertTrue(result["verification"]["audio_stream"])
            self.assertTrue(result["verification"]["black_frame_check"])


if __name__ == "__main__":
    unittest.main()
