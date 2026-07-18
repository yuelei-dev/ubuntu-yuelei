# -*- coding: utf-8 -*-
"""文字快剪 kuaijian 域：提交校验 / 计费档位 / 文稿聚合 / 剪辑段计算 / 登记接线。"""
import base64
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import func_names  # noqa: E402
from content_domains import assets_store, feature_flags, kuaijian, points, registry  # noqa: E402


def _data_url(mime="video/mp4", payload=b"\x00" * 64):
    return "data:%s;base64,%s" % (mime, base64.b64encode(payload).decode())


class ValidateTranscribeTests(unittest.TestCase):
    def test_rejects_non_data_url(self):
        with self.assertRaises(ValueError):
            kuaijian.validate_kuaijian_payload({"op": "transcribe", "video_data": "http://x/v.mp4"}, "u1")

    def test_rejects_bad_mime(self):
        with self.assertRaises(ValueError):
            kuaijian.validate_kuaijian_payload({"op": "transcribe", "video_data": _data_url("image/png")}, "u1")

    def test_rejects_bad_base64(self):
        with self.assertRaises(ValueError):
            kuaijian.validate_kuaijian_payload({"op": "transcribe", "video_data": "data:video/mp4;base64,!!!"}, "u1")

    def test_rejects_oversize(self):
        big = _data_url(payload=b"\x00" * (kuaijian.MAX_VIDEO_BYTES + 1))
        with self.assertRaises(ValueError):
            kuaijian.validate_kuaijian_payload({"op": "transcribe", "video_data": big}, "u1")

    @mock.patch.object(kuaijian, "_probe_duration", return_value=30.0)
    def test_ok_writes_duration_back(self, _m):
        body = kuaijian.validate_kuaijian_payload(
            {"op": "transcribe", "video_data": _data_url(), "filename": "测试片"}, "u1")
        self.assertEqual(body["op"], "transcribe")
        self.assertEqual(body["duration"], 30.0)
        self.assertEqual(body["filename"], "测试片")

    @mock.patch.object(kuaijian, "_probe_duration", return_value=301.0)
    def test_rejects_over_300s(self, _m):
        with self.assertRaises(ValueError):
            kuaijian.validate_kuaijian_payload({"op": "transcribe", "video_data": _data_url()}, "u1")

    @mock.patch.object(kuaijian, "_probe_duration", return_value=0.5)
    def test_rejects_too_short(self, _m):
        with self.assertRaises(ValueError):
            kuaijian.validate_kuaijian_payload({"op": "transcribe", "video_data": _data_url()}, "u1")


class ValidateCutTests(unittest.TestCase):
    def _src(self, **kw):
        d = {"op": "transcribe", "duration": 120.0, "filename": "源片",
             "sentences": [{"i": i, "text": "句%d" % i, "start": i * 1000, "end": i * 1000 + 800}
                           for i in range(4)]}
        d.update(kw)
        return d

    @mock.patch.object(kuaijian, "_load_transcribe_result")
    def test_ok(self, m):
        m.return_value = self._src()
        body = kuaijian.validate_kuaijian_payload(
            {"op": "cut", "source_job_id": 7, "deletes": [2, 1], "tighten_pauses": True}, "u1")
        self.assertEqual(body["deletes"], [1, 2])          # 去重排序
        self.assertEqual(body["duration"], 120.0)          # 计费时长从源 job 落定
        self.assertEqual(body["filename"], "源片")
        self.assertTrue(body["tighten_pauses"])

    @mock.patch.object(kuaijian, "_load_transcribe_result")
    def test_rejects_out_of_range_delete(self, m):
        m.return_value = self._src()
        with self.assertRaises(ValueError):
            kuaijian.validate_kuaijian_payload({"op": "cut", "source_job_id": 7, "deletes": [9]}, "u1")

    @mock.patch.object(kuaijian, "_load_transcribe_result")
    def test_rejects_delete_all(self, m):
        m.return_value = self._src()
        with self.assertRaises(ValueError):
            kuaijian.validate_kuaijian_payload(
                {"op": "cut", "source_job_id": 7, "deletes": [0, 1, 2, 3]}, "u1")

    def test_rejects_bad_op(self):
        with self.assertRaises(ValueError):
            kuaijian.validate_kuaijian_payload({"op": "x"}, "u1")

    def test_rejects_missing_source(self):
        with self.assertRaises(ValueError):
            kuaijian.validate_kuaijian_payload({"op": "cut"}, "u1")


class CostTests(unittest.TestCase):
    def test_transcribe_cost(self):
        self.assertEqual(points.cost_of("kuaijian", {"op": "transcribe"}), 2)

    def test_cut_tiers(self):
        for dur, want in [(1, 5), (59, 5), (60, 5), (61, 10), (120, 10), (180, 10), (181, 15), (300, 15)]:
            self.assertEqual(points.cost_of("kuaijian", {"op": "cut", "duration": dur}), want, "dur=%s" % dur)

    def test_core_cost_fallback_is_not_free(self):
        # core.py 的 ⚠️：新增 kind 忘登记 COST 就是免费。kuaijian 必须登记非 0。
        from content_domains import core
        self.assertGreater(core.COST.get("kuaijian", 0), 0)


class AggregateTests(unittest.TestCase):
    TEXT = "你好，世界。再见！"
    TS = [[0, 200], [200, 400], [500, 700], [700, 900], [1000, 1200], [1200, 1400]]

    def test_sentences(self):
        sents = kuaijian._aggregate_sentences(self.TEXT, self.TS)
        self.assertEqual(len(sents), 2)
        self.assertEqual(sents[0]["text"], "你好，世界。")
        self.assertEqual((sents[0]["start"], sents[0]["end"]), (0, 900))
        self.assertEqual(sents[1]["text"], "再见！")
        self.assertEqual((sents[1]["start"], sents[1]["end"]), (1000, 1400))
        self.assertEqual([s["i"] for s in sents], [0, 1])

    def test_missing_tail_ts_interpolated(self):
        # paraformer 实测句尾末字掉戳（8s 样片 51 字 50 戳）：缺戳字前字 end+120ms 插值
        sents = kuaijian._aggregate_sentences(self.TEXT, self.TS[:5])
        self.assertEqual(len(sents), 2)
        self.assertEqual((sents[1]["start"], sents[1]["end"]), (1000, 1440))

    def test_sentence_pauses(self):
        sents = kuaijian._aggregate_sentences(self.TEXT, self.TS)
        self.assertEqual(kuaijian._sentence_pauses(sents), [])  # 900→1000 只有 100ms，不达 300ms 门槛
        sents2 = [{"i": 0, "text": "a", "start": 0, "end": 1000},
                  {"i": 1, "text": "b", "start": 1500, "end": 2000}]
        self.assertEqual(kuaijian._sentence_pauses(sents2), [(0, 500)])


class KeepSegmentsTests(unittest.TestCase):
    def _sents(self):
        return [
            {"i": 0, "text": "a", "start": 1000, "end": 2000},
            {"i": 1, "text": "b", "start": 3000, "end": 4000},
            {"i": 2, "text": "c", "start": 5000, "end": 6000},
        ]

    def test_no_delete_keeps_all(self):
        segs = kuaijian._build_keep_segments([], self._sents(), set(), False, 10.0)
        self.assertEqual(segs, [[0.0, 10.0]])

    def test_delete_middle_pads_into_gap(self):
        # 删句1：[2970,4030) 挖掉，护边吃空隙不伤邻句
        segs = kuaijian._build_keep_segments([], self._sents(), {1}, False, 10.0)
        self.assertEqual(segs, [[0.0, 2.97], [4.03, 10.0]])

    def test_delete_first_clamps_to_next_kept(self):
        # 删句0：护边 2030，不超过句1 的 3000
        segs = kuaijian._build_keep_segments([], self._sents(), {0}, False, 10.0)
        self.assertEqual(segs, [[0.0, 0.97], [2.03, 10.0]])

    def test_delete_adjacent(self):
        segs = kuaijian._build_keep_segments([], self._sents(), {0, 1}, False, 10.0)
        self.assertEqual(segs, [[0.0, 0.97], [4.03, 10.0]])

    def test_delete_non_adjacent(self):
        # {0,2} 不连续：句 0/2 各自独立挖，句 1 完整保留
        segs = kuaijian._build_keep_segments([], self._sents(), {0, 2}, False, 10.0)
        self.assertEqual(segs, [[0.0, 0.97], [2.03, 4.97], [6.03, 10.0]])

    def test_tighten_pauses(self):
        chars = [("你", 1000, 1200), ("好", 2000, 2200)]   # 字间 800ms 停顿
        segs = kuaijian._build_keep_segments(chars, self._sents(), set(), True, 10.0)
        # 挖掉中段 [1275,1925)，两侧各留 75ms 气口
        self.assertEqual(segs, [[0.0, 1.275], [1.925, 10.0]])

    def test_short_pause_untouched(self):
        chars = [("你", 1000, 1200), ("好", 1400, 1600)]   # 200ms，不达 300ms 门槛
        segs = kuaijian._build_keep_segments(chars, self._sents(), set(), True, 10.0)
        self.assertEqual(segs, [[0.0, 10.0]])

    def test_out_of_range_delete_ignored(self):
        segs = kuaijian._build_keep_segments([], self._sents(), {9}, False, 10.0)
        self.assertEqual(segs, [[0.0, 10.0]])


class RegistrationTests(unittest.TestCase):
    def test_handler_registered(self):
        self.assertIn("kuaijian", registry.HANDLERS)

    def test_func_name(self):
        self.assertEqual(func_names.func_name("kuaijian", {}), "文字快剪")
        self.assertEqual(func_names.path_func("/api/gen/kuaijian"), "文字快剪 · 提交")

    def test_feature_flag_catalog(self):
        keys = {item["key"] for item in feature_flags.CATALOG}
        self.assertIn("kuaijian", keys)

    def test_assets_kind_registered(self):
        self.assertIn("kuaijian", assets_store.KINDS)

    def test_assets_project_skips_transcribe(self):
        self.assertIsNone(assets_store._project("kuaijian", {"op": "transcribe"}))

    def test_assets_project_cut(self):
        proj = assets_store._project("kuaijian", {
            "op": "cut", "filename": "测试源片", "url": "https://cos.example/x.mp4",
            "file": "kuaijian/9/out.mp4", "duration_before": 152.6, "duration_after": 141.6,
            "removed_s": 11.0, "deleted_sentences": 2, "tighten_pauses": True, "source_job_id": 3})
        title, file, url, meta = proj
        self.assertEqual(title, "测试源片")
        self.assertEqual(file, "kuaijian/9/out.mp4")
        self.assertEqual(url, "https://cos.example/x.mp4")
        self.assertEqual(meta["removed_s"], 11.0)
        self.assertEqual(meta["source_job_id"], 3)


if __name__ == "__main__":
    unittest.main()
