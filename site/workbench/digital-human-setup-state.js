(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.DigitalHumanSetupState=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function view(phase){
    var locked=phase==='approved';
    return {locked:locked,voiceSourceDisabled:locked,voiceSampleDisabled:locked,restartHidden:!locked};
  }
  function applyControls(nodes,phase){
    if(!nodes||!nodes.voiceSource||!nodes.voiceSample||!nodes.restart)throw new Error('setup controls are required');
    var next=view(phase);
    nodes.voiceSource.disabled=next.voiceSourceDisabled;
    nodes.voiceSample.disabled=next.voiceSampleDisabled;
    nodes.restart.hidden=next.restartHidden;
    return next;
  }
  function restart(current,confirmed){
    var state=Object.assign({},current||{});
    if(state.phase!=='approved'||!confirmed)return {changed:false,state:state,submit:false,charge:false,cancelRun:false};
    return {changed:true,state:{phase:'input'},submit:false,charge:false,cancelRun:true};
  }
  function canContinue(runEpoch,currentEpoch){return Number(runEpoch)===Number(currentEpoch);}
  function cancelled(){var error=new Error('本次生成已被用户放弃');error.generationCancelled=true;return error;}
  function runJobs(options){
    var items=options.items||[],ids=(options.ids||[]).slice(),keys=(options.keys||[]).slice(),failed=(options.failed||[]).slice(),results=[];
    function guard(){if(!canContinue(options.epoch,options.currentEpoch()))throw cancelled();}
    function commit(){guard();options.commit({ids:ids.slice(),keys:keys.slice(),failed:failed.slice()});}
    function launch(item,index,retry){
      guard();
      if(retry||!keys[index])keys[index]=options.key(index);
      failed[index]=false;ids[index]=0;commit();
      return options.submit(item,index,keys[index]).then(function(id){
        guard();ids[index]=id;commit();return options.poll(id);
      });
    }
    return Promise.all(items.map(function(item,index){
      var active=Number(ids[index])||0;
      return options.resume({
        jobId:active,
        retryApproved:!!failed[index],
        poll:options.poll,
        launch:function(retry){return launch(item,index,retry);},
        markTerminal:function(){guard();failed[index]=true;commit();}
      }).then(function(result){
        guard();results[index]=result;
        if(options.onCount)options.onCount(results.filter(Boolean).length,items.length);
      });
    })).then(function(){guard();commit();return {ids:ids,results:results};}).catch(function(error){guard();throw error;});
  }
  return {view:view,applyControls:applyControls,restart:restart,canContinue:canContinue,runJobs:runJobs};
});
