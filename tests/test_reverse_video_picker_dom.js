const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(
  path.join(__dirname, '..', 'site', 'workbench', 'script.html'),
  'utf8'
);
const start = html.indexOf('var reverseVideoPickerRequest=0;');
const end = html.indexOf('function _setGenerateBusy', start);
assert.notEqual(start, -1, 'reverse video picker source start should exist');
assert.notEqual(end, -1, 'reverse video picker source end should exist');
const pickerSource = html.slice(start, end);

class FakeClassList {
  constructor(initial) {
    this.names = new Set(String(initial || '').split(/\s+/).filter(Boolean));
  }
  toggle(name, force) {
    if (force) this.names.add(name);
    else this.names.delete(name);
  }
  contains(name) {
    return this.names.has(name);
  }
}

class FakeElement {
  constructor(tagName, id) {
    this.tagName = String(tagName || 'div').toUpperCase();
    this.id = id || '';
    this.style = {};
    this.attributes = {};
    this.children = [];
    this.classList = new FakeClassList();
    this.disabled = false;
    this.onclick = null;
    this._innerHTML = '';
    this.textContent = '';
  }
  set className(value) {
    this.classList = new FakeClassList(value);
  }
  get className() {
    return [...this.classList.names].join(' ');
  }
  set innerHTML(value) {
    this._innerHTML = String(value);
    this.children = [];
  }
  get innerHTML() {
    return this._innerHTML;
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name]
      : null;
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  querySelectorAll(selector) {
    if (selector === '[data-reverse-duration]') {
      return this.children.filter((child) => child.getAttribute('data-reverse-duration') !== null);
    }
    if (selector === '[data-reverse-avatar]') {
      return this.children.filter((child) => child.getAttribute('data-reverse-avatar') !== null);
    }
    if (selector === '[data-reverse-retry]') {
      return this.children.filter((child) => child.getAttribute('data-reverse-retry') !== null);
    }
    return [];
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return {promise, resolve, reject};
}

function jsonResponse(body) {
  return {ok: true, json: () => Promise.resolve(body)};
}

function response(items) {
  return jsonResponse({items});
}

async function settlePromises() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

function createHarness() {
  const ids = {};
  const add = (id, tag) => {
    ids[id] = new FakeElement(tag || 'div', id);
    return ids[id];
  };
  add('reverseVideoPickModal');
  const noAvatar = add('reverseVideoNoAvatar', 'button');
  noAvatar.className = 'sc-opt on';
  add('reverseVideoSeedanceStatus');
  const grid = add('reverseVideoAvatarGrid');
  const duration = add('reverseVideoDuration');
  [5, 10, 15].forEach((seconds) => {
    const button = new FakeElement('button');
    button.setAttribute('data-reverse-duration', seconds);
    duration.appendChild(button);
  });
  add('reverseVideoCost');
  add('reverseVideoConfirm', 'button');
  add('reverseVideoPickClose', 'button');

  const requests = [];
  const context = {
    Promise,
    console,
    document: {
      getElementById: (id) => ids[id] || null,
      createElement: (tagName) => new FakeElement(tagName),
    },
    fetch: (url, options) => {
      const request = deferred();
      request.url = url;
      request.options = options || {};
      requests.push(request);
      return request.promise;
    },
    tok: () => 'test-token',
    esc: (value) => String(value == null ? '' : value),
  };
  vm.runInNewContext(pickerSource, context, {filename: 'script.html#reverse-video-picker'});
  return {context, ids, requests};
}

test('confirm dismisses before firing its callback exactly once', async () => {
  const {context, ids, requests} = createHarness();
  const generationButton = {disabled: false};
  let callbackCount = 0;
  let displaySeenByCallback = '';
  context._showReverseVideoPicker('prompt', () => {
    callbackCount += 1;
    displaySeenByCallback = ids.reverseVideoPickModal.style.display;
    generationButton.disabled = true;
  });

  assert.equal(ids.reverseVideoConfirm.disabled, true, 'no-avatar submit waits for channel health');
  assert.equal(requests[1].url, '/api/gen/health');
  assert.equal(requests[1].options.cache, 'no-store');
  requests[1].resolve(jsonResponse({seedance_video_enabled: true}));
  await settlePromises();

  ids.reverseVideoConfirm.onclick();
  ids.reverseVideoConfirm.onclick();

  assert.equal(ids.reverseVideoPickModal.style.display, 'none');
  assert.equal(displaySeenByCallback, 'none', 'modal must close before generation starts');
  assert.equal(callbackCount, 1, 'double click must only submit once');
  assert.equal(ids.reverseVideoConfirm.disabled, true);
  assert.equal(generationButton.disabled, true, 'generation callback must still lock its source button');
});

test('retry starts a new avatar load without resetting no-avatar or duration', async () => {
  const {context, ids, requests} = createHarness();
  context._showReverseVideoPicker('prompt', () => {});
  requests[1].resolve(jsonResponse({seedance_video_enabled: true}));
  await settlePromises();
  const duration15 = ids.reverseVideoDuration.children[2];
  duration15.onclick();
  ids.reverseVideoNoAvatar.onclick();

  requests[0].reject(new Error('network down'));
  await settlePromises();

  const retry = ids.reverseVideoAvatarGrid.querySelector('[data-reverse-retry]');
  assert.ok(retry, 'load failure must render an explicit retry button');
  retry.onclick();

  assert.equal(requests.length, 3, 'retry must start a fresh avatar request without repeating health');
  assert.equal(requests[2].url, '/api/gen/video/avatars?limit=60');
  assert.equal(ids.reverseVideoNoAvatar.classList.contains('on'), true);
  assert.equal(ids.reverseVideoNoAvatar.getAttribute('aria-pressed'), 'true');
  assert.equal(duration15.classList.contains('on'), true, 'retry must preserve duration selection');
  assert.equal(ids.reverseVideoCost.textContent, '预计消耗 450 点');
});

test('stale response from a prior invocation cannot overwrite the current grid', async () => {
  const {context, ids, requests} = createHarness();
  context._showReverseVideoPicker('old prompt', () => {});
  context._showReverseVideoPicker('current prompt', () => {});

  requests[3].resolve(jsonResponse({seedance_video_enabled: true}));
  requests[2].resolve(response([{id: 'current', name: 'current avatar'}]));
  await settlePromises();
  requests[0].resolve(response([{id: 'stale', name: 'stale avatar'}]));
  requests[1].resolve(jsonResponse({seedance_video_enabled: false}));
  await settlePromises();

  const cards = ids.reverseVideoAvatarGrid.querySelectorAll('[data-reverse-avatar]');
  assert.deepEqual(cards.map((card) => card.getAttribute('data-reverse-avatar')), ['current']);
  assert.equal(ids.reverseVideoSeedanceStatus.getAttribute('data-state'), 'ready');
});

test('disabled Seedance blocks no-avatar submission before the paid endpoint', async () => {
  const {context, ids, requests} = createHarness();
  let callbackCount = 0;
  context._showReverseVideoPicker('prompt', () => {
    callbackCount += 1;
  });

  requests[1].resolve(jsonResponse({seedance_video_enabled: false}));
  await settlePromises();

  assert.equal(ids.reverseVideoNoAvatar.disabled, true);
  assert.equal(ids.reverseVideoNoAvatar.getAttribute('aria-pressed'), 'false');
  assert.equal(ids.reverseVideoConfirm.disabled, true);
  assert.equal(ids.reverseVideoSeedanceStatus.getAttribute('data-state'), 'blocked');
  assert.match(ids.reverseVideoSeedanceStatus.textContent, /Seedance 通道暂未开启/);
  ids.reverseVideoConfirm.onclick();
  assert.equal(callbackCount, 0, 'blocked channel must not invoke generation callback');
});

test('disabled Seedance still allows the explicit avatar cinematic path', async () => {
  const {context, ids, requests} = createHarness();
  let choice = null;
  context._showReverseVideoPicker('prompt', (value) => {
    choice = value;
  });

  requests[0].resolve(response([{id: 'avatar-7', name: 'avatar'}]));
  requests[1].resolve(jsonResponse({seedance_video_enabled: false}));
  await settlePromises();

  const card = ids.reverseVideoAvatarGrid.querySelectorAll('[data-reverse-avatar]')[0];
  card.onclick();
  assert.equal(ids.reverseVideoConfirm.disabled, false);
  assert.equal(ids.reverseVideoCost.textContent, '预计消耗 100 点');
  ids.reverseVideoConfirm.onclick();
  assert.equal(choice.avatarId, 'avatar-7');
  assert.equal(choice.duration, 10);
});

test('health lookup failure fails closed for no-avatar Seedance generation', async () => {
  const {context, ids, requests} = createHarness();
  context._showReverseVideoPicker('prompt', () => {});

  requests[1].reject(new Error('health unavailable'));
  await settlePromises();

  assert.equal(ids.reverseVideoNoAvatar.disabled, true);
  assert.equal(ids.reverseVideoConfirm.disabled, true);
  assert.equal(ids.reverseVideoSeedanceStatus.getAttribute('data-state'), 'blocked');
  assert.match(ids.reverseVideoSeedanceStatus.textContent, /无法确认 Seedance 通道状态/);
});
