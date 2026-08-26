(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQDirectorAgent=api; if(root.document) api.bootstrap(root.document,root); }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var STORAGE_KEY='hq_director_agent_v1';
  var DIGITAL_HUMAN_STORAGE_KEY='hq_director_agent_digital_human_v1';
  var DIGITAL_HUMAN_GUIDE_CONTRACT='digital-human-oneclick-guide-v1';
  var PRIVATE_DOMAIN_STORAGE_KEY='hq_director_agent_private_domain_v1';
  var ROUTES={
    script:'/workbench/script.html',digital_human:'/workbench/digital-human-oneclick.html',
    private_domain_video:'/workbench/private-domain-video.html',
    ip12:'/workbench/ip12.html',assets:'/workbench/assets.html',audio:'/workbench/audio.html',
    video:'/workbench/video.html',canvas:'/workbench/canvas.html'
  };
  var FOCUS={
    topic:'scTopic',selling_points:'scSell',generate_script:'scGen',breakdown_url:'bdUrl',
    analyze_breakdown:'bdGen',generate_video:'scGenVideo',generate_audio:'scGenAudio',export_script:'scExport',
    photo_upload:'photoDrop',voice_source:'voiceSource',voice_upload:'voiceUploadDrop',
    customer_materials:'customerMaterialsPicker',full_audio_upload:'driveAudioDrop',
    photo_authorization:'consent',analyze_plan:'analyze',generate_photo_video:'start',
    video_upload:'dhDrop',precision_authorization:'dhConsent',analyze_voice:'dhAnalyze',
    generate_precision_video:'dhStart',private_domain_copy:'copy',
    private_domain_randomize:'randomize',private_domain_plan:'plan'
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
  function createScriptPageContext(doc){
    var breakdown=doc.getElementById('panelBreakdown');
    var activeMode=doc.querySelector('#scModeTabs [data-mode].on');
    var requestedMode=String(activeMode&&activeMode.getAttribute('data-mode')||'');
    var mode=/^(write|script_to_video|breakdown)$/.test(requestedMode)
      ?requestedMode:(isVisible(breakdown)?'breakdown':'write');
    var activeBreakdownTool=doc.querySelector('#bdToolTabs [data-bd-tool].on');
    var requestedBreakdownTool=String(activeBreakdownTool&&activeBreakdownTool.getAttribute('data-bd-tool')||'');
    var breakdownTool=/^(scenes|reverse_prompt)$/.test(requestedBreakdownTool)?requestedBreakdownTool:'scenes';
    var hasReversePrompt=breakdownTool==='reverse_prompt'&&!!text(doc.getElementById('bdReversePromptText'));
    var sceneCount=countScenes(doc,'#scScenes');
    var breakdownCount=breakdownTool==='scenes'?countScenes(doc,'#scScenes'):0;
    var meta=doc.getElementById('scMeta');
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
      has_breakdown:mode==='breakdown'&&(breakdownCount>0||hasReversePrompt),
      breakdown_scene_count:mode==='breakdown'?breakdownCount:0,
      breakdown_url:text(doc.getElementById('bdUrl')).slice(0,2000),
      breakdown_tool:breakdownTool,has_reverse_prompt:mode==='breakdown'&&hasReversePrompt,
      active_job_status:busy?'running':'idle'
    };
  }
  function hasClass(node,name){
    return !!(node&&node.classList&&typeof node.classList.contains==='function'&&node.classList.contains(name));
  }
  function hasFile(node){ return !!(node&&node.files&&node.files.length); }
  function digitalHumanMode(doc){
    var active=doc.querySelector('[data-dh-mode].on');
    var requested=String(active&&active.getAttribute('data-dh-mode')||'');
    if(requested==='photo'||requested==='video') return requested;
    return isVisible(doc.getElementById('dhVideoMode'))?'video':'photo';
  }
  function digitalHumanJobStatus(doc,hasResult){
    if(doc.querySelector('.step.failed')) return 'failed';
    if(hasResult) return 'completed';
    if(doc.querySelector('.step.running')) return 'running';
    return 'idle';
  }
  function materialCount(doc){
    var match=text(doc.getElementById('customerMaterialCount')).match(/^\d+/);
    var count=match?Number(match[0]):0;
    return Math.max(0,Math.min(6,count));
  }
  function createDigitalHumanPageContext(doc){
    var contract=String(doc.body&&doc.body.getAttribute('data-director-guide-contract')||'');
    if(contract!==DIGITAL_HUMAN_GUIDE_CONTRACT) throw new Error('数字人顾客引导合同版本无效');
    var mode=digitalHumanMode(doc);
    var narration=doc.querySelector('input[name="narrationMode"]:checked');
    var narrationMode=String(narration&&narration.value||'text');
    if(narrationMode!=='audio') narrationMode='text';
    var scriptNode=doc.getElementById(mode==='video'?'dhScript':'script');
    var scriptText=text(scriptNode).slice(0,6000);
    var result=doc.getElementById(mode==='video'?'dhPrecisionResult':'result');
    var hasResult=hasClass(result,'show');
    var photoName=doc.getElementById('photoName'),videoName=doc.getElementById('dhVideoName');
    var voiceSource=doc.getElementById('voiceSource'),voiceSourceValue=text(voiceSource);
    var videoVoicePreview=doc.getElementById('dhVoicePreview');
    var activeTemplate=doc.querySelector('.precision-template.on');
    return {
      page:'digital_human_oneclick',path:'/workbench/digital-human-oneclick.html',guide_contract:contract,mode:mode,
      narration_mode:narrationMode,script_text:scriptText,script_length:scriptText.length,
      has_portrait:hasFile(doc.getElementById('photo'))||hasClass(photoName,'file-ready'),
      has_video_source:hasFile(doc.getElementById('dhVideoFile'))||hasClass(videoName,'file-ready')||!!doc.querySelector('.precision-source.on'),
      has_voice_source:mode==='video'
        ?!!(videoVoicePreview&&videoVoicePreview.disabled===false)
        :!!((voiceSourceValue&&voiceSourceValue!=='__clone__')||hasFile(doc.getElementById('voice'))),
      has_drive_audio:hasFile(doc.getElementById('driveAudio')),
      customer_material_count:materialCount(doc),
      consent_confirmed:!!(doc.getElementById(mode==='video'?'dhConsent':'consent')||{}).checked,
      precision_template:String(activeTemplate&&activeTemplate.getAttribute('data-template')||''),
      has_result:hasResult,active_job_status:digitalHumanJobStatus(doc,hasResult)
    };
  }
  function createPrivateDomainPageContext(doc){
    var copyText=text(doc.getElementById('copy')).slice(0,3000);
    var bgm=doc.getElementById('bgm'),bgmValues=[];
    if(bgm) Array.prototype.forEach.call(bgm.options||[],function(option){
      if(option.value&&option.value!=='random') bgmValues.push(String(option.value).slice(0,160));
    });
    var stateText=text(doc.getElementById('serverState'));
    var catalogStatus=/失败/.test(stateText)?'failed':(/预览/.test(stateText)?'preview':(/已连接/.test(stateText)?'ready':'loading'));
    return {
      page:'private_domain_video',path:'/workbench/private-domain-video.html',mode:'plan',
      copy_text:copyText,copy_count:copyText?copyText.split(/\n\s*\n/).filter(function(item){return item.trim();}).length:0,
      template:String((doc.getElementById('template')||{}).value||'data'),
      duration:String((doc.getElementById('duration')||{}).value||'8'),
      bgm:String((bgm||{}).value||'random'),bgm_values:bgmValues,
      asset_count:Number((doc.getElementById('materials')||{}).getAttribute&&doc.getElementById('materials').getAttribute('data-asset-count'))||0,
      selected_asset_count:Number((doc.getElementById('materials')||{}).getAttribute&&doc.getElementById('materials').getAttribute('data-selected-count'))||0,
      catalog_status:catalogStatus,active_job_status:'idle'
    };
  }
  function createPageContext(doc){
    if(doc&&doc.body&&doc.body.getAttribute('data-page')==='private_domain_video') return createPrivateDomainPageContext(doc);
    if(doc&&doc.getElementById('dhPhotoMode')) return createDigitalHumanPageContext(doc);
    return createScriptPageContext(doc);
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
      prompt:String(prompt||'').trim().slice(0,6000),session_id:sessionId(storage),
      page_revision:snapshot.page_revision,page_context:snapshot.page_context,
      history:(state.messages||[]).filter(function(item){return item.role==='user'||item.role==='assistant';})
        .slice(-10).map(function(item){return {role:item.role,content:String(item.content||'').slice(0,2000)};}),
      source_page:snapshot.page_context.page,
      provider:'openai_responses',quoted_cost:0
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
      var dataValue=String(node.getAttribute&&(node.getAttribute('data-mode')||node.getAttribute('data-bd-tool')||node.getAttribute('data-dh-mode')||node.getAttribute('data-template')||node.getAttribute('value'))||node.value||'').toLowerCase();
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
      var mode=doc.getElementById('dhPhotoMode')?digitalHumanMode(doc):'';
      var fields={topic:'scTopic',selling_points:'scSell',breakdown_url:'bdUrl',
        digital_human_script:mode==='video'?'dhScript':'script',private_domain_copy:'copy'};
      var field=doc.getElementById(fields[action.field]);
      if(!field) throw new Error('页面字段不存在');
      dispatchValue(field,action.value); return '已填入'+(action.label||'页面字段');
    }
    if(action.type==='choose_option'){
      var selectors={style:'#segStyle .sc-opt',duration:'#segDur .sc-opt',platform:'#platRow .sc-chip',
        breakdown_tool:'#bdToolTabs [data-bd-tool]',narration_mode:'input[name="narrationMode"]',
        precision_template:'.precision-template'};
      var privateSelect={private_domain_template:'template',private_domain_duration:'duration',private_domain_bgm:'bgm'}[action.field];
      if(privateSelect){
        var select=doc.getElementById(privateSelect),exists=false;
        if(!select) throw new Error('页面选项不存在');
        Array.prototype.forEach.call(select.options||[],function(option){if(String(option.value)===String(action.value))exists=true;});
        if(!exists) throw new Error('页面上没有找到“'+action.value+'”选项');
        dispatchValue(select,action.value); return '已选择 '+action.value;
      }
      if(!selectors[action.field]) throw new Error('页面选项无效');
      choose(doc,selectors[action.field],action.value); return '已选择 '+action.value;
    }
    if(action.type==='switch_mode'){
      var selector=(action.mode==='photo'||action.mode==='video')?'[data-dh-mode]':'#scModeTabs [data-mode]';
      choose(doc,selector,action.mode); return '已切换页面模式';
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
  function validPendingRequest(value){
    if(!value||typeof value!=='object'||Array.isArray(value)) return null;
    var key=String(value.key||''),body=value.body,jobId=value.job_id;
    if(!/^director-agent-[A-Za-z0-9_-]{8,100}$/.test(key)) return null;
    if(!body||typeof body!=='object'||Array.isArray(body)) return null;
    try{ if(JSON.stringify(body).length>48000) return null; }catch(error){ return null; }
    if(jobId!==null&&jobId!==undefined&&!/^[A-Za-z0-9_-]{1,80}$/.test(String(jobId))) return null;
    return {
      key:key,body:body,
      summary:value.summary&&typeof value.summary==='object'?value.summary:{},
      job_id:jobId===null||jobId===undefined?null:String(jobId),
      created_at:Number(value.created_at)||Date.now()
    };
  }
  function createPendingRequest(body,key,prompt,now){
    var copy=JSON.parse(JSON.stringify(body||{}));
    return validPendingRequest({
      key:key,body:copy,job_id:null,created_at:Number(now)||Date.now(),
      summary:{
        prompt:String(prompt||'').slice(0,2000),
        page_revision:String(copy.page_revision||'').slice(0,32),
        mode:String(copy.page_context&&copy.page_context.mode||'').slice(0,24)
      }
    });
  }
  function readState(storage,key){
    try{
      var value=JSON.parse(storage.getItem(key||STORAGE_KEY)||'null');
      if(value&&Array.isArray(value.messages)) return {
        messages:value.messages.slice(-20),open:!!value.open,
        pending_request:validPendingRequest(value.pending_request)
      };
    }catch(error){}
    return {messages:[],open:false,pending_request:null};
  }
  function saveState(storage,state,key){
    try{ storage.setItem(key||STORAGE_KEY,JSON.stringify({
      messages:state.messages.slice(-20),open:state.open,
      pending_request:validPendingRequest(state.pending_request)
    })); }catch(error){}
  }
  function jsonFetch(win,url,options){
    options=options||{}; var headers=options.headers||{};
    headers['Content-Type']='application/json';
    return win.fetch(url,{method:options.method||'GET',credentials:'same-origin',cache:'no-store',headers:headers,
      body:options.body===undefined?undefined:JSON.stringify(options.body)}).then(function(response){
      return response.text().then(function(raw){
        var data={}; try{data=raw?JSON.parse(raw):{};}catch(error){}
        if(!response.ok){
          var requestError=new Error(data.detail||('请求失败（'+response.status+'）'));
          requestError.status=response.status;
          requestError.data=data;
          throw requestError;
        }
        return data;
      });
    });
  }
  function bootstrap(doc,win,mounter){
    if(!doc||(!doc.getElementById('scTopic')&&!doc.getElementById('dhPhotoMode')&&!(doc.body&&doc.body.getAttribute('data-page')==='private_domain_video'))||!win||typeof win.fetch!=='function') return Promise.resolve(null);
    return jsonFetch(win,'/api/gen/health').then(function(health){
      if(!health||health.director_agent_enabled!==true) return null;
      return (mounter||mount)(doc,win);
    }).catch(function(){return null;});
  }
  function pollJob(win,jobId,onProgress){
    var started=Date.now(),transientFailures=0;
    return new Promise(function(resolve,reject){
      function timedOut(){ return Date.now()-started>300000; }
      function tick(){
        if(timedOut()){ var timeoutError=new Error('编导助手响应超时，请稍后重试'); timeoutError.terminal=false; reject(timeoutError); return; }
        jsonFetch(win,'/api/gen/job/'+encodeURIComponent(jobId)).then(function(job){
          transientFailures=0;
          if(job.status==='done'){
            var result=job.result; if(typeof result==='string') result=JSON.parse(result); resolve(result); return;
          }
          if(job.status==='error'||job.status==='failed'){ var jobError=new Error(job.error||'编导助手处理失败'); jobError.terminal=true; reject(jobError); return; }
          if(timedOut()){ reject(new Error('编导助手响应超时，请稍后重试')); return; }
          if(onProgress) onProgress(Math.floor((Date.now()-started)/1000));
          setTimeout(tick,1400);
        }).catch(function(error){
          transientFailures+=1;
          if(timedOut()){ error.terminal=false; reject(error); return; }
          if(error.status&&error.status<500){ error.terminal=true; reject(error); return; }
          if(onProgress) onProgress(Math.floor((Date.now()-started)/1000));
          setTimeout(tick,Math.min(5000,1400*transientFailures));
        });
      }
      tick();
    });
  }
  function resumeRequest(win,record,onRecord,onProgress){
    record=validPendingRequest(record);
    if(!record) return Promise.reject(new Error('未找到可恢复的编导助手请求'));
    var started=Date.now();
    function timedOut(){return Date.now()-started>300000;}
    function accepted(){
      if(record.job_id) return Promise.resolve(record);
      return jsonFetch(win,'/api/gen/director_agent',{
        method:'POST',body:record.body,headers:{'Idempotency-Key':record.key}
      }).then(function(data){
        if(!data.job_id) throw new Error(data.detail||'编导助手任务提交失败');
        record.job_id=String(data.job_id);
        if(onRecord) onRecord(record);
        return record;
      }).catch(function(error){
        var code=error.data&&error.data.code;
        var retryable=!error.status||error.status>=500||code==='idempotency_in_progress';
        if(retryable&&!timedOut()){
          if(onProgress) onProgress(0,'submitting');
          return new Promise(function(resolve){
            setTimeout(function(){resolve(accepted());},1400);
          });
        }
        error.terminal=!retryable;
        throw error;
      });
    }
    return accepted().then(function(){
      return pollJob(win,record.job_id,function(seconds){
        if(onProgress) onProgress(seconds,'polling');
      });
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
      +'.hq-agent-focus{outline:3px solid rgba(244,205,114,.78)!important;outline-offset:3px!important;box-shadow:0 0 0 7px rgba(231,178,76,.16)!important}@media(max-width:640px){.hq-da-launch{right:14px;bottom:14px}.hq-da-launch.digital-human{bottom:94px}.hq-da-panel{right:14px;bottom:70px;height:calc(100vh - 88px)}}';
    doc.head.appendChild(style);
  }
  function mount(doc,win){
    if((!doc.getElementById('scTopic')&&!doc.getElementById('dhPhotoMode')&&!(doc.body&&doc.body.getAttribute('data-page')==='private_domain_video'))||doc.getElementById('hqDirectorAgent')) return null;
    var page=createPageContext(doc).page,isDigitalHuman=page==='digital_human_oneclick',isPrivateDomain=page==='private_domain_video';
    addStyles(doc); var storage=win.sessionStorage;
    var storageKey=isPrivateDomain?PRIVATE_DOMAIN_STORAGE_KEY:(isDigitalHuman?DIGITAL_HUMAN_STORAGE_KEY:STORAGE_KEY);
    var state=readState(storage,storageKey),pending=false,currentPlan=null;
    function persist(){saveState(storage,state,storageKey);}
    var assistantName=isPrivateDomain?'私域成片助手':(isDigitalHuman?'数字人制作助手':'编导助手');
    var launch=doc.createElement('button'); launch.type='button'; launch.className='hq-da-launch'+(isDigitalHuman?' digital-human':''); launch.id='hqDirectorAgent'; launch.textContent='✦ '+assistantName; launch.setAttribute('aria-expanded',state.open?'true':'false');
    var panel=doc.createElement('section'); panel.className='hq-da-panel'+(state.open?' on':''); panel.setAttribute('aria-label',assistantName);
    var head=doc.createElement('div'); head.className='hq-da-head';
    var title=doc.createElement('div'); title.innerHTML='<b>'+assistantName+'</b><span>会看当前页面，但不会替你授权、扣点或生成</span>';
    var close=doc.createElement('button'); close.type='button'; close.className='hq-da-close'; close.textContent='×'; close.setAttribute('aria-label','关闭'); head.appendChild(title); head.appendChild(close);
    var messages=doc.createElement('div'); messages.className='hq-da-messages';
    var status=doc.createElement('div'); status.className='hq-da-status';
    var compose=doc.createElement('div'); compose.className='hq-da-compose';
    var input=doc.createElement('textarea'); input.className='hq-da-input'; input.rows=2; input.maxLength=6000; input.placeholder=isPrivateDomain?'把批量文案发给我，或告诉我想用的模板和音乐':(isDigitalHuman?'把文案发给我，或问我下一步怎么做':'例如：我第一次用，下一步该做什么？');
    var send=doc.createElement('button'); send.type='button'; send.className='hq-da-send'; send.textContent='发送'; compose.appendChild(input); compose.appendChild(send);
    panel.appendChild(head); panel.appendChild(messages); panel.appendChild(status); panel.appendChild(compose); doc.body.appendChild(launch); doc.body.appendChild(panel);
    function setOpen(open){state.open=!!open; panel.classList.toggle('on',state.open); launch.setAttribute('aria-expanded',state.open?'true':'false'); persist(); if(state.open) input.focus();}
    function addMessage(role,content){state.messages.push({role:role,content:String(content||'')}); state.messages=state.messages.slice(-20); persist(); render();}
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
        var welcome=doc.createElement('div'); welcome.className='hq-da-msg assistant'; welcome.textContent=isPrivateDomain
          ?'你好，把批量文案发给我，我可以直接填入，也能帮你切换模板、时长和音乐。随机素材和生成方案仍由你点击确认。'
          :(isDigitalHuman?'你好，把口播文案发给我，我可以直接填入当前数字人模式，也能根据页面状态告诉你还缺什么。上传、授权和生成仍由你点击确认。'
          :'你好，我能根据你现在填写的内容，告诉你怎么生成脚本、拆解视频，或下一步该去哪里。'); messages.appendChild(welcome);
        var quick=doc.createElement('div'); quick.className='hq-da-actions';
        (isPrivateDomain
          ?['帮我填入这批文案','帮我选择排版和时长','帮我看看还缺什么']
          :(isDigitalHuman
          ?['我第一次用，带我走一遍','帮我看看还缺什么','照片模式和真人视频模式怎么选']
          :['我第一次用，带我走一遍','帮我看看还缺什么','生成脚本后怎么做视频']
        )).forEach(function(label){var b=doc.createElement('button');b.type='button';b.className='hq-da-quick';b.textContent=label;b.onclick=function(){submit(label);};quick.appendChild(b);});
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
    function handleResult(result){
      state.pending_request=null; persist();
      currentPlan=result&&result.plan||null;
      addMessage('assistant',result&&result.content||'我已经看完当前页面。');
      if(currentPlan&&currentPlan.actions.length){
        try{
          validatePlan(currentPlan,doc);
          var applied=currentPlan.actions.map(function(action){return applyAction(action,doc,win);});
          status.textContent=applied.join('；')+'。涉及扣点或生成时，仍需要你点击原页面按钮确认。';
          currentPlan=null;
          render();
        }catch(error){
          status.textContent=error.message||'自动操作失败，请重新告诉我你的要求';
        }
      }else{
        status.textContent='';
      }
    }
    function runPending(record,resumed){
      record=validPendingRequest(record);
      if(!record) return;
      state.pending_request=record; persist();
      pending=true;
      status.textContent=resumed?'正在恢复上次未完成的请求…':'正在结合当前页面判断…';
      render();
      resumeRequest(win,record,function(updated){
        state.pending_request=validPendingRequest(updated);
        persist();
      },function(seconds,phase){
        status.textContent=phase==='submitting'
          ?'正在确认上次提交结果…'
          :'编导助手思考中，已用 '+seconds+' 秒…';
      }).then(handleResult).catch(function(error){
        if(error.terminal) state.pending_request=null;
        persist();
        addMessage('error',error.message||'编导助手请求失败，请稍后重试');
        status.textContent=state.pending_request
          ?'原请求已保留，刷新页面后会继续，不会创建新的幂等键。':'';
      }).finally(function(){pending=false;render();});
    }
    function submit(value){
      value=String(value||input.value||'').trim(); if(!value||pending) return;
      var body=buildPayload(value,doc,state,storage);
      var key='director-agent-'+Date.now().toString(36)+Math.random().toString(36).slice(2,10);
      var record=createPendingRequest(body,key,value);
      if(!record){ addMessage('error','编导助手请求摘要保存失败，请重试'); return; }
      input.value=''; currentPlan=null; addMessage('user',value);
      state.pending_request=record; persist();
      runPending(record,false);
    }
    launch.onclick=function(){setOpen(!state.open);}; close.onclick=function(){setOpen(false);}; send.onclick=function(){submit();};
    input.addEventListener('keydown',function(event){if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();submit();}});
    render();
    if(state.pending_request) runPending(state.pending_request,true);
    return {
      state:state,submit:submit,setOpen:setOpen,resume:function(){runPending(state.pending_request,true);}
    };
  }
  return {digest:digest,createPageContext:createPageContext,createPageSnapshot:createPageSnapshot,
    buildPayload:buildPayload,validatePlan:validatePlan,applyAction:applyAction,pollJob:pollJob,
    validPendingRequest:validPendingRequest,createPendingRequest:createPendingRequest,
    readState:readState,saveState:saveState,resumeRequest:resumeRequest,
    bootstrap:bootstrap,mount:mount,routes:ROUTES};
});
