export const knowledgeLayouts = [
  'avatar_full',
  'avatar_title',
  'avatar_pip',
  'visual_full',
  'visual_card',
  'comparison',
  'text_fallback'
] as const;

export type KnowledgeLayout = typeof knowledgeLayouts[number];

export const knowledgeSafeArea = {
  left: 60,
  right: 60,
  title: {top: 140, height: 240},
  captions: {top: 1320, height: 320, maxLines: 2}
} as const;

export function assertKnowledgeLayout(layout: string): asserts layout is KnowledgeLayout {
  if (!(knowledgeLayouts as readonly string[]).includes(layout)) {
    throw new Error(`unsupported knowledge layout: ${layout}`);
  }
}
