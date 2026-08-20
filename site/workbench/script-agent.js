(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQDirectorAgent=api; if(root.document) api.mount(root.document,root); }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var STORAGE_KEY='hq_director_agent_v1';
  var ROUTES={
    ip12:'/workbench/ip12.html',assets:'/workbench/assets.html',audio:'/workbench/audio.html',
    video:'/workbench/video.html',canvas:'/workbench/canvas.html'
  };
  var FOCUS={
    topic:'scTopic',selling_points:'scSell',generate_script:'scGen',breakdown_url:'bdUrl',
    analyze_breakdown:'bdGen',generate_video:'scGenVideo',generate_audio:'scGenAudio',export_script:'scExport'
  };

  function digest(value){
    var text=JSON.stringify(value),hash=2166136261;
    for(var i=0;i<text.length;i++){ hash^=text.charCodeAt(i); hash=Math.imul(hash,16777619); }
    return ('00000000'+(hash>>>0).toString(16)).slice(-8);
  }
  function text(node){ return String(node&&node.value!=null?node.value:node&&node.textContent||'').trim(); }
  function activeText(doc,selector){ var node=doc.querySelector(selector+' .on'); return text(node); }
  function isVisible(node){
    if(!node) return false;
    if(node.style&&node.style.display==='none') return false;
    return !(node.hidden||node.getAttribute&&node.getAttribute('aria-hidden')==='true');
  }
  function countScenes(doc,selector){
    return Array.prototype.filter.call(doc.querySelectorAll(selector+' .sc-card'),function(node){
      return !(node.getAttribute&&node.getAttribute('data-placeholder')==='1');
    }).length;
  }
  function createPageContext(doc){
    var breakdown=doc.getElementById('panelBreakdown');
    var activeMode=doc.querySelector('#scModeTabs [data-mode].on');
    var requestedMode=String(activeMode&&activeMode.getAttribute('data-mode')||'');
    var mode=/^(write|script_to_video|breakdown)$/.test(requestedMode)
      ?requestedMode:(isVisible(breakdown)?'breakdown':'write');
    var sceneCount=countScenes(doc,'#scScenes');
    var breakdownCount=countScenes(doc,'#scScenes');
    var meta=doc.getElementById('scMeta'),analysis=doc.getElementById('bdAnalysis');
    var busy=['scGen','bdGen','scGenVideo','scGenAudio','bdImageReverse','bdVideoReverse']
      .some(function(id){var node=doc.getElementById(id);return !!(node&&node.disabled);});
    return {
      page:'script',path:'/workbench/script.html',mode:mode,
      topic:text(doc.getElementById('scTopic')).slice(0,1000),
      selling_points:text(doc.getElementById('scSell')).slice(0,2000),
      style:activeText(doc,'#segStyle').slice(0,40),
      duration:activeText(doc,'#segDur').slice(0,20),
      platform:activeText(doc,'#platRow').slice(0,40),
      has_script:mode!=='breakdown'&&isVisible(meta)&&sceneCount>0,scene_count:mode!=='breakdown'?sceneCount:0,
      has_breakdown:mode==='breakdown'&&isVisible(analysis)&&breakdownCount>0,
      breakdown_scene_count:mode==='breakdown'?breakdownCount:0,
      breakdown_url:text(doc.getElementById('bdUrl')).slice(0,2000),
      active_job_status:busy?'running':'idle'
    };
  }
  function createPageSnapshot(doc){
    var context=createPageContext(doc);
    return {page_context:context,page_revision:digest(context)};
  }
  function sessionId(storage){
    var stored='';
    try{ stored=storage.getItem('hq_director_agent_session')||''; }catch(error){}
    if(/^[A-Za-z0-9_-]{8,80}$/.test(stored)) return stored;
    stored='director_'+Date.now().toString(36)+Math.random().toString(36).slice(2,12);
    try{ storage.setItem('hq_director_agent_session',stored); }catch(error){}
    return stored;
  }
  function buildPayload(prompt,doc,state,storage){
    var snapshot=createPageSnapshot(doc);
    return {
      prompt:String(prompt||'').trim().slice(0,2000),session_id:sessionId(storage),
      page_revision:snapshot.page_revision,page_context:snapshot.page_context,
      history:(state.messages||[]).filter(function(item){return item.role==='user'||item.role==='assistant';})
        .slice(-10).map(function(item){return {role:item.role,content:String(item.content||'').slice(0,2000)};}),
      source_page:'script',provider:'openai_responses',quoted_cost:0
    };
  }
  function validatePlan(plan,doc){
    if(!plan||!Array.isArray(plan.actions)||plan.actions.length>6) throw new Error('编导助手方案无效，请重新询问');
    if(plan.page_revision!==createPageSnapshot(doc).page_revision) throw new Error('页面内容已变化，请重新让编导助手判断');
    return true;
  }
  function dispatchValue(node,value){
    node.value=String(value||'');
    if(typeof node.dispatchEvent==='function'){
      var EventCtor=node.ownerDocument&&node.ownerDocument.defaultView&&node.ownerDocument.defaultView.Event;
      if(EventCtor){ node.dispatchEvent(new EventCtor('input',{bubbles:true})); node.dispatchEvent(new EventCtor('change',{bubbles:true})); }
    }
    if(typeof node.focus==='function') node.focus();
  }
  function choose(doc,selector,value){
    var wanted=String(value||'').replace(/\s+/g,'').toLowerCase(),found=null;
    if(!wanted) throw new Error('页面选项不能为空');
    var nodes=Array.prototype.slice.call(doc.querySelectorAll(selector));
    function parts(node){
      var current=text(node).replace(/\s+/g,'').toLowerCase();
      var dataValue=String(node.getAttribute&&node.getAttribute('data-mode')||'').toLowerCase();
      return {node:node,current:current,dataValue:dataValue};
    }
    for(var i=0;i<nodes.length;i++){
      var exact=parts(nodes[i]);
      if(exact.dataValue===wanted||exact.current===wanted){ found=exact.node; break; }
    }
    for(var j=0;!found&&j<nodes.length;j++){
      var partial=parts(nodes[j]);
      if(partial.current&&(partial.current.indexOf(wanted)>=0||wanted.indexOf(partial.current)>=0)){
        found=partial.node; break;
      }
    }
    if(!found) throw new Error('页面上没有找到“'+value+'”选项');
    if(typeof found.click==='function') found.click();
    return found;
  }
  function applyAction(action,doc,win){
    if(!action||!action.type) throw new Error('编导助手动作无效');
    if(action.type==='fill_field'){
      var fields={topic:'scTopic',selling_points:'scSell',breakdown_url:'bdUrl'};
      var field=doc.getElementById(fields[action.field]);
      if(!field) throw new Error('页面字段不存在');
      dispatchValue(field,action.value); return '已填入'+(action.label||'页面字段');
    }
    if(action.type==='choose_option'){
      var selectors={style:'#segStyle .sc-opt',duration:'#segDur .sc-opt',platform:'#platRow .sc-chip'};
      if(!selectors[action.field]) throw new Error('页面选项无效');
      choose(doc,selectors[action.field],action.value); return '已选择 '+action.value;
    }
    if(action.type==='switch_mode'){
      choose(doc,'#scModeTabs [data-mode]',action.mode); return '已切换编导模式';
    }
    if(action.type==='focus'){
      var node=doc.getElementById(FOCUS[action.target]);
      if(!node) throw new Error('页面目标不存在');
      if(typeof node.scrollIntoView==='function') node.scrollIntoView({behavior:'smooth',block:'center'});
      if(typeof node.focus==='function') node.focus();
      node.classList&&node.classList.add('hq-agent-focus');
      setTimeout(function(){node.classList&&node.classList.remove('hq-agent-focus');},1800);
      return '已定位到页面操作';
    }
    if(action.type==='navigate'){
      if(!ROUTES[action.target]) throw new Error('站内目标无效');
      if(win&&win.location) win.location.href=ROUTES[action.target];
      return '正在前往下一步';
    }
    throw new Error('不允许执行这个动作');
  }
  function readState(storage){
    try{
      var value=JSON.parse(storage.getItem(STORAGE_KEY)||'null');
      if(value&&Array.isArray(value.messages)) return {messages:value.messages.slice(-20),open:!!value.open};
    }catch(error){}
    return {messages:[],open:false};
  }
  function saveState(storage,state){
    try{ storage.setItem(STORAGE_KEY,JSON.stringify({messages:state.messages.slice(-20),open:state.open})); }catch(error){}
  }
  function jsonFetch(win,url,options){
    options=options||{}; var headers=options.headers||{};
    headers['Content-Type']='application/json';
    return win.fetch(url,{method:options.method||'GET',credentials:'same-origin',cache:'no-store',headers:headers,
      body:options.body===undefined?undefined:JSON.stringify(options.body)}).then(function(response){
      return response.text().then(function(raw){
        var data={}; try{data=raw?JSON.parse(raw):{};}catch(error){}
        if(!response.ok) throw new Error(data.detail||('请求失败（'+response.status+'）'));
        return data;
      });
    });
  }
  function pollJob(win,jobId,onProgress){
    var started=Date.now(),transientFailures=0;
    return new Promise(function(resolve,reject){
      function timedOut(){ return Date.now()-started>300000; }
      function tick(){
        if(timedOut()){ reject(new Error('编导助手响应超时，请稍后重试')); return; }
        jsonFetch(win,'/api/gen/job/'+encodeURIComponent(jobId)).then(function(job){
          transientFailures=0;
          if(job.status==='done'){
            var result=job.result; if(typeof result==='string') result=JSON.parse(result); resolve(result); return;
          }
          if(job.status==='error'||job.status==='failed'){ reject(new Error(job.error||'编导助手处理失败')); return; }
          if(timedOut()){ reject(new Error('编导助手响应超时，请稍后重试')); return; }
          if(onProgress) onProgress(Math.floor((Date.now()-started)/1000));
          setTimeout(tick,1400);
        }).catch(function(error){
          transientFailures+=1;
          if(timedOut()){ reject(new Error('编导助手响应超时，请稍后重试')); return; }
          if(transientFailures>=3){ reject(error); return; }
          if(onProgress) onProgress(Math.floor((Date.now()-started)/1000));
          setTimeout(tick,1400);
        });
      }
      tick();
    });
  }
  function addStyles(doc){
    if(doc.getElementById('hqDirectorAgentStyle')) return;
    var style=doc.createElement('style'); style.id='hqDirectorAgentStyle';
    style.textContent=''
      +'.hq-da-launch{position:fixed;right:24px;bottom:24px;z-index:8800;border:0;border-radius:999px;padding:12px 17px;background:linear-gradient(135deg,#f4cd72,#e7b24c);color:#241604;font:700 14px/1.2 inherit;box-shadow:0 16px 42px rgba(0,0,0,.38);cursor:pointer}'
      +'.hq-da-panel{position:fixed;right:24px;bottom:82px;z-index:8801;width:min(390px,calc(100vw - 28px));height:min(620px,calc(100vh - 112px));display:none;flex-direction:column;border:1px solid rgba(231,178,76,.25);border-radius:18px;background:#0b111c;color:#eaf1fa;box-shadow:0 24px 70px rgba(0,0,0,.55);overflow:hidden}'
      +'.hq-da-panel.on{display:flex}.hq-da-head{display:flex;align-items:center;justify-content:space-between;padding:15px 16px;border-bottom:1px solid rgba(148,164,187,.13);background:linear-gradient(135deg,rgba(231,178,76,.12),rgba(11,17,28,.96))}'
      +'.hq-da-head b{font-size:15px}.hq-da-head span{display:block;margin-top:3px;color:#94a4bb;font-size:11px}.hq-da-close{border:0;background:transparent;color:#94a4bb;font-size:22px;cursor:pointer}'
      +'.hq-da-messages{flex:1;overflow:auto;padding:14px;display:flex;flex-direction:column;gap:10px}.hq-da-msg{max-width:88%;padding:10px 12px;border-radius:13px;font-size:13px;line-height:1.65;white-space:pre-wrap}.hq-da-msg.user{align-self:flex-end;background:#e7b24c;color:#211502}.hq-da-msg.assistant{align-self:flex-start;background:#141e2e;border:1px solid rgba(148,164,187,.12)}.hq-da-msg.error{align-self:flex-start;background:rgba(244,112,138,.12);color:#ffc1ce}'
      +'.hq-da-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:7px}.hq-da-action,.hq-da-quick{border:1px solid rgba(231,178,76,.32);border-radius:999px;background:rgba(231,178,76,.08);color:#f4cd72;padding:7px 10px;font:600 11.5px/1 inherit;cursor:pointer}.hq-da-action[disabled]{opacity:.45;cursor:not-allowed}'
      +'.hq-da-status{min-height:18px;padding:0 14px;color:#94a4bb;font-size:11px}.hq-da-compose{display:flex;gap:8px;padding:12px 14px 14px;border-top:1px solid rgba(148,164,187,.13)}.hq-da-input{flex:1;min-width:0;resize:none;border:1px solid rgba(148,164,187,.18);border-radius:12px;background:#070b13;color:#eaf1fa;padding:10px;font:13px/1.5 inherit;outline:none}.hq-da-send{border:0;border-radius:12px;background:#e7b24c;color:#241604;padding:0 14px;font-weight:700;cursor:pointer}.hq-da-send[disabled]{opacity:.5;cursor:not-allowed}'
      +'.hq-agent-focus{outline:3px solid rgba(244,205,114,.78)!important;outline-offset:3px!important;box-shadow:0 0 0 7px rgba(231,178,76,.16)!important}@media(max-width:640px){.hq-da-launch{right:14px;bottom:14px}.hq-da-panel{right:14px;bottom:70px;height:calc(100vh - 88px)}}';
    doc.head.appendChild(style);
  }
  function mount(doc,win){
    if(!doc.getElementById('scTopic')||doc.getElementById('hqDirectorAgent')) return null;
    addStyles(doc); var storage=win.sessionStorage,state=readState(storage),pending=false,currentPlan=null;
    var launch=doc.createElement('button'); launch.type='button'; launch.className='hq-da-launch'; launch.id='hqDirectorAgent'; launch.textContent='✦ 编导助手'; launch.setAttribute('aria-expanded',state.open?'true':'false');
    var panel=doc.createElement('section'); panel.className='hq-da-panel'+(state.open?' on':''); panel.setAttribute('aria-label','编导助手');
    var head=doc.createElement('div'); head.className='hq-da-head';
    var title=doc.createElement('div'); title.innerHTML='<b>编导助手</b><span>会看当前页面，但不会替你扣点或生成</span>';
    var close=doc.createElement('button'); close.type='button'; close.className='hq-da-close'; close.textContent='×'; close.setAttribute('aria-label','关闭'); head.appendChild(title); head.appendChild(close);
    var messages=doc.createElement('div'); messages.className='hq-da-messages';
    var status=doc.createElement('div'); status.className='hq-da-status';
    var compose=doc.createElement('div'); compose.className='hq-da-compose';
    var input=doc.createElement('textarea'); input.className='hq-da-input'; input.rows=2; input.maxLength=2000; input.placeholder='例如：我第一次用，下一步该做什么？';
    var send=doc.createElement('button'); send.type='button'; send.className='hq-da-send'; send.textContent='发送'; compose.appendChild(input); compose.appendChild(send);
    panel.appendChild(head); panel.appendChild(messages); panel.appendChild(status); panel.appendChild(compose); doc.body.appendChild(launch); doc.body.appendChild(panel);
    function setOpen(open){state.open=!!open; panel.classList.toggle('on',state.open); launch.setAttribute('aria-expanded',state.open?'true':'false'); saveState(storage,state); if(state.open) input.focus();}
    function addMessage(role,content){state.messages.push({role:role,content:String(content||'')}); state.messages=state.messages.slice(-20); saveState(storage,state); render();}
    function actionButton(action){
      var button=doc.createElement('button'); button.type='button'; button.className='hq-da-action'; button.textContent=action.label||'应用建议';
      button.onclick=function(){
        try{validatePlan(currentPlan,doc); var result=applyAction(action,doc,win); button.disabled=true; status.textContent=result+'。需要扣点或生成时，请再点击页面原按钮确认。';}
        catch(error){status.textContent=error.message||'应用建议失败';}
      }; return button;
    }
    function render(){
      messages.textContent='';
      if(!state.messages.length){
        var welcome=doc.createElement('div'); welcome.className='hq-da-msg assistant'; welcome.textContent='你好，我能根据你现在填写的内容，告诉你怎么生成脚本、拆解视频，或下一步该去哪里。'; messages.appendChild(welcome);
        var quick=doc.createElement('div'); quick.className='hq-da-actions';
        ['我第一次用，带我走一遍','帮我看看还缺什么','生成脚本后怎么做视频'].forEach(function(label){var b=doc.createElement('button');b.type='button';b.className='hq-da-quick';b.textContent=label;b.onclick=function(){submit(label);};quick.appendChild(b);});
        messages.appendChild(quick);
      }
      state.messages.forEach(function(message,index){
        var box=doc.createElement('div'); box.className='hq-da-msg '+message.role; box.textContent=message.content; messages.appendChild(box);
        if(message.role==='assistant'&&index===state.messages.length-1&&currentPlan&&currentPlan.actions.length){
          var actions=doc.createElement('div'); actions.className='hq-da-actions'; currentPlan.actions.forEach(function(action){actions.appendChild(actionButton(action));}); messages.appendChild(actions);
        }
      });
      messages.scrollTop=messages.scrollHeight; send.disabled=pending; input.disabled=pending;
    }
    function submit(value){
      value=String(value||input.value||'').trim(); if(!value||pending) return;
      var body=buildPayload(value,doc,state,storage),key='director-agent-'+Date.now().toString(36)+Math.random().toString(36).slice(2,10);
      input.value=''; currentPlan=null; addMessage('user',value); pending=true; status.textContent='正在结合当前页面判断…'; render();
      jsonFetch(win,'/api/gen/director_agent',{method:'POST',body:body,headers:{'Idempotency-Key':key}}).then(function(data){
        if(!data.job_id) throw new Error(data.detail||'编导助手任务提交失败');
        return pollJob(win,data.job_id,function(seconds){status.textContent='编导助手思考中，已用 '+seconds+' 秒…';});
      }).then(function(result){
        currentPlan=result&&result.plan||null;
        addMessage('assistant',result&&result.content||'我已经看完当前页面。');
        if(currentPlan&&currentPlan.actions.length){
          try{
            validatePlan(currentPlan,doc);
            var applied=currentPlan.actions.map(function(action){return applyAction(action,doc,win);});
            status.textContent=applied.join('；')+'。涉及扣点或生成时，仍需要你点击原页面按钮确认。';
            currentPlan=null;
            render();
          }catch(error){ status.textContent=error.message||'自动操作失败，请重新告诉我你的要求'; }
        }else{
          status.textContent='';
        }
      }).catch(function(error){addMessage('error',error.message||'编导助手请求失败，请稍后重试');status.textContent='';}).finally(function(){pending=false;render();});
    }
    launch.onclick=function(){setOpen(!state.open);}; close.onclick=function(){setOpen(false);}; send.onclick=function(){submit();};
    input.addEventListener('keydown',function(event){if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();submit();}});
    render(); return {state:state,submit:submit,setOpen:setOpen};
  }
  return {digest:digest,createPageContext:createPageContext,createPageSnapshot:createPageSnapshot,
    buildPayload:buildPayload,validatePlan:validatePlan,applyAction:applyAction,pollJob:pollJob,mount:mount,routes:ROUTES};
});
