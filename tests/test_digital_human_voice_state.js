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

test('clone status normalizes the wrapped response used by the current API', () => {
  const raw={ok:true,result:{status:' READY ',preview_url:'/audio/demo.mp3',clone_error:null}};
  const result=voice.normalizeCloneStatus(raw);
  assert.equal(result.status,'ready');
  assert.equal(result.preview_url,'/audio/demo.mp3');
  assert.equal(result.clone_error,'');
  assert.equal(result.raw,raw.result);
});

test('clone status keeps compatibility with the legacy flat response', () => {
  const raw={status:'training',preview_url:'',clone_error:''};
  const result=voice.normalizeCloneStatus(raw);
  assert.equal(result.status,'training');
  assert.equal(result.raw,raw);
});

test('clone status carries attempt id and recovery rejects another operation', async () => {
  const attempt='dh-voice-clone-current-001';
  assert.equal(voice.normalizeCloneStatus({result:{status:'training',attempt_id:attempt}}).attempt_id,attempt);
  assert.equal(voice.restoredCloneDecision({result:{status:'ready',attempt_id:'old-attempt-001'}},{accepted:true,attemptId:attempt}).action,'blocked');
  let submits=0;
  await assert.rejects(voice.runCloneRecovery({
    expectedAttemptId:attempt,hasSample:false,allowSubmit:false,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>({result:{status:'ready',attempt_id:'old-attempt-001'}}),
    submit:()=>{submits++;return Promise.resolve();},
  }),/操作标识不匹配/);
  assert.equal(submits,0);
});

test('ready clone is reused without requiring a sample or another submission', () => {
  const result=voice.cloneRetryDecision({ok:true,result:{status:'ready',preview_url:'/audio/demo.mp3'}},false);
  assert.deepEqual(result,{status:'ready',action:'reuse',error:'',clone_error:'',preview_url:'/audio/demo.mp3'});
});

test('training clone is only polled even when a sample is still present', () => {
  assert.equal(voice.cloneRetryDecision({status:'training'},true).action,'poll');
  assert.equal(voice.cloneRetryDecision({status:'training'},false).action,'poll');
});

test('failed and active clone can be submitted only while the sample exists', () => {
  assert.equal(voice.cloneRetryDecision({status:'failed',clone_error:'provider failed'},true).action,'submit');
  assert.equal(voice.cloneRetryDecision({ok:true,result:{status:'active'}},true).action,'submit');

  const failedWithoutSample=voice.cloneRetryDecision({status:'failed',clone_error:'provider failed'},false);
  assert.equal(failedWithoutSample.action,'blocked');
  assert.equal(failedWithoutSample.error,'provider failed');
  assert.match(voice.cloneRetryDecision({status:'active'},false).error,/\u91cd\u65b0\u4e0a\u4f20\u58f0\u97f3\u6837\u672c/);
});

test('missing malformed and unknown status are blocked even with a sample', () => {
  for(const response of [null,{}, {ok:true,result:{}}, {status:'queued'}]){
    const result=voice.cloneRetryDecision(response,true);
    assert.equal(result.action,'blocked');
    assert.match(result.error,/\u58f0\u97f3\u72b6\u6001\u54cd\u5e94\u5f02\u5e38/);
  }
});

test('restored clone accepts ready only after this page observed submission acceptance', () => {
  const ready={ok:true,result:{status:'ready'}};
  assert.equal(voice.restoredCloneDecision(ready,{accepted:false,progress:false}).action,'reattach');
  assert.match(voice.restoredCloneDecision(ready,{accepted:false,progress:false}).error,/提交结果无法确认/);
  assert.equal(voice.restoredCloneDecision(ready,{accepted:true,progress:false}).action,'reuse');
  assert.equal(voice.restoredCloneDecision(ready,{accepted:false,progress:true}).action,'reattach');
});

test('restored clone polls training only after the clone POST was accepted', () => {
  assert.equal(voice.restoredCloneDecision({result:{status:'training'}},{accepted:true}).action,'poll');
  assert.equal(voice.restoredCloneDecision({result:{status:'training'}},{accepted:false}).action,'reattach');
  const failed=voice.restoredCloneDecision({result:{status:'failed',clone_error:'provider rejected'}},{accepted:true});
  assert.equal(failed.action,'reattach');
  assert.equal(failed.error,'provider rejected');
});

test('clone recovery reuses a ready voice without another paid submission', async () => {
  let submits=0,statuses=0;
  const result=await voice.runCloneRecovery({
    hasSample:false,allowSubmit:false,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>{statuses++;return {ok:true,result:{status:'ready'}};},
    submit:()=>{submits++;return Promise.resolve();},
  });
  assert.equal(result.status,'ready');
  assert.equal(statuses,1);
  assert.equal(submits,0);
});

test('clone recovery polls training without resubmitting', async () => {
  const responses=['training','training','ready'];let submits=0;
  const result=await voice.runCloneRecovery({
    hasSample:true,allowSubmit:true,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>({result:{status:responses.shift()}}),
    submit:()=>{submits++;return Promise.resolve();},
  });
  assert.equal(result.status,'ready');
  assert.equal(submits,0);
});

test('failed clone only submits once while the original sample is available', async () => {
  const responses=['failed','training','ready'];let submits=0;
  const result=await voice.runCloneRecovery({
    hasSample:true,allowSubmit:true,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>({result:{status:responses.shift(),clone_error:'provider failed'}}),
    submit:()=>{submits++;return Promise.resolve();},
  });
  assert.equal(result.status,'ready');
  assert.equal(submits,1);
});

test('refresh without a sample surfaces the provider failure and never submits', async () => {
  let submits=0;
  await assert.rejects(voice.runCloneRecovery({
    hasSample:false,allowSubmit:false,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>({ok:true,result:{status:'failed',clone_error:'真实供应商错误'}}),
    submit:()=>{submits++;return Promise.resolve();},
  }),/真实供应商错误/);
  assert.equal(submits,0);
});

test('only an explicit failed status for the same attempt rotates recovery markers', () => {
  const current='dh-voice-clone-current-001',next='dh-voice-clone-next-002';
  const failed=voice.failedAttemptTransition({result:{status:'failed',attempt_id:current,clone_error:'provider failed'}},current,next);
  assert.deepEqual(failed,{rotate:true,key:next,submitted:false,accepted:false,progress:false,error:'provider failed'});
  for(const response of [
    {result:{status:'training',attempt_id:current}},
    {result:{status:'failed',attempt_id:'dh-voice-clone-other-003'}},
    {},
  ]){
    const unchanged=voice.failedAttemptTransition(response,current,next);
    assert.equal(unchanged.rotate,false);
    assert.equal(unchanged.key,current);
    assert.equal(unchanged.submitted,null);
  }
});

test('accepted same-attempt provider failure requires reattach and never reuses the failed key', async () => {
  const current='dh-voice-clone-current-001';let submits=0;
  await assert.rejects(voice.runCloneRecovery({
    expectedAttemptId:current,initialSubmitted:true,initialProgress:true,
    hasSample:true,allowSubmit:true,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>({result:{status:'failed',attempt_id:current,clone_error:'provider rejected'}}),
    submit:()=>{submits++;return Promise.resolve();},
  }),error=>error.cloneAttemptFailed===true&&error.attempt_id===current);
  assert.equal(submits,0);
});

test('a rotated failed attempt starts the replacement key exactly once after sample reattach', async () => {
  const oldKey='dh-voice-clone-old-001',newKey='dh-voice-clone-new-002';
  const transition=voice.failedAttemptTransition(
    {result:{status:'failed',attempt_id:oldKey,clone_error:'provider rejected'}},oldKey,newKey,
  );
  let submits=0;const statuses=['training','ready'];
  const result=await voice.runCloneRecovery({
    expectedAttemptId:transition.key,hasSample:true,allowSubmit:true,forceSubmit:true,
    initialSubmitted:transition.submitted,initialProgress:transition.progress,
    pollDelay:0,wait:()=>Promise.resolve(),
    submit:()=>{submits++;return Promise.resolve({ok:true,attempt_id:newKey});},
    getStatus:()=>({result:{status:statuses.shift(),attempt_id:newKey}}),
  });
  assert.equal(result.status,'ready');
  assert.equal(submits,1);
});

test('an uncertain or conflicting submit fails closed before reading shared slot state', async () => {
  for(const submitError of [
    new Error('response lost'),
    Object.assign(new Error('in progress'),{status:409,code:'voice_clone_in_progress'}),
  ]){
    let submits=0,statuses=0;
    await assert.rejects(voice.runCloneRecovery({
      hasSample:true,allowSubmit:true,forceSubmit:true,pollDelay:0,wait:()=>Promise.resolve(),
      getStatus:()=>{statuses++;return {result:{status:'ready'}};},
      submit:()=>{submits++;return Promise.reject(submitError);},
    }),error=>error===submitError);
    assert.equal(submits,1);
    assert.equal(statuses,0);
  }
});

test('only same-key idempotency in-progress can safely retry the original POST', () => {
  assert.deepEqual(voice.cloneSubmitErrorDecision(Object.assign(new Error('same request'),{
    status:409,code:'idempotency_in_progress',
  })),{action:'retry',accepted:false});
  for(const error of [
    Object.assign(new Error('another request'),{status:409,code:'voice_clone_in_progress'}),
    Object.assign(new Error('payload conflict'),{status:409,code:'idempotency_conflict'}),
    new Error('response lost'),
  ])assert.deepEqual(voice.cloneSubmitErrorDecision(error),{action:'fail',accepted:false});
});

test('idempotency in-progress retries the POST and never queries stale slot status', async () => {
  let submits=0,statusChecks=0;
  const response=await voice.submitCloneWithIdempotency({
    maxAttempts:3,retryDelay:0,wait:()=>Promise.resolve(),
    submit:()=>{submits++;return submits===1
      ?Promise.reject(Object.assign(new Error('processing'),{status:409,code:'idempotency_in_progress'}))
      :Promise.resolve({ok:true,voice:{status:'training'}});},
  });
  assert.equal(response.ok,true);
  assert.equal(submits,2);
  assert.equal(statusChecks,0);
});

test('bounded idempotency in-progress fails closed without accepting old ready', async () => {
  let submits=0,statusChecks=0;
  await assert.rejects(voice.submitCloneWithIdempotency({
    maxAttempts:3,retryDelay:0,wait:()=>Promise.resolve(),
    submit:()=>{submits++;return Promise.reject(Object.assign(new Error('processing'),{
      status:409,code:'idempotency_in_progress',
    }));},
  }),/processing/);
  assert.equal(submits,3);
  assert.equal(statusChecks,0);
});

test('a successful POST may be followed directly by ready and is accepted exactly once', async () => {
  let submits=0,accepted=false,statusChecks=0;
  const result=await voice.runCloneRecovery({
    hasSample:true,allowSubmit:true,forceSubmit:true,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>{statusChecks++;return {result:{status:'ready'}};},
    submit:()=>{submits++;accepted=true;return Promise.resolve({ok:true});},
  });
  assert.equal(result.status,'ready');
  assert.equal(result.submitted,true);
  assert.equal(accepted,true);
  assert.equal(submits,1);
  assert.equal(statusChecks,1);
});

test('a confirmed resubmission after an uncertain attempt may complete immediately', async () => {
  let submits=0;
  const result=await voice.runCloneRecovery({
    hasSample:true,
    allowSubmit:true,
    forceSubmit:true,
    initialSubmitted:true,
    initialProgress:false,
    requireProgressBeforeReady:true,
    pollDelay:0,
    wait:()=>Promise.resolve(),
    submit:async()=>{submits++;return {ok:true};},
    getStatus:async()=>({ok:true,result:{status:'ready'}}),
  });
  assert.equal(result.status,'ready');
  assert.equal(submits,1);
});

test('a successful POST and observed training persist enough markers to resume after refresh', async () => {
  const marker={submitted:false,accepted:false,progress:false};
  const refreshed=Object.assign(new Error('page refreshed'),{generationCancelled:true});
  let firstSubmits=0,firstChecks=0;
  await assert.rejects(voice.runCloneRecovery({
    hasSample:true,allowSubmit:true,forceSubmit:true,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>{firstChecks++;return firstChecks===1?{result:{status:'training'}}:Promise.reject(refreshed);},
    submit:()=>{firstSubmits++;marker.submitted=true;return Promise.resolve({ok:true}).then(function(result){marker.accepted=true;return result;});},
    onStatus:(status)=>{if(status==='training')marker.progress=true;},
  }),error=>error===refreshed);
  assert.deepEqual(marker,{submitted:true,accepted:true,progress:true});
  assert.equal(firstSubmits,1);

  let resumedSubmits=0;
  const result=await voice.runCloneRecovery({
    initialSubmitted:marker.submitted,
    initialProgress:marker.progress,
    requireProgressBeforeReady:marker.submitted&&!marker.accepted,
    hasSample:false,allowSubmit:false,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>({result:{status:'ready'}}),
    submit:()=>{resumedSubmits++;return Promise.resolve();},
  });
  assert.equal(result.status,'ready');
  assert.equal(result.submitted,true);
  assert.equal(resumedSubmits,0);
});

test('an uncertain persisted submit cannot accept an old ready voice', async () => {
  let submits=0;
  await assert.rejects(voice.runCloneRecovery({
    initialSubmitted:true,requireProgressBeforeReady:true,hasSample:false,allowSubmit:false,
    pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>({result:{status:'ready'}}),
    submit:()=>{submits++;return Promise.resolve();},
  }),/提交结果无法确认/);
  assert.equal(submits,0);
});

test('old ready is rejected until an uncertain persisted submission observes progress', async () => {
  let submits=0;
  await assert.rejects(voice.runCloneRecovery({
    initialSubmitted:true,initialProgress:false,requireProgressBeforeReady:true,
    hasSample:false,allowSubmit:false,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>({result:{status:'ready'}}),
    submit:()=>{submits++;return Promise.resolve();},
  }),error=>{
    assert.equal(error.cloneTerminal,true);
    assert.match(error.message,/\u63d0\u4ea4\u7ed3\u679c\u65e0\u6cd5\u786e\u8ba4/);
    return true;
  });
  assert.equal(submits,0);
});

test('a successful submit may briefly report active before training', async () => {
  const responses=['active','training','ready'];let submits=0;
  const result=await voice.runCloneRecovery({
    hasSample:true,allowSubmit:true,forceSubmit:true,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>({result:{status:responses.shift()}}),
    submit:()=>{submits++;return Promise.resolve();},
  });
  assert.equal(result.status,'ready');
  assert.equal(submits,1);
});

test('deterministic clone submission errors fail immediately', async () => {
  for(const status of [400,403,422]){
    let statusChecks=0;
    await assert.rejects(voice.runCloneRecovery({
      hasSample:true,allowSubmit:true,forceSubmit:true,pollDelay:0,wait:()=>Promise.resolve(),
      getStatus:()=>{statusChecks++;return {result:{status:'ready'}};},
      submit:()=>Promise.reject(Object.assign(new Error('HTTP '+status),{status})),
    }),new RegExp(String(status)));
    assert.equal(statusChecks,0);
  }
});

test('a deterministic POST failure can retry on the same page from failed through training to ready', async () => {
  const marker={submitted:false,accepted:false,progress:false};
  let submitAttempts=0,statusChecks=0;
  function submit(){
    submitAttempts++;
    marker.submitted=true;
    if(submitAttempts===1)return Promise.reject(Object.assign(new Error('sample rejected'),{status:422}));
    marker.accepted=true;
    return Promise.resolve({ok:true});
  }

  await assert.rejects(voice.runCloneRecovery({
    hasSample:true,allowSubmit:true,forceSubmit:true,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>{statusChecks++;return {result:{status:'ready'}};},submit:submit,
  }),/sample rejected/);
  assert.deepEqual(marker,{submitted:true,accepted:false,progress:false});
  assert.equal(statusChecks,0);

  const statuses=['failed','training','ready'];
  const result=await voice.runCloneRecovery({
    initialSubmitted:marker.submitted,
    initialProgress:marker.progress,
    requireProgressBeforeReady:marker.submitted&&!marker.accepted,
    hasSample:true,allowSubmit:true,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>{statusChecks++;return {result:{status:statuses.shift(),clone_error:'previous attempt failed'}};},
    submit:submit,
    onStatus:(status)=>{if(status==='training')marker.progress=true;},
  });
  assert.equal(result.status,'ready');
  assert.equal(submitAttempts,2);
  assert.equal(statusChecks,3);
  assert.deepEqual(marker,{submitted:true,accepted:true,progress:true});
});

test('cancellation is never converted into a network retry', async () => {
  const cancelled=Object.assign(new Error('cancelled'),{generationCancelled:true});
  let waits=0;
  await assert.rejects(voice.runCloneRecovery({
    hasSample:false,allowSubmit:false,pollDelay:0,wait:()=>{waits++;return Promise.resolve();},
    getStatus:()=>Promise.reject(cancelled),submit:()=>Promise.resolve(),
  }),error=>error===cancelled);
  assert.equal(waits,0);

  await assert.rejects(voice.runCloneRecovery({
    hasSample:true,allowSubmit:true,forceSubmit:true,pollDelay:0,wait:()=>{waits++;return Promise.resolve();},
    getStatus:()=>({result:{status:'ready'}}),submit:()=>Promise.reject(cancelled),
  }),error=>error===cancelled);
  assert.equal(waits,0);
});

test('clone status tolerates bounded network faults but fails closed after the limit', async () => {
  let attempts=0;
  const recovered=await voice.runCloneRecovery({
    hasSample:false,allowSubmit:false,maxErrors:3,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>{attempts++;if(attempts<3)return Promise.reject(new Error('temporary'));return {result:{status:'ready'}};},
    submit:()=>Promise.resolve(),
  });
  assert.equal(recovered.status,'ready');
  assert.equal(attempts,3);
  attempts=0;
  await assert.rejects(voice.runCloneRecovery({
    hasSample:false,allowSubmit:false,maxErrors:3,pollDelay:0,wait:()=>Promise.resolve(),
    getStatus:()=>{attempts++;return Promise.reject(new Error('still offline'));},
    submit:()=>Promise.resolve(),
  }),/still offline/);
  assert.equal(attempts,3);
});
