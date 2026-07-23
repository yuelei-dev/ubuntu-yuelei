import {describe, expect, it} from 'vitest';
import {ProjectStatusSchema, StoryboardSchema} from '@huangque/contracts';

describe('@huangque/contracts', () => {
  it('exports shared contracts from the package root', () => {
    expect(StoryboardSchema).toBeDefined();
    expect(ProjectStatusSchema.parse('CREATED')).toBe('CREATED');
  });
});
