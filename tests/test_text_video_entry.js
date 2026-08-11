const assert = require('assert');
const entry = require('../site/workbench/text-video-entry.js');

assert.strictEqual(entry.modeFromSearch('?mode=script_to_video'), 'script_to_video');
assert.strictEqual(entry.modeFromSearch('?mode=write'), 'write');
assert.strictEqual(entry.modeFromSearch(''), 'write');
assert.strictEqual(entry.keepModeAfterWrite('script_to_video'), 'script_to_video');
assert.strictEqual(entry.keepModeAfterWrite('breakdown'), 'write');

const target = new URL(entry.canonicalTarget('?draft=42&mode=legacy&draft=43', '#scene-2'), 'https://example.test');
assert.strictEqual(target.pathname, '/workbench/script');
assert.strictEqual(target.searchParams.get('mode'), 'script_to_video');
assert.deepStrictEqual(target.searchParams.getAll('draft'), ['42', '43']);
assert.strictEqual(target.hash, '#scene-2');

// A refresh runs the same URL-derived mode selection again.
assert.strictEqual(entry.modeFromSearch(target.search), 'script_to_video');

console.log('text-video entry behavior: 10 assertions passed');
