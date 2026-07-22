'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'site', 'workbench', 'video.html'), 'utf8');

function between(startMarker, endMarker) {
  const start = html.indexOf(startMarker);
  const end = html.indexOf(endMarker, start + startMarker.length);
  assert(start >= 0, `missing ${startMarker}`);
  assert(end > start, `missing ${endMarker}`);
  return html.slice(start, end);
}

function fakeElement(value = '') {
  const classes = new Set();
  return {
    value,
    textContent: '',
    title: '',
    disabled: false,
    style: {},
    dataset: {},
    focused: false,
    focus() { this.focused = true; },
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
  };
}

const elements = Object.create(null);
globalThis.$ = function getElement(id) {
  if (!elements[id]) elements[id] = fakeElement();
  return elements[id];
};
globalThis.activeVideoTaskCounts = () => ({talking: 0, cinematic: 0, tryon: 0, xiaole: 0});
globalThis.getTryonHintState = () => ({enable: true, text: ''});
globalThis.updateVideoCosts = () => {};
globalThis.maxActiveCinematic = 2;
globalThis.maxActiveTryon = 1;
globalThis.maxActiveXiaoleVideo = 3;
globalThis.videoSubmitLocks = {talking: false, cinematic: false, tryon: false, xiaole: false};
globalThis.motionOptimizeBusy = false;
globalThis.talkingMotionOptimizedSource = null;
globalThis.token = 'test-token';
globalThis.location = {href: ''};

const runtime = [
  between('function applyButtonState', 'function getTryonHintState'),
  between('function syncVideoGenerateButtons', 'function xiaoleCostNote'),
  between('function talkingMotionFields', 'function setTalkingMotionState'),
  between('function setTalkingMotionState', 'function setMotionOptimizeLock'),
  between('function setMotionOptimizeLock', 'function optimizeTalkingMotion'),
  between('function optimizeTalkingMotion', "$('talkingMotionPrompt').addEventListener"),
].join('\n');
vm.runInThisContext(runtime, {filename: 'video-motion-runtime.js'});

async function flushPromises() {
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
}

async function run() {
  $('talkingMotionPrompt').value = '原始动作 A';
  $('talkingMotionOptimized').value = '优化动作 A';
  globalThis.talkingMotionOptimizedSource = '原始动作 A';
  let fields = globalThis.talkingMotionFields();
  assert.deepStrictEqual(fields, {
    motion_prompt_original: '原始动作 A',
    motion_prompt: '优化动作 A',
  });

  $('talkingMotionPrompt').value = '已经改成动作 B';
  fields = globalThis.talkingMotionFields();
  assert.deepStrictEqual(fields, {
    motion_prompt_original: '已经改成动作 B',
    motion_prompt: '已经改成动作 B',
  });
  assert.strictEqual($('talkingMotionState').dataset.state, 'stale');

  globalThis.videoSubmitLocks.talking = false;
  globalThis.setMotionOptimizeLock(true);
  assert.strictEqual($('talkingMotionOptimize').disabled, true);
  assert.strictEqual($('generateBtn').disabled, true);
  globalThis.setMotionOptimizeLock(false);
  assert.strictEqual($('talkingMotionOptimize').disabled, false);
  assert.strictEqual($('generateBtn').disabled, false);

  globalThis.videoSubmitLocks.talking = true;
  globalThis.syncVideoGenerateButtons();
  assert.strictEqual($('talkingMotionOptimize').disabled, true);
  assert.strictEqual($('generateBtn').disabled, true);
  globalThis.videoSubmitLocks.talking = false;
  globalThis.syncVideoGenerateButtons();

  const original = '失败时必须保留的原始动作';
  $('talkingMotionPrompt').value = original;
  globalThis.talkingMotionOptimizedSource = null;
  let fetchCount = 0;
  globalThis.fetch = (url, options) => {
    fetchCount += 1;
    assert.strictEqual(url, '/api/gen/video/motion-prompt-optimize');
    assert.strictEqual(options.headers.Authorization, 'Bearer test-token');
    return Promise.resolve({
      status: 429,
      json: () => Promise.resolve({detail: '请求太频繁'}),
    });
  };
  globalThis.optimizeTalkingMotion();
  assert.strictEqual($('talkingMotionOptimize').disabled, true);
  assert.strictEqual($('generateBtn').disabled, true);
  await flushPromises();
  assert.strictEqual(fetchCount, 1);
  assert.strictEqual($('talkingMotionPrompt').value, original);
  assert.strictEqual($('talkingMotionState').dataset.state, 'error');
  assert.strictEqual($('talkingMotionState').textContent, '请求太频繁');
  assert.strictEqual($('talkingMotionOptimize').disabled, false);
  assert.strictEqual($('generateBtn').disabled, false);

  console.log('talking motion prompt frontend behavior: PASS');
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
