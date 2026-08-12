const test = require('node:test');
const assert = require('node:assert/strict');
const setup = require('../site/workbench/digital-human-setup-state.js');

function nodes(){return {voiceSource:{disabled:false},voiceSample:{disabled:false},restart:{hidden:true}};}

test('initial setup keeps restart hidden and both voice controls enabled', () => {
  const dom=nodes();
  setup.applyControls(dom,'input');
  assert.deepEqual(dom,{voiceSource:{disabled:false},voiceSample:{disabled:false},restart:{hidden:true}});
});

test('entering approved immediately locks both voice controls and shows restart', () => {
  const dom=nodes();
  setup.applyControls(dom,'approved');
  assert.deepEqual(dom,{voiceSource:{disabled:true},voiceSample:{disabled:true},restart:{hidden:false}});
});

test('cancelling restart preserves approved state and locked DOM', () => {
  const current={phase:'approved',voiceKey:'vip_keep',jobs:{video:[9]}};
  const result=setup.restart(current,false),dom=nodes();
  setup.applyControls(dom,result.state.phase);
  assert.equal(result.changed,false);
  assert.deepEqual(result.state,current);
  assert.equal(dom.restart.hidden,false);
});

test('confirming restart returns input and re-enables both voice controls', () => {
  const result=setup.restart({phase:'approved',voiceKey:'vip_old'},true),dom=nodes();
  setup.applyControls(dom,result.state.phase);
  assert.equal(result.changed,true);
  assert.deepEqual(result.state,{phase:'input'});
  assert.equal(dom.voiceSource.disabled,false);
  assert.equal(dom.voiceSample.disabled,false);
  assert.equal(dom.restart.hidden,true);
});

test('restart transition never submits a task or charges points', () => {
  const runEpoch=4,result=setup.restart({phase:'approved'},true),currentEpoch=result.cancelRun?runEpoch+1:runEpoch;
  assert.equal(result.submit,false);
  assert.equal(result.charge,false);
  assert.equal(result.cancelRun,true);
  assert.equal(setup.canContinue(runEpoch,currentEpoch),false);
  let submitCount=0;
  if(setup.canContinue(runEpoch,currentEpoch))submitCount++;
  assert.equal(submitCount,0);
});

test('completed state hides restart and leaves controls ready for another video', () => {
  const dom=nodes();
  setup.applyControls(dom,'complete');
  assert.deepEqual(dom,{voiceSource:{disabled:false},voiceSample:{disabled:false},restart:{hidden:true}});
});
