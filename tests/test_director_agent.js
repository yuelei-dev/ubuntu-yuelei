const assert = require('assert');
const agent = require('../site/workbench/script-agent.js');

function node(value, options) {
  options = options || {};
  return {
    value: value || '', textContent: options.textContent || value || '', hidden: false,
    style: { display: options.display || '' }, disabled: !!options.disabled,
    attributes: options.attributes || {}, clicked: false, focused: false,
    classList: { add() {}, remove() {}, toggle() {} },
    getAttribute(name) { return this.attributes[name] || null; },
    click() { this.clicked = true; }, focus() { this.focused = true; },
    scrollIntoView() {}, dispatchEvent() {},
  };
}

function fixture() {
  const nodes = {
    panelBreakdown: node('', { display: 'none' }), scTopic: node('夏日护肤'),
    scSell: node('清爽不黏腻'), scMeta: node('meta'), bdAnalysis: node('', { display: 'none' }),
    bdUrl: node(''), scGen: node(''), bdGen: node(''), scGenVideo: node(''),
    scGenAudio: node(''), scExport: node(''),
  };
  const options = {
    '#segStyle .on': node('口播'), '#segDur .on': node('30s'), '#platRow .on': node('抖音'),
  };
  const lists = {
    '#scScenes .sc-card': [node('scene1'), node('scene2'), node('scene3')],
    '#segStyle .sc-opt': [options['#segStyle .on'], node('剧情'), node('种草')],
    '#segDur .sc-opt': [node('15s'), options['#segDur .on'], node('60s')],
    '#platRow .sc-chip': [options['#platRow .on'], node('小红书'), node('视频号')],
    '#scModeTabs [data-mode]': [node('AI写脚本', { attributes: {'data-mode': 'write'} }), node('拆解视频', { attributes: {'data-mode': 'breakdown'} })],
  };
  const doc = {
    defaultView: { Event: function Event() {} },
    getElementById(id) { const value = nodes[id] || null; if(value) value.ownerDocument = doc; return value; },
    querySelector(selector) { return options[selector] || null; },
    querySelectorAll(selector) { return lists[selector] || []; },
  };
  return { doc, nodes, lists };
}

{
  const { doc } = fixture();
  const context = agent.createPageContext(doc);
  assert.equal(context.page, 'script');
  assert.equal(context.mode, 'write');
  assert.equal(context.topic, '夏日护肤');
  assert.equal(context.scene_count, 3);
  assert.equal(context.has_script, true);
  assert.match(agent.createPageSnapshot(doc).page_revision, /^[a-f0-9]{8}$/);
}

{
  const { doc, nodes, lists } = fixture();
  agent.applyAction({type:'fill_field',field:'selling_points',value:'三秒吸收',label:'填入卖点'}, doc, {});
  assert.equal(nodes.scSell.value, '三秒吸收');
  agent.applyAction({type:'choose_option',field:'style',value:'剧情',label:'选剧情'}, doc, {});
  assert.equal(lists['#segStyle .sc-opt'][1].clicked, true);
  agent.applyAction({type:'focus',target:'generate_video',label:'看视频按钮'}, doc, {});
  assert.equal(nodes.scGenVideo.focused, true);
  const win = { location: { href: '' } };
  agent.applyAction({type:'navigate',target:'assets',label:'去素材库'}, doc, win);
  assert.equal(win.location.href, '/workbench/assets.html');
  assert.throws(() => agent.applyAction({type:'navigate',target:'https://evil.example',label:'外链'}, doc, win), /站内目标无效/);
}

{
  const { doc } = fixture();
  const snapshot = agent.createPageSnapshot(doc);
  assert.equal(agent.validatePlan({page_revision:snapshot.page_revision,actions:[]}, doc), true);
  assert.throws(() => agent.validatePlan({page_revision:'deadbeef',actions:[]}, doc), /页面内容已变化/);
}

const source = require('fs').readFileSync(require('path').join(__dirname, '../site/workbench/script-agent.js'), 'utf8');
assert.ok(source.includes('currentPlan.actions.map(function(action){return applyAction(action,doc,win);}'));
assert.ok(source.includes('涉及扣点或生成时，仍需要你点击原页面按钮确认'));

console.log('director agent frontend tests passed');
