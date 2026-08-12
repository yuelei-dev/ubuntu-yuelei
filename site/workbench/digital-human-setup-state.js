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
  return {view:view,applyControls:applyControls,restart:restart,canContinue:canContinue};
});
