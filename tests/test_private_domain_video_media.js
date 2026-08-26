const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../site/workbench/private-domain-video.html'), 'utf8');
const start = html.indexOf('function assetUrl(item)');
const end = html.indexOf('function toggleAsset(item)');
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

const imagePoolBody = html.match(/function imagePool\(\)\{([\s\S]*?)\}\nfunction videoPool/);
const videoPoolBody = html.match(/function videoPool\(\)\{([\s\S]*?)\}\nfunction visibleAssetPool/);
const poolBody = html.match(/function visibleAssetPool\(\)\{([\s\S]*?)\}\nfunction previewBundle/);
assert.ok(poolBody, 'visible candidate pool must remain inspectable');
const poolContext = { allAssets: [
  ...Array.from({ length: 10 }, (_, index) => ({ id: index + 1, media_type: 'image' })),
  ...Array.from({ length: 10 }, (_, index) => ({ id: index + 11, media_type: 'video' })),
] };
vm.createContext(poolContext);
vm.runInContext(`function imagePool(){${imagePoolBody[1]}} function videoPool(){${videoPoolBody[1]}} function visibleAssetPool(){${poolBody[1]}}`, poolContext);
assert.deepEqual(
  Array.from(poolContext.visibleAssetPool(), item => item.id),
  [1, 2, 3, 4, 5, 6, 11, 12, 13, 14, 15, 16],
  'candidate pool must show six images and six videos without attaching the full library',
);

const randomBundleBody = html.match(/function previewBundle\(\)\{([\s\S]*?)\}\nfunction renderAssets/);
assert.ok(randomBundleBody, 'random preview bundle should remain inspectable');
assert.ok(randomBundleBody[1].includes('slice(0,2)'), 'preview bundle needs two still images');
assert.ok(randomBundleBody[1].includes('slice(0,1)'), 'preview bundle needs one video');
assert.ok(html.includes('new IntersectionObserver'), 'material streams must be attached lazily');
assert.ok(html.includes("video.setAttribute('data-src',url)"), 'initial material cards must not receive eager src attributes');
assert.ok(html.includes('function toggleAsset(item)'), 'selection changes must use the incremental path');
assert.ok(html.includes('<img id="previewImage"'), 'frame zero preview must be an immediately decodable still image');

console.log('PASS private-domain Range streaming, lazy media, and visible random pool');
