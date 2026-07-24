import type {Scene} from '@huangque/contracts';

const imageVideoPattern = /实拍|外观|展示|画面|操作过程/u;
const chartPattern = /数据|增长|下降|比例|对比/u;

export const classifySegment = (text: string, index: number, total: number): Scene['type'] => {
  if (index === 0 || index === total - 1) return 'avatar';
  if (imageVideoPattern.test(text)) return 'image_video';
  if (chartPattern.test(text)) return 'chart';
  return 'avatar';
};
