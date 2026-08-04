import json
import pathlib
import shutil
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_HTML = ROOT / "site" / "workbench" / "script.html"


def _extract_function(source, name):
    marker = f"function {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


class ReversePromptHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node is required for reverse prompt history contracts")
        cls.html = SCRIPT_HTML.read_text(encoding="utf-8")
        cls.functions = "\n".join(
            _extract_function(cls.html, name)
            for name in (
                "isReversePromptErrorText",
                "validReversePromptText",
                "reverseResultPrompt",
                "reverseLegacyPrompt",
                "reverseAuditData",
                "reverseReferenceThumbnailIndices",
                "reverseReferenceImages",
                "breakdownMetaFromResult",
                "compactBreakdownHistoryMeta",
                "isBreakdownHistoryMeta",
                "saveBreakdownHistory",
                "breakdownHistoryResult",
                "loadBreakdownHistoryDetail",
            )
        )

    def _normalize(self, payload):
        harness = f"""
function normalizeBreakdownScenes(value){{return value||[];}}
{self.functions}
var meta=breakdownMetaFromResult({json.dumps(payload, ensure_ascii=False)});
process.stdout.write(JSON.stringify({{meta:meta,valid:isBreakdownHistoryMeta(meta)}}));
"""
        result = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(result.stdout)

    def test_history_preserves_reverse_timeline_scores_and_evidence_mapping(self):
        payload = {
            "type": "breakdown_reverse",
            "prompt": "RESTORABLE PROMPT",
            "frame_thumbnails": [f"frame-{index}" for index in range(1, 10)],
            "reference_thumbnail_indices": [2, 4, 6, 8],
            "audit_thumbnail_indices": [1, 3, 5, 7],
            "frame_manifest": [{"global_frame_number": 1}],
            "timeline_audit": {"precision_seconds": 0.1, "windows": [[0, 4, "0-4"]]},
            "quality_score": {"total": 95, "components": {}},
            "reverse_audit": {"segments": [{"segment_id": 1}]},
        }
        result = self._normalize(payload)
        self.assertTrue(result["valid"])
        meta = result["meta"]
        self.assertEqual(meta["frame_thumbnails"], payload["frame_thumbnails"][:8])
        self.assertEqual(meta["reference_thumbnail_indices"], [2, 4, 6, 8])
        self.assertEqual(meta["audit_thumbnail_indices"], [1, 3, 5, 7])
        self.assertEqual(meta["timeline_audit"]["precision_seconds"], 0.1)
        self.assertEqual(meta["quality_score"]["total"], 95)
        self.assertEqual(meta["reverse_audit"]["segments"][0]["segment_id"], 1)

    def test_invalid_failure_result_is_not_saved_as_history(self):
        result = self._normalize(
            {"type": "breakdown_reverse", "prompt": "反推失败：上游异常 · 已退点"}
        )
        self.assertFalse(result["valid"])

    def test_generation_uses_reference_indices_and_legacy_fallback(self):
        harness = f"""
{self.functions}
var mapped=reverseReferenceImages({{
  frame_thumbnails:['audit-1','ref-2','audit-3','ref-4','audit-5','ref-6','audit-7','ref-8'],
  reference_thumbnail_indices:[2,4,6,8]
}});
var legacy=reverseReferenceImages({{frame_thumbnails:['f1','f2','f3','f4','f5']}});
process.stdout.write(JSON.stringify({{mapped:mapped,legacy:legacy}}));
"""
        result = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        got = json.loads(result.stdout)
        self.assertEqual(got["mapped"], ["ref-2", "ref-4", "ref-6", "ref-8"])
        self.assertEqual(got["legacy"], ["f1", "f2", "f3", "f4"])

    def test_local_history_stays_compact_and_keeps_server_job_id(self):
        harness = f"""
var BREAKDOWN_HISTORY_KEY='history';
var storage={{}};
var toasts=[];
var historyState={{textContent:''}};
var localStorage={{
  getItem:function(key){{return storage[key]||null;}},
  setItem:function(key,value){{storage[key]=value;}}
}};
var window={{HQ:{{toast:function(message){{toasts.push(message);}}}}}};
var HQ=window.HQ;
function normalizeBreakdownScenes(value){{return value||[];}}
{self.functions}
var large='data:image/jpeg;base64,'+'A'.repeat(320*1024);
var ok=saveBreakdownHistory({{
  type:'breakdown_reverse',_history_job_id:3258,
  source_url:'https://example.invalid/video',source_title:'大缩略图反推',
  prompt:'可恢复提示词',frame_thumbnails:Array(8).fill(large),
  reference_thumbnail_indices:[2,4,6,8],audit_thumbnail_indices:[1,3,5,7],
  frame_manifest:[{{global_frame_number:1}}],timeline_audit:{{precision_seconds:0.1}},
  quality_score:{{total:96}},reverse_audit:{{segments:[{{segment_id:1}}]}}
}});
var raw=storage[BREAKDOWN_HISTORY_KEY]||'';
var saved=JSON.parse(raw)[0];
process.stdout.write(JSON.stringify({{ok:ok,length:raw.length,item:saved}}));
"""
        result = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        got = json.loads(result.stdout)
        self.assertTrue(got["ok"])
        self.assertLess(got["length"], 16 * 1024)
        self.assertEqual(got["item"]["job_id"], 3258)
        self.assertEqual(got["item"]["meta"]["job_id"], 3258)
        for field in (
            "frame_thumbnails",
            "frame_manifest",
            "timeline_audit",
            "quality_score",
            "reverse_audit",
            "sections",
        ):
            self.assertNotIn(field, got["item"]["meta"])

    def test_owned_job_detail_restores_complete_server_result(self):
        self.assertIn("loadBreakdownHistoryDetail(item).then(function(detail)", self.html)
        self.assertIn("renderBreakdownReverse(Object.assign({},detail", self.html)
        harness = f"""
var loginCalls=0;
var window={{HQ:{{login:function(){{loginCalls++;}}}}}};
var HQ=window.HQ;
function tok(){{return 'cookie';}}
var full={{
  type:'breakdown_reverse',prompt:'服务端完整提示词',
  frame_thumbnails:['f1','f2','f3','f4','f5','f6','f7','f8'],
  reference_thumbnail_indices:[2,4,6,8],quality_score:{{total:97}}
}};
var requested='';
function fetch(url,options){{
  requested=url;
  return Promise.resolve({{ok:true,status:200,json:function(){{return Promise.resolve({{result:full}});}}}});
}}
{self.functions}
loadBreakdownHistoryDetail({{job_id:3258,meta:{{type:'breakdown_reverse'}}}}).then(function(detail){{
  process.stdout.write(JSON.stringify({{requested:requested,detail:detail,loginCalls:loginCalls}}));
}});
"""
        result = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        got = json.loads(result.stdout)
        self.assertEqual(got["requested"], "/api/gen/job/3258")
        self.assertEqual(len(got["detail"]["frame_thumbnails"]), 8)
        self.assertEqual(got["detail"]["reference_thumbnail_indices"], [2, 4, 6, 8])
        self.assertEqual(got["detail"]["_history_job_id"], 3258)
        self.assertEqual(got["loginCalls"], 0)

    def test_real_completion_paths_attach_job_and_batch_identity(self):
        self.assertIn(
            "var localResult=breakdownHistoryResult(d.result||{},jobId)",
            self.html,
        )
        self.assertIn(
            "saveBreakdownHistory(breakdownHistoryResult(item,x.d.job_id,index))",
            self.html,
        )
        self.assertGreaterEqual(
            self.html.count("result=breakdownHistoryResult(result,x.d.job_id)"),
            2,
        )

    def test_all_completion_identities_survive_compact_local_history(self):
        harness = f"""
var BREAKDOWN_HISTORY_KEY='history';
var storage={{}};
var historyState={{textContent:''}};
var localStorage={{
  getItem:function(key){{return storage[key]||null;}},
  setItem:function(key,value){{storage[key]=value;}}
}};
var window={{HQ:{{toast:function(){{}}}}}};
var HQ=window.HQ;
function normalizeBreakdownScenes(value){{return value||[];}}
{self.functions}
saveBreakdownHistory(breakdownHistoryResult({{
  type:'breakdown_reverse',source_url:'https://example.invalid/local',prompt:'LOCAL'
}},701));
saveBreakdownHistory(breakdownHistoryResult({{
  type:'breakdown_reverse',source_url:'https://example.invalid/link',prompt:'LINK'
}},702));
saveBreakdownHistory(breakdownHistoryResult({{
  type:'breakdown',source_url:'https://example.invalid/batch-1',scenes:[{{scene:'S1'}}]
}},703,0));
saveBreakdownHistory(breakdownHistoryResult({{
  type:'breakdown',source_url:'https://example.invalid/batch-2',scenes:[{{scene:'S2'}}]
}},703,1));
var items=JSON.parse(storage[BREAKDOWN_HISTORY_KEY]);
process.stdout.write(JSON.stringify(items.map(function(item){{return {{
  job_id:item.job_id,meta_job_id:item.meta.job_id,batch_index:item.meta._batch_index,
  source_url:item.meta.source_url
}};}})));
"""
        result = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        got = json.loads(result.stdout)
        by_source = {item["source_url"]: item for item in got}
        self.assertEqual(by_source["https://example.invalid/local"]["job_id"], 701)
        self.assertIsNone(by_source["https://example.invalid/local"]["batch_index"])
        self.assertEqual(by_source["https://example.invalid/link"]["meta_job_id"], 702)
        self.assertIsNone(by_source["https://example.invalid/link"]["batch_index"])
        self.assertEqual(by_source["https://example.invalid/batch-1"]["job_id"], 703)
        self.assertEqual(by_source["https://example.invalid/batch-1"]["batch_index"], 0)
        self.assertEqual(by_source["https://example.invalid/batch-2"]["batch_index"], 1)

    def test_batch_history_detail_uses_parent_job_and_stable_index(self):
        harness = f"""
var window={{HQ:{{login:function(){{}}}}}};
var HQ=window.HQ;
function tok(){{return 'cookie';}}
var requested='';
function fetch(url,options){{
  requested=url;
  return Promise.resolve({{ok:true,status:200,json:function(){{return Promise.resolve({{
    result:{{type:'breakdown_batch',results:[
      {{type:'breakdown',prompt:'FIRST'}},
      {{type:'breakdown_reverse',prompt:'SECOND',frame_thumbnails:['f1','f2']}}
    ]}}
  }});}}}});
}}
{self.functions}
loadBreakdownHistoryDetail({{job_id:703,meta:{{_batch_index:1}}}}).then(function(detail){{
  process.stdout.write(JSON.stringify({{requested:requested,detail:detail}}));
}});
"""
        result = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        got = json.loads(result.stdout)
        self.assertEqual(got["requested"], "/api/gen/job/703")
        self.assertEqual(got["detail"]["prompt"], "SECOND")
        self.assertEqual(got["detail"]["_history_job_id"], 703)


if __name__ == "__main__":
    unittest.main()
