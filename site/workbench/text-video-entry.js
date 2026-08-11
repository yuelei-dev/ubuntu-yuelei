(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  else root.HQTextVideoEntry=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  var MODE='script_to_video';
  var TARGET_PATH='/workbench/script';

  function modeFromSearch(search){
    try{
      return new URLSearchParams(String(search||'')).get('mode')===MODE?MODE:'write';
    }catch(e){
      return 'write';
    }
  }

  function canonicalTarget(search,hash){
    var params=new URLSearchParams(String(search||''));
    params.set('mode',MODE);
    var suffix=String(hash||'');
    if(suffix&&suffix.charAt(0)!=='#') suffix='#'+suffix;
    return TARGET_PATH+'?'+params.toString()+suffix;
  }

  function keepModeAfterWrite(currentMode){
    return currentMode===MODE?MODE:'write';
  }

  return {
    MODE:MODE,
    TARGET_PATH:TARGET_PATH,
    modeFromSearch:modeFromSearch,
    canonicalTarget:canonicalTarget,
    keepModeAfterWrite:keepModeAfterWrite
  };
});
