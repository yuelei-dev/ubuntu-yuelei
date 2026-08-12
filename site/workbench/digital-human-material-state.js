(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.DigitalHumanMaterialState=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function normalize(items,now){
    now=Number(now)||Math.floor(Date.now()/1000);
    return (Array.isArray(items)?items:[]).filter(function(item){
      return item&&/^img_[a-f0-9]{32}$/.test(String(item.upload_id||''))&&
        Number(item.expires_at)>now&&typeof item.name==='string';
    }).slice(0,6).map(function(item){return {
      upload_id:item.upload_id,name:item.name,sha256:String(item.sha256||''),
      expires_at:Number(item.expires_at),
    };});
  }
  function canChange(phase,busy){return !busy&&phase!=='approved';}
  function canAnalyze(phase,busy){return canChange(phase,busy);}
  function canStart(phase,busy,hasPlan){return !busy&&!!hasPlan;}
  return {normalize:normalize,canChange:canChange,canAnalyze:canAnalyze,canStart:canStart};
});
