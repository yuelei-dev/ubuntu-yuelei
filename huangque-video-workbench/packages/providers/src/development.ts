/**
 * Deterministic providers are intentionally isolated from the production
 * package entry point. Import this subpath only from explicit test/development
 * compositions.
 */
export * from './mock/avatar.js';
export * from './mock/image.js';
export * from './mock/video.js';
