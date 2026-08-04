(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  root.HQPricingGate=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  function create(options){
    options=options||{};
    var required=(options.requiredKeys||[]).slice();
    var fetchFn=options.fetchFn||function(url,init){return fetch(url,init);};
    var onState=typeof options.onState==='function'?options.onState:function(){};
    var url=options.url||'/api/gen/pricing';
    var state={status:'idle',values:null,error:''};
    var requestId=0;

    function snapshot(){
      return {status:state.status,values:state.values&&Object.assign({},state.values),error:state.error};
    }
    function emit(){onState(snapshot());}
    function validatedValues(payload){
      var values=payload&&payload.values;
      if(!values||typeof values!=='object'||Array.isArray(values)) throw new Error('invalid pricing payload');
      required.forEach(function(key){
        var value=values[key];
        if(!Object.prototype.hasOwnProperty.call(values,key)||typeof value!=='number'||
           !Number.isInteger(value)||value<1||value>100000){
          throw new Error('invalid pricing key: '+key);
        }
      });
      return Object.assign({},values);
    }
    function load(){
      var mine=++requestId;
      state={status:'loading',values:null,error:''}; emit();
      return Promise.resolve().then(function(){
        return fetchFn(url,{cache:'no-store'});
      }).then(function(response){
        if(!response||!response.ok) throw new Error('pricing http error');
        return response.json();
      }).then(function(payload){
        var values=validatedValues(payload);
        if(mine!==requestId) return snapshot();
        state={status:'ready',values:values,error:''}; emit();
        return snapshot();
      }).catch(function(){
        if(mine!==requestId) return snapshot();
        state={status:'error',values:null,error:'收费标准加载失败，暂不能提交付费生成，请重试。'}; emit();
        return snapshot();
      });
    }
    function guard(){return state.status==='ready'&&!!state.values;}
    return {load:load,guard:guard,getState:snapshot};
  }

  return {create:create};
});
