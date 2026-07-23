import type {Scene} from '@huangque/contracts';

const imageVideoPattern = /??|??|??|??|????/u;
const chartPattern = /??|??|??|??|??/u;

export const classifySegment = (text: string, index: number, total: number): Scene['type'] => {
  if (index === 0 || index === total - 1) return 'avatar';
  if (imageVideoPattern.test(text)) return 'image_video';
  if (chartPattern.test(text)) return 'chart';
  return 'avatar';
};
