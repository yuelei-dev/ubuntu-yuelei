const test = require('node:test');
const assert = require('node:assert/strict');
const state = require('../site/workbench/digital-human-material-state.js');

test('upload in progress blocks analyze, start, add, and remove', () => {
  assert.equal(state.canAnalyze('input',true),false);
  assert.equal(state.canStart('planned',true,true),false);
  assert.equal(state.canChange('planned',true),false);
});

test('approved production locks the frozen material set', () => {
  assert.equal(state.canChange('approved',false),false);
  assert.equal(state.canAnalyze('approved',false),false);
  assert.equal(state.canStart('approved',false,true),true);
});

test('refresh restore exposes a clickable resume action for approved work', () => {
  const button={disabled:true,textContent:'生成进行中'};
  state.restoreStartButton(button,'approved');
  assert.equal(button.disabled,false);
  assert.equal(button.textContent,'继续上次未完成的生成');
  assert.equal(state.canStart('approved',false,true),true);
});

test('planned restore exposes the normal confirmation action', () => {
  const button={disabled:true,textContent:''};
  state.restoreStartButton(button,'planned');
  assert.equal(button.disabled,false);
  assert.equal(button.textContent,'确认方案并生成');
});

test('restore keeps only valid unexpired private uploads', () => {
  const valid={upload_id:'img_'+'a'.repeat(32),name:'有效素材.png',sha256:'digest',expires_at:2000};
  const expired={upload_id:'img_'+'b'.repeat(32),name:'过期素材.png',expires_at:999};
  const foreign={upload_id:'bad-id',name:'异常素材.png',expires_at:2000};
  assert.deepEqual(state.normalize([valid,expired,foreign],1000),[valid]);
});
