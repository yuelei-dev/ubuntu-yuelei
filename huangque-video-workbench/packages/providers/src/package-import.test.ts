import {expect, it} from 'vitest';
import {assertAllowedProvenance} from '@huangque/providers';
import {MockAvatarProvider} from '@huangque/providers/development';

it('exports the provider API from the package root', () => {
  expect(new MockAvatarProvider()).toBeDefined();
  expect(assertAllowedProvenance).toBeTypeOf('function');
});
