const test = require('node:test');
const assert = require('node:assert/strict');
const voice = require('../site/workbench/digital-human-voice-state.js');

const slots = [{slot_id:'slot_ready',status:'ready',voice_id:7,voice_name:'岳磊'}];

test('approved refresh preserves and locks the frozen voice', () => {
  const current={phase:'approved',voiceMode:'existing',voiceKey:'vip_slot_ready'};
  const result=voice.resolveLoaded(current,slots);
  assert.deepEqual(result.selection,current);
  assert.equal(result.locked,true);
  assert.equal(result.valid,true);
});

test('slots failure never rewrites an approved frozen voice', () => {
  const current={phase:'approved',voiceMode:'existing',voiceKey:'vip_slot_ready'};
  const result=voice.loadFailed(current,'HTTP 500');
  assert.deepEqual(result.selection,current);
  assert.equal(result.valid,false);
  assert.match(result.error,/声音选择未改变/);
});

test('slots failure preserves planned selection without freezing future changes', () => {
  const current={phase:'planned',voiceMode:'existing',voiceKey:'vip_slot_ready'};
  const result=voice.loadFailed(current,'HTTP 500');
  assert.deepEqual(result.selection,current);
  assert.equal(result.locked,false);
  assert.equal(result.valid,false);
  assert.equal(voice.change(current,'__clone__').accepted,true);
});

test('missing approved voice stops with an explicit error and preserves selection', () => {
  const current={phase:'approved',voiceMode:'existing',voiceKey:'vip_slot_missing'};
  const result=voice.resolveLoaded(current,slots);
  assert.deepEqual(result.selection,current);
  assert.equal(result.valid,false);
  assert.match(result.error,/当前不可用/);
});

test('approved user attempt to switch voice is rejected', () => {
  const current={phase:'approved',voiceMode:'existing',voiceKey:'vip_slot_ready'};
  const result=voice.change(current,'__clone__');
  assert.equal(result.accepted,false);
  assert.deepEqual(result.selection,current);
});

test('approved interrupted reclone stops because local sample cannot be restored', () => {
  const current={phase:'approved',voiceMode:'clone',voiceKey:''};
  const result=voice.resolveLoaded(current,slots);
  assert.deepEqual(result.selection,current);
  assert.equal(result.locked,true);
  assert.equal(result.valid,false);
  assert.match(result.error,/无法恢复本地样音文件/);
});

test('planned user can switch between ready voice and reclone', () => {
  const current={phase:'planned',voiceMode:'existing',voiceKey:'vip_slot_ready'};
  assert.deepEqual(voice.change(current,'__clone__').selection,{phase:'planned',voiceMode:'clone',voiceKey:''});
  assert.deepEqual(voice.change(current,'vip_slot_ready').selection,current);
});
