(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.DigitalHumanVoiceState=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function keyForSlot(slot){return 'vip_'+String(slot&&slot.slot_id||'').replace(/[^a-zA-Z0-9_-]/g,'_');}
  function readyVoices(items){
    return (Array.isArray(items)?items:[]).filter(function(item){
      return item&&item.slot_id&&item.status==='ready'&&item.voice_id;
    }).map(function(item,index){return {
      key:keyForSlot(item),name:item.voice_name||('我的声音 '+(index+1)),preview_url:item.preview_url||'',
    };});
  }
  function snapshot(current){return {
    phase:String(current&&current.phase||'input'),
    voiceMode:String(current&&current.voiceMode||((current&&current.voiceKey)?'existing':'')),
    voiceKey:String(current&&current.voiceKey||''),
  };}
  function resolveLoaded(current,items){
    var frozen=snapshot(current),voices=readyVoices(items),locked=frozen.phase==='approved';
    if(locked){
      if(frozen.voiceMode==='existing'&&frozen.voiceKey){
        var found=voices.some(function(item){return item.key===frozen.voiceKey;});
        return {selection:frozen,voices:voices,locked:true,valid:found,error:found?'':'已冻结的个人声音当前不可用，请恢复该声音后再继续'};
      }
      if(frozen.voiceMode==='clone')return {selection:frozen,voices:voices,locked:true,valid:false,error:'重新复刻样音未完成，刷新后无法恢复本地样音文件，请重新开始本次制作'};
      return {selection:frozen,voices:voices,locked:true,valid:false,error:'已批准任务缺少冻结的声音参数，无法继续'};
    }
    if(frozen.voiceMode==='existing'&&frozen.voiceKey&&voices.some(function(item){return item.key===frozen.voiceKey;})){
      return {selection:frozen,voices:voices,locked:false,valid:true,error:''};
    }
    var next={phase:frozen.phase,voiceMode:voices.length?'existing':'clone',voiceKey:voices.length?voices[0].key:''};
    return {selection:next,voices:voices,locked:false,valid:true,error:''};
  }
  function loadFailed(current,message){
    var frozen=snapshot(current);
    return {selection:frozen,voices:[],locked:frozen.phase==='approved',valid:false,error:'读取资产库声音失败，声音选择未改变：'+String(message||'请稍后重试')};
  }
  function change(current,value){
    var frozen=snapshot(current);
    if(frozen.phase==='approved')return {accepted:false,selection:frozen,error:'生成已开始，声音选择已冻结'};
    var clone=value==='__clone__';
    return {accepted:true,selection:{phase:frozen.phase,voiceMode:clone?'clone':'existing',voiceKey:clone?'':String(value||'')},error:''};
  }
  return {keyForSlot:keyForSlot,readyVoices:readyVoices,resolveLoaded:resolveLoaded,loadFailed:loadFailed,change:change};
});
