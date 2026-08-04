(function(){
  'use strict';

  function esc(value){
    return String(value == null ? '' : value).replace(/[&<>"']/g,function(ch){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];
    });
  }
  function api(path, options){
    options=options||{};
    options.credentials='same-origin';
    options.headers=Object.assign({'Authorization':'Bearer __cookie__'},options.headers||{});
    return fetch(path,options).then(function(response){
      return response.json().catch(function(){return {detail:'响应不是 JSON'}}).then(function(data){
        if(!response.ok){var error=new Error(data.detail||('HTTP '+response.status));error.status=response.status;throw error}
        return data;
      });
    });
  }
  function fmtTime(value){
    var n=Number(value||0);if(!n)return '-';
    return new Date(n>100000000000?n:n*1000).toLocaleString();
  }
  function notify(message){
    var node=document.getElementById('pricingMessage');
    if(node)node.textContent=message||'';
  }

  var state={items:[],audit:[],loading:false};

  function groupItems(items){
    var groups=[];
    items.forEach(function(item){
      var group=groups.filter(function(x){return x.name===item.group})[0];
      if(!group){group={name:item.group,items:[]};groups.push(group)}
      group.items.push(item);
    });
    return groups;
  }

  function render(data){
    state.items=data.items||[];state.audit=data.audit||[];
    var box=document.getElementById('pricingBox');
    if(!box)return;
    box.innerHTML=groupItems(state.items).map(function(group){
      return '<div style="margin:0 0 18px"><h3 style="margin:0 0 8px;font-size:14px;color:var(--gold)">'+esc(group.name)+'</h3>'+
        '<div class="table-wrap"><table><thead><tr><th>功能</th><th>计费单位</th><th>当前点数</th><th>代码默认</th><th>状态</th><th>最近修改</th><th>操作</th></tr></thead><tbody>'+
        group.items.map(function(item){
          return '<tr data-pricing-key="'+esc(item.key)+'" data-version="'+Number(item.version||0)+'" data-current="'+Number(item.points||0)+'">'+
            '<td><b>'+esc(item.label)+'</b><div class="muted"><code>'+esc(item.key)+'</code></div></td>'+
            '<td>'+esc(item.unit)+'</td>'+
            '<td><input class="field pricing-value" type="number" min="1" max="100000" step="1" value="'+Number(item.points||0)+'" style="min-width:100px;width:110px"></td>'+
            '<td><code>'+Number(item.default_points||0)+'</code></td>'+
            '<td>'+(item.configured?'<span class="pill warn">已调整</span>':'<span class="pill ok">默认值</span>')+'</td>'+
            '<td><div>'+esc(item.updated_by||'-')+'</div><div class="muted">'+esc(fmtTime(item.updated_at))+'</div></td>'+
            '<td><div class="actions"><button class="mini primary" data-pricing-save>保存</button><button class="mini" data-pricing-reset '+(item.configured?'':'disabled')+'>恢复默认</button></div></td>'+
          '</tr>';
        }).join('')+'</tbody></table></div></div>';
    }).join('');
    renderAudit();
    notify('共 '+state.items.length+' 项 · 保存后新受理任务立即按新标准扣点，无需重启服务');
  }

  function renderAudit(){
    var box=document.getElementById('pricingAudit');if(!box)return;
    if(!state.audit.length){box.innerHTML='<div class="empty">暂无收费调整记录</div>';return}
    box.innerHTML='<div class="table-wrap"><table><thead><tr><th>时间</th><th>项目</th><th>变化</th><th>操作者</th><th>原因</th></tr></thead><tbody>'+
      state.audit.map(function(item){return '<tr><td><code>'+esc(fmtTime(item.created_at))+'</code></td><td><code>'+esc(item.pricing_key)+'</code></td><td><b>'+Number(item.before_points||0)+' → '+Number(item.after_points||0)+'</b></td><td>'+esc(item.actor)+'</td><td>'+esc(item.reason)+'</td></tr>'}).join('')+
      '</tbody></table></div>';
  }

  function load(){
    if(state.loading)return;state.loading=true;notify('正在读取收费标准…');
    api('/api/admin/pricing').then(render).catch(function(error){
      notify('读取失败：'+error.message);
      var box=document.getElementById('pricingBox');if(box)box.innerHTML='<div class="error">'+esc(error.message)+'</div>';
    }).finally(function(){state.loading=false});
  }

  function saveRow(row, action){
    var item=state.items.filter(function(x){return x.key===row.getAttribute('data-pricing-key')})[0];
    if(!item)return;
    var reason=(document.getElementById('pricingReason').value||'').trim();
    if(reason.length<2){notify('请先填写至少 2 个字的调整原因');document.getElementById('pricingReason').focus();return}
    var input=row.querySelector('.pricing-value');
    var points=Number(input.value);
    if(action==='set'&&(!Number.isInteger(points)||points<1||points>100000)){notify('收费点数必须是 1 到 100000 的整数');input.focus();return}
    var target=action==='reset'?Number(item.default_points):points;
    if(action==='set'&&target===Number(item.points)){notify('点数没有变化');return}
    var question=(action==='reset'?'恢复默认收费':'修改收费标准')+'：\n'+item.label+'（'+item.unit+'）\n'+item.points+' 点 → '+target+' 点\n\n该修改会立即影响新受理任务的实际扣点。确认继续？';
    if(!window.confirm(question))return;
    Array.prototype.forEach.call(row.querySelectorAll('button'),function(button){button.disabled=true});
    api('/api/admin/pricing',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      key:item.key,action:action,points:points,version:Number(item.version||0),reason:reason
    })}).then(function(){
      document.getElementById('pricingReason').value='';notify('已保存 '+item.label+'：'+target+' 点，立即生效');load();
    }).catch(function(error){notify('保存失败：'+error.message);if(error.status===409)load()}).finally(function(){
      Array.prototype.forEach.call(row.querySelectorAll('button'),function(button){button.disabled=false});
    });
  }

  function install(){
    var tabs=document.getElementById('moduleTabs');
    var grid=document.querySelector('#app .grid');
    if(!tabs||!grid||document.getElementById('pricingModule'))return;
    var button=document.createElement('button');
    button.className='tab';button.type='button';button.textContent='收费标准';button.setAttribute('data-module-tab','pricing');
    tabs.appendChild(button);
    var card=document.createElement('section');
    card.id='pricingModule';card.className='card span12 module-card';card.setAttribute('data-module','pricing');card.hidden=true;
    card.innerHTML='<div class="section-head"><div><h2>功能收费标准</h2><div class="hint">这里控制后端实际扣点。动态计费项目显示的是单价；批量、时长和数量仍按业务规则乘算。</div></div><button id="pricingRefresh" type="button">刷新</button></div>'+
      '<div class="ops-note" style="margin-bottom:12px">安全规则：只允许 1–100000 的正整数；每次修改都要求原因并写入审计；并发修改会拒绝旧页面覆盖新值。已受理任务保留提交时费用，新任务立即使用新价格。</div>'+
      '<div class="toolbar"><input class="field" id="pricingReason" maxlength="300" autocomplete="off" placeholder="本次调整原因（必填，至少 2 字）" style="min-width:360px"><span class="muted" id="pricingMessage"></span></div>'+
      '<div id="pricingBox"><div class="empty">点“收费标准”加载</div></div><h2 style="margin-top:22px">最近调整记录</h2><div id="pricingAudit"></div>';
    grid.appendChild(card);
    button.addEventListener('click',function(){
      Array.prototype.forEach.call(document.querySelectorAll('[data-module-tab]'),function(node){node.classList.toggle('active',node===button)});
      Array.prototype.forEach.call(document.querySelectorAll('[data-module]'),function(node){node.hidden=node!==card});
      load();
    });
    card.addEventListener('click',function(event){
      var row=event.target.closest('[data-pricing-key]');if(!row)return;
      if(event.target.closest('[data-pricing-save]'))saveRow(row,'set');
      if(event.target.closest('[data-pricing-reset]'))saveRow(row,'reset');
    });
    document.getElementById('pricingRefresh').addEventListener('click',load);
  }

  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',install);else install();
})();
