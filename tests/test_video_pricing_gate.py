# -*- coding: utf-8 -*-
import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIDEO_HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")
GATE_JS = ROOT / "site/workbench/pricing-gate.js"
NODE = shutil.which("node")
REQUIRED_KEYS = (
    "avatar", "talking.per_sec", "cinematic.motion.per_sec",
    "cinematic.duo.per_sec", "cinematic.open.per_sec", "tryon.single",
    "tryon.combo", "xiaole_video.per_sec", "grok_video.v1.480p.per_sec",
    "grok_video.v1.720p.per_sec", "grok_video.v1_5.480p.per_sec",
    "grok_video.v1_5.720p.per_sec", "grok_video.v1_5.1080p.per_sec",
)


def _node_case(body):
    script = """
const assert=require('assert');
const {create}=require(%s);
const keys=%s;
function complete(rate=100){
  const values={}; keys.forEach((key,index)=>values[key]=index+1);
  values['grok_video.v1.720p.per_sec']=rate;
  return {values};
}
(async()=>{%s})().catch(error=>{console.error(error);process.exit(1);});
""" % (json.dumps(str(GATE_JS)), json.dumps(REQUIRED_KEYS), body)
    return subprocess.run([NODE, "-e", script], cwd=ROOT, check=False,
                          capture_output=True, text=True, encoding="utf-8")


@unittest.skipUnless(NODE, "Node.js is required for pricing-gate contract tests")
class VideoPricingGateUnitTests(unittest.TestCase):
    def assert_node_ok(self, body):
        result = _node_case(body)
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)

    def test_complete_success_uses_authoritative_price(self):
        self.assert_node_ok("""
const gate=create({requiredKeys:keys,fetchFn:async()=>({ok:true,json:async()=>complete(100)})});
const state=await gate.load();
assert.equal(state.status,'ready'); assert.equal(gate.guard(),true);
assert.equal(state.values['grok_video.v1.720p.per_sec'],100);
""")

    def test_http_5xx_fails_closed(self):
        self.assert_node_ok("""
const gate=create({requiredKeys:keys,fetchFn:async()=>({ok:false,status:500})});
const state=await gate.load();
assert.equal(state.status,'error'); assert.equal(gate.guard(),false);
assert.match(state.error,/收费标准加载失败/);
""")

    def test_network_failure_fails_closed(self):
        self.assert_node_ok("""
const gate=create({requiredKeys:keys,fetchFn:async()=>{throw new Error('offline')}});
await gate.load(); assert.equal(gate.guard(),false);
assert.equal(gate.getState().values,null);
""")

    def test_malformed_and_missing_key_fail_closed(self):
        self.assert_node_ok("""
for(const payload of [{}, {values:{}}, {values:{...complete().values,'tryon.single':'25'}}]){
  const gate=create({requiredKeys:keys,fetchFn:async()=>({ok:true,json:async()=>payload})});
  await gate.load(); assert.equal(gate.guard(),false);
}
""")

    def test_retry_recovers_only_after_complete_success(self):
        self.assert_node_ok("""
let attempt=0;
const gate=create({requiredKeys:keys,fetchFn:async()=>++attempt===1
  ?{ok:false,status:503}:{ok:true,json:async()=>complete(100)}});
assert.equal((await gate.load()).status,'error'); assert.equal(gate.guard(),false);
assert.equal((await gate.load()).status,'ready'); assert.equal(gate.guard(),true);
assert.equal(gate.getState().values['grok_video.v1.720p.per_sec'],100);
""")

    def test_click_during_failure_does_not_issue_paid_request(self):
        self.assert_node_ok("""
let paidRequests=0, recover=false;
const gate=create({requiredKeys:keys,fetchFn:async()=>recover
  ?{ok:true,json:async()=>complete(100)}:{ok:false,status:500}});
function clickPaid(){if(!gate.guard()) return; paidRequests++;}
await gate.load(); clickPaid(); assert.equal(paidRequests,0);
recover=true; await gate.load(); clickPaid(); assert.equal(paidRequests,1);
""")


class VideoPricingGatePageContractTests(unittest.TestCase):
    def test_paid_controls_start_disabled_and_have_chinese_retry(self):
        for element_id in ("generateBtn", "cineGenerateBtn", "tryonGenerateBtn",
                           "grokGenerateBtn", "microGenerateBtn", "omniGenerateBtn",
                           "cineNewAvatarFile"):
            self.assertRegex(VIDEO_HTML, r'id="%s"[^>]*\bdisabled\b' % element_id)
        self.assertIn('id="pricingRetryBtn"', VIDEO_HTML)
        self.assertIn("收费标准加载失败，暂不能提交付费生成，请重试", VIDEO_HTML)

    def test_every_paid_submit_path_has_defense_in_depth_guard(self):
        for name in ("submitCreateAvatar", "submitCinematic", "submitTryon",
                     "submitVideoBatch", "submitVideo", "submitXiaole"):
            marker = "function %s(" % name
            start = VIDEO_HTML.index(marker)
            body_start = VIDEO_HTML.index("{", start)
            self.assertEqual("if(!ensurePricingReady()) return;",
                             VIDEO_HTML[body_start + 1:body_start + 100].strip().splitlines()[0], name)

    def test_page_requires_complete_catalog_without_default_price_fallbacks(self):
        self.assertIn('src="pricing-gate.js', VIDEO_HTML)
        self.assertIn("requiredKeys:VIDEO_PRICING_KEYS", VIDEO_HTML)
        self.assertIn("if(pricingGate.guard()) return true", VIDEO_HTML)
        self.assertIn("var PRICING_VALUES={};", VIDEO_HTML)
        for stale in ("||30", "||TALKING_RATE", "||(pricey?40:25)"):
            self.assertNotIn(stale, VIDEO_HTML)


if __name__ == "__main__":
    unittest.main()
