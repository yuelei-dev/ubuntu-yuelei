import {expect, it} from 'vitest';
import {MockAvatarProvider, assertAllowedProvenance} from '@huangque/providers';

it('exports the provider API from the package root', () => {
  expect(new MockAvatarProvider()).toBeDefined();
  expect(assertAllowedProvenance).toBeTypeOf('function');
});
