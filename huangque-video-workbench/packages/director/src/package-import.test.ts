import {expect, it} from 'vitest';
import {buildStoryboard, validateStoryboard} from '@huangque/director';

it('exports the director API from the package root', () => {
  const board = buildStoryboard({title: '测试', script: '你好。'});

  expect(validateStoryboard(board)).toEqual([]);
});
