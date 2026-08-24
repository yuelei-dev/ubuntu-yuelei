const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../site/workbench/private-domain-video.html'), 'utf8');
const start = html.indexOf('function assetUrl(item)');
const end = html.indexOf('function protectFirstFrame(video,autoplay)');
assert.ok(start >= 0 && end > start, 'media cache functions must be present');

let fetchCount = 0;
const created = [];
const revoked = [];
const URLCtor = URL;
URLCtor.createObjectURL = () => {
  const value = `blob:test-${created.length + 1}`;
  created.push(value);
  return value;
};
URLCtor.revokeObjectURL = value => revoked.push(value);

const context = {
  URL: URLCtor,
  location: { href: 'https://example.test/workbench/private-domain-video.html', origin: 'https://example.test' },
  mediaCache: {},
  mediaEpoch: 0,
  fetch: async () => {
    fetchCount += 1;
    return { ok: true, blob: async () => ({}) };
  },
  Promise,
};
vm.createContext(context);
vm.runInContext(html.slice(start, end), context);

(async () => {
  const asset = { id: 17, video_url: '/api/gen/file/private.mp4' };
  const first = context.playable(asset);
  const second = context.playable(asset);
  assert.equal(first, second, 'same asset must share one in-flight promise');
  assert.equal(await first, 'blob:test-1');
  assert.equal(fetchCount, 1, 'same asset must only be downloaded once');

  context.revokeObjects();
  assert.deepEqual(revoked, ['blob:test-1'], 'refresh must revoke the exact cached Blob URL');
  assert.equal(Object.keys(context.mediaCache).length, 0, 'refresh must empty the media cache');

  assert.equal(await context.playable(asset), 'blob:test-2');
  assert.equal(fetchCount, 2, 'asset may be fetched again only after an explicit cache release');
  context.revokeObjects();
  assert.deepEqual(revoked, ['blob:test-1', 'blob:test-2']);

  const randomBody = html.match(/function chooseRandom\(\)\{([\s\S]*?)\}\nfunction loadAssets/);
  assert.ok(randomBody, 'random selection function should remain inspectable');
  assert.ok(!randomBody[1].includes('renderAssets()'), 'random selection must not rebuild all media nodes');
  assert.ok(html.includes('function toggleAsset(item)'), 'selection changes must use the incremental path');
  console.log('PASS private-domain media cache and exact Blob cleanup');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
