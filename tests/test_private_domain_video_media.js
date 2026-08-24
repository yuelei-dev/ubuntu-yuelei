const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../site/workbench/private-domain-video.html'), 'utf8');
const start = html.indexOf('function assetUrl(item)');
const end = html.indexOf('function protectFirstFrame(video,autoplay)');
assert.ok(start >= 0 && end > start, 'streaming helpers must be present');

const context = {
  URL,
  location: {
    href: 'https://example.test/workbench/private-domain-video.html',
    origin: 'https://example.test',
  },
  mediaObserver: null,
  window: {},
  $: () => ({}),
};
vm.createContext(context);
vm.runInContext(html.slice(start, end), context);

const asset = { id: 17, video_url: '/api/gen/file/private.mp4' };
assert.equal(
  context.streamUrl(asset),
  'https://example.test/api/gen/file/private.mp4',
  'same-origin protected media must stream directly so the browser can use Range requests',
);
assert.equal(
  context.streamUrl({ video_url: 'https://other.example/private.mp4' }),
  'https://other.example/private.mp4',
  'server-issued HTTPS signed preview URLs must remain directly streamable',
);
assert.equal(context.streamUrl({ video_url: 'http://other.example/private.mp4' }), '',
  'insecure cross-origin media URLs must be rejected');
assert.ok(!html.includes('response.blob()'), 'the page must never download complete videos into Blob objects');
assert.ok(!html.includes('URL.createObjectURL'), 'the page must not allocate media Blob URLs');

const poolBody = html.match(/function visibleAssetPool\(\)\{([\s\S]*?)\}\nfunction renderAssets/);
assert.ok(poolBody, 'visible candidate pool must remain inspectable');
const poolContext = { allAssets: Array.from({ length: 30 }, (_, index) => ({ id: index + 1 })) };
vm.createContext(poolContext);
vm.runInContext(`function visibleAssetPool(){${poolBody[1]}}`, poolContext);
assert.deepEqual(
  Array.from(poolContext.visibleAssetPool(), item => item.id),
  Array.from({ length: 12 }, (_, index) => index + 1),
  'large libraries must cap the rendered and random candidate pool at twelve visible assets',
);

const randomBody = html.match(/function chooseRandom\(\)\{([\s\S]*?)\}\nfunction loadAssets/);
assert.ok(randomBody, 'random selection function should remain inspectable');
assert.ok(randomBody[1].includes('visibleAssetPool()'), 'random selection must only use visible candidates');
assert.ok(!randomBody[1].includes('renderAssets()'), 'random selection must not rebuild media nodes');
assert.ok(html.includes('new IntersectionObserver'), 'material streams must be attached lazily');
assert.ok(html.includes("video.setAttribute('data-src',url)"), 'initial material cards must not receive eager src attributes');
assert.ok(html.includes('function toggleAsset(item)'), 'selection changes must use the incremental path');

console.log('PASS private-domain Range streaming, lazy media, and visible random pool');
