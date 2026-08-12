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

function deferred(){
  let resolve,reject;
  const promise=new Promise((ok,bad)=>{resolve=ok;reject=bad;});
  return {promise,resolve,reject};
}

function resume(options){
  if(!options.jobId)return options.launch(false);
  if(options.retryApproved)return options.launch(true);
  return options.poll(options.jobId).catch(error=>{
    if(error&&error.terminalJob)options.markTerminal();
    throw error;
  });
}

test('resolving a suspended submit after confirmed restart cannot repopulate state or launch polling', async () => {
  const pending=deferred(),commits=[],progress=[];
  let currentEpoch=1,pollCount=0,nextPaidStageCount=0;
  let pageState={phase:'approved',jobs:[],keys:[]},localStorageValue=JSON.stringify(pageState),ui='old-running';
  const running=setup.runJobs({
    items:[{name:'old'}],ids:[],keys:[],failed:[],epoch:1,
    currentEpoch:()=>currentEpoch,key:()=> 'old-key',
    submit:()=>pending.promise,poll:()=>{pollCount++;return Promise.resolve({old:true});},
    resume,commit:value=>{commits.push(value);pageState={phase:'approved',jobs:value.ids,keys:value.keys};localStorageValue=JSON.stringify(pageState);},
    onCount:value=>{progress.push(value);ui='old-progress';},
  }).then(()=>{nextPaidStageCount++;});
  assert.equal(commits.length,1);
  currentEpoch++;
  commits.length=0;
  pageState={phase:'input',jobs:[],keys:[]};
  localStorageValue=JSON.stringify(pageState);
  ui='new-input';
  pending.resolve(91);
  await assert.rejects(running,error=>error.generationCancelled===true);
  assert.deepEqual(pageState,{phase:'input',jobs:[],keys:[]});
  assert.deepEqual(JSON.parse(localStorageValue),{phase:'input',jobs:[],keys:[]});
  assert.equal(ui,'new-input');
  assert.deepEqual(commits,[]);
  assert.deepEqual(progress,[]);
  assert.equal(pollCount,0);
  assert.equal(nextPaidStageCount,0);
});

test('resolving a suspended poll after confirmed restart cannot write old job progress', async () => {
  const pendingPoll=deferred(),pollStarted=deferred(),commits=[],progress=[];
  let currentEpoch=7,nextPaidStageCount=0;
  let pageState={phase:'approved',jobs:[],keys:[]},localStorageValue=JSON.stringify(pageState),ui='old-running';
  const running=setup.runJobs({
    items:[{name:'old'}],ids:[],keys:[],failed:[],epoch:7,
    currentEpoch:()=>currentEpoch,key:()=> 'old-key',
    submit:()=>Promise.resolve(92),
    poll:()=>{pollStarted.resolve();return pendingPoll.promise;},resume,
    commit:value=>{commits.push(value);pageState={phase:'approved',jobs:value.ids,keys:value.keys};localStorageValue=JSON.stringify(pageState);},
    onCount:value=>{progress.push(value);ui='old-progress';},
  }).then(()=>{nextPaidStageCount++;});
  await pollStarted.promise;
  currentEpoch++;
  commits.length=0;
  pageState={phase:'input',jobs:[],keys:[]};
  localStorageValue=JSON.stringify(pageState);
  ui='new-input';
  pendingPoll.resolve({old:true});
  await assert.rejects(running,error=>error.generationCancelled===true);
  assert.deepEqual(pageState,{phase:'input',jobs:[],keys:[]});
  assert.deepEqual(JSON.parse(localStorageValue),{phase:'input',jobs:[],keys:[]});
  assert.equal(ui,'new-input');
  assert.deepEqual(commits,[]);
  assert.deepEqual(progress,[]);
  assert.equal(nextPaidStageCount,0);
});

test('a stale rejected request is converted to cancellation and cannot revive old UI', async () => {
  const pending=deferred(),commits=[];
  let currentEpoch=11,ui='old-running';
  const running=setup.runJobs({
    items:[{name:'old'}],ids:[],keys:[],failed:[],epoch:11,
    currentEpoch:()=>currentEpoch,key:()=> 'old-key',
    submit:()=>pending.promise,poll:()=>Promise.resolve({}),resume,
    commit:value=>commits.push(value),onCount:()=>{ui='old-progress';},
  });
  currentEpoch++;
  commits.length=0;
  ui='new-input';
  pending.reject(new Error('late network error'));
  await assert.rejects(running,error=>error.generationCancelled===true);
  assert.deepEqual(commits,[]);
  assert.equal(ui,'new-input');
});

test('cancelling the restart confirmation preserves the epoch and lets the current job recover', async () => {
  const transition=setup.restart({phase:'approved'},false),commits=[];
  let currentEpoch=3;
  if(transition.cancelRun)currentEpoch++;
  const result=await setup.runJobs({
    items:[{name:'keep'}],ids:[93],keys:['keep-key'],failed:[false],epoch:3,
    currentEpoch:()=>currentEpoch,key:()=> 'unused',submit:()=>Promise.reject(new Error('must not submit')),
    poll:id=>Promise.resolve({jobId:id}),resume,commit:value=>commits.push(value),
  });
  assert.equal(transition.changed,false);
  assert.deepEqual(result.results,[{jobId:93}]);
  assert.equal(commits.at(-1).ids[0],93);
});
