const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(
  path.join(__dirname, '..', 'site', 'workbench', 'script.html'),
  'utf8'
);
const pendingStart = html.indexOf('var _pendingSubmissionMemory={}');
const pendingEnd = html.indexOf('function currentTheme()', pendingStart);
const submitStart = html.indexOf('function _submitLocalReverse(mediaType,file,btn)');
const submitEnd = html.indexOf('if(bdImagePick)', submitStart);
assert.notEqual(pendingStart, -1);
assert.notEqual(pendingEnd, -1);
assert.notEqual(submitStart, -1);
assert.notEqual(submitEnd, -1);
const source = html.slice(pendingStart, pendingEnd) + '\n' +
  html.slice(submitStart, submitEnd);

function response(status, body) {
  return {status, json: () => Promise.resolve(body)};
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

function createHarness(results) {
  const storage = new Map();
  const requests = [];
  const failures = [];
  const polls = [];
  let randomCounter = 0;
  const context = {
    Promise,
    Date: {now: () => 123456},
    Math: Object.assign(Object.create(Math), {
      random: () => (++randomCounter) / 100,
    }),
    sessionStorage: {
      getItem: (key) => storage.has(key) ? storage.get(key) : null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
    },
    fetch: (url, options) => {
      requests.push({url, options});
      const next = results.shift();
      return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
    },
    _localPointsCheck: () => Promise.resolve(),
    _videoDuration: () => Promise.resolve(1),
    _localBusy: () => {},
    _localFail: (message) => failures.push(message),
    _pollLocalReverse: (jobId) => polls.push(jobId),
    setBdPhase: () => {},
    bdProgress: {style: {}},
    bdLocalStatus: {textContent: '', style: {}},
    window: {},
  };
  vm.runInNewContext(source, context, {filename: 'script.html#local-upload'});
  return {context, storage, requests, failures, polls};
}

const file = {
  name: 'sample.jpg', size: 12, type: 'image/jpeg', lastModified: 77,
};

test('success confirms the key and a new explicit submission gets a new key', async () => {
  const harness = createHarness([
    response(200, {job_id: 41}), response(200, {job_id: 42}),
  ]);
  harness.context._submitLocalReverse('image', file, {});
  await settle();
  const firstKey = harness.requests[0].options.headers['Idempotency-Key'];
  assert.equal(harness.storage.size, 0);
  assert.deepEqual(harness.polls, [41]);

  harness.context._submitLocalReverse('image', file, {});
  await settle();
  const secondKey = harness.requests[1].options.headers['Idempotency-Key'];
  assert.notEqual(secondKey, firstKey);
  assert.equal(harness.storage.size, 0);
  assert.deepEqual(harness.polls, [41, 42]);
});

test('network failure and truncated 200 retain the exact key for retry', async () => {
  const truncated = {status: 200, json: () => Promise.reject(new SyntaxError('truncated'))};
  const harness = createHarness([
    new Error('Failed to fetch'), response(202, {code: 'idempotency_in_progress', job_id: 51}),
    truncated, response(200, {job_id: 52}),
  ]);

  harness.context._submitLocalReverse('image', file, {});
  await settle();
  const networkKey = harness.requests[0].options.headers['Idempotency-Key'];
  assert.equal(harness.storage.size, 1);
  harness.context._submitLocalReverse('image', file, {});
  await settle();
  assert.equal(harness.requests[1].options.headers['Idempotency-Key'], networkKey);
  assert.equal(harness.storage.size, 1, '202 must retain the credential');

  harness.context._submitLocalReverse('image', file, {});
  await settle();
  const truncatedKey = harness.requests[2].options.headers['Idempotency-Key'];
  assert.equal(truncatedKey, networkKey);
  assert.equal(harness.storage.size, 1);
  harness.context._submitLocalReverse('image', file, {});
  await settle();
  assert.equal(harness.requests[3].options.headers['Idempotency-Key'], truncatedKey);
  assert.equal(harness.storage.size, 0);
  assert.deepEqual(harness.polls, [52]);
});

test('conflict clears only the stale key and next explicit submission uses a new key', async () => {
  const harness = createHarness([
    response(409, {code: 'idempotency_conflict', detail: 'conflict'}),
    response(200, {job_id: 61}),
  ]);
  harness.context._submitLocalReverse('image', file, {});
  await settle();
  const conflictKey = harness.requests[0].options.headers['Idempotency-Key'];
  assert.equal(harness.storage.size, 0);
  assert.match(harness.failures[0], /conflict/);

  harness.context._submitLocalReverse('image', file, {});
  await settle();
  assert.notEqual(harness.requests[1].options.headers['Idempotency-Key'], conflictKey);
  assert.deepEqual(harness.polls, [61]);
});

test('terminal client rejection clears the key while 409 in-progress retains it', async () => {
  const harness = createHarness([
    response(409, {code: 'idempotency_in_progress', job_id: 71}),
    response(400, {detail: 'invalid file'}),
  ]);
  harness.context._submitLocalReverse('image', file, {});
  await settle();
  const key = harness.requests[0].options.headers['Idempotency-Key'];
  assert.equal(harness.storage.size, 1);
  harness.context._submitLocalReverse('image', file, {});
  await settle();
  assert.equal(harness.requests[1].options.headers['Idempotency-Key'], key);
  assert.equal(harness.storage.size, 0);
});
