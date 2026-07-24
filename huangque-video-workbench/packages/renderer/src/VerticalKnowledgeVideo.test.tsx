// @vitest-environment jsdom
import {render} from '@testing-library/react';
import {describe, expect, it, vi} from 'vitest';
import {VerticalKnowledgeVideo} from './VerticalKnowledgeVideo';
import {sceneSequences} from './scene-sequences';
import {calculateVerticalKnowledgeMetadata} from './metadata';
import {captionPresentation} from './caption-presentation';
import {assertKnowledgeLayout, knowledgeSafeArea} from './layout';

vi.mock('remotion', () => ({
  AbsoluteFill: ({children, ...props}: any) => <div {...props}>{children}</div>,
  Img: (props: any) => <img {...props}/>,
  Sequence: ({children, from, durationInFrames}: any) => <div
    data-testid="remotion-sequence"
    data-remotion-sequence="true"
    data-from={from}
    data-duration-in-frames={durationInFrames}
  >{children}</div>,
  Video: (props: any) => <video {...props}/>,
  useCurrentFrame: () => 0
}));

describe('VerticalKnowledgeVideo', () => {
  const timeline = {
    fps: 30,
    scenes: [
      {id: 'intro', startFrame: 0, durationInFrames: 60, endFrame: 60},
      {id: 'detail', startFrame: 60, durationInFrames: 90, endFrame: 150}
    ]
  };

  it('maps every timeline scene to exactly one sequence', () => {
    const sequences = sceneSequences(timeline);

    expect(sequences).toHaveLength(timeline.scenes.length);
    expect(sequences.map((sequence) => sequence.sceneId)).toEqual(['intro', 'detail']);
    expect(sequences.map((sequence) => sequence.from)).toEqual([0, 60]);
    expect(sequences.map((sequence) => sequence.durationInFrames)).toEqual([60, 90]);
  });

  it('renders every timeline scene as exactly one Remotion scene', () => {
    const tree = render(<VerticalKnowledgeVideo timeline={timeline} scenes={[
      {id: 'intro', layout: 'avatar_full', script: 'Welcome'},
      {id: 'detail', layout: 'visual_card', script: 'Details'}
    ]}/>);

    expect(tree.getAllByTestId(/^scene-/)).toHaveLength(timeline.scenes.length);
    expect(tree.getAllByTestId('remotion-sequence').map((sequence) => ({
      from: sequence.getAttribute('data-from'),
      duration: sequence.getAttribute('data-duration-in-frames')
    }))).toEqual([
      {from: '0', duration: '60'},
      {from: '60', duration: '90'}
    ]);
  });

  it('returns exact metadata duration from the final scene end', () => {
    expect(calculateVerticalKnowledgeMetadata({timeline})).toEqual({
      durationInFrames: 150,
      fps: 30,
      width: 1080,
      height: 1920
    });
  });

  it('defines title and caption regions inside the vertical safe area', () => {
    expect(knowledgeSafeArea).toMatchObject({
      left: 60,
      right: 60,
      title: {top: 140, height: 240},
      captions: {top: 1320, height: 320, maxLines: 2}
    });
  });

  it('renders title and long caption boxes within the required safe regions', () => {
    const longCaption = [
      {text: 'A very long caption line that should be constrained to two lines', startMs: 0, endMs: 1000},
      {text: 'without leaving the allowed caption area', startMs: 1000, endMs: 2000}
    ];
    const tree = render(<VerticalKnowledgeVideo timeline={{
      fps: 30,
      scenes: [{id: 'safe', startFrame: 0, durationInFrames: 60, endFrame: 60, words: longCaption}]
    }} scenes={[{id: 'safe', layout: 'avatar_title', headline: 'A safe title'}]}/>);

    const safeArea = tree.getByTestId('safe-area-safe');
    const titleRegion = tree.getByTestId('title-region-safe');
    const title = tree.getByTestId('title-safe');
    const captions = tree.getByTestId('captions');
    expect(safeArea.style.paddingLeft).toBe('60px');
    expect(safeArea.style.paddingRight).toBe('60px');
    expect(safeArea.style.zIndex).toBe('1');
    expect(titleRegion.style.top).toBe('140px');
    expect(titleRegion.style.height).toBe('240px');
    expect(titleRegion.style.overflow).toBe('hidden');
    expect(title.style.maxHeight).toBe('240px');
    expect(captions.style.left).toBe('60px');
    expect(captions.style.right).toBe('60px');
    expect(captions.style.bottom).toBe('280px');
    expect(captions.style.maxHeight).toBe('320px');
    expect(captions.style.WebkitLineClamp).toBe('2');
  });

  it('anchors visual-card and avatar title content in an overflowing two-line title region', () => {
    const longTitle = 'A deliberately long title that must stay inside the title-safe region even when the source copy exceeds two lines';
    const tree = render(<VerticalKnowledgeVideo timeline={{
      fps: 30,
      scenes: [
        {id: 'avatar-title', startFrame: 0, durationInFrames: 30, endFrame: 30},
        {id: 'visual-card-title', startFrame: 30, durationInFrames: 30, endFrame: 60}
      ]
    }} scenes={[
      {id: 'avatar-title', layout: 'avatar_title', headline: longTitle},
      {id: 'visual-card-title', layout: 'visual_card', headline: longTitle, script: 'Supporting copy'}
    ]}/>);

    for (const id of ['avatar-title', 'visual-card-title']) {
      const region = tree.getByTestId(`title-region-${id}`);
      const title = tree.getByTestId(`title-${id}`);
      expect(region.style.position).toBe('absolute');
      expect(region.style.top).toBe('140px');
      expect(region.style.height).toBe('240px');
      expect(region.style.overflow).toBe('hidden');
      expect(title.style.overflow).toBe('hidden');
      expect(title.style.WebkitLineClamp).toBe('2');
    }
  });

  it('applies safe horizontal and caption geometry to every layout', () => {
    const layouts = ['avatar_full', 'avatar_title', 'avatar_pip', 'visual_full', 'visual_card', 'comparison', 'text_fallback'] as const;
    const tree = render(<VerticalKnowledgeVideo timeline={{
      fps: 30,
      scenes: layouts.map((layout, index) => ({
        id: layout,
        startFrame: index * 30,
        durationInFrames: 30,
        endFrame: (index + 1) * 30,
        words: [{text: layout, startMs: 0, endMs: 1000}]
      }))
    }} scenes={layouts.map((layout) => ({id: layout, layout, headline: 'Heading'}))}/>);

    for (const layout of layouts) {
      const safeArea = tree.getByTestId(`safe-area-${layout}`);
      expect(safeArea.style.paddingLeft).toBe('60px');
      expect(safeArea.style.paddingRight).toBe('60px');
    }
    for (const captions of tree.getAllByTestId('captions')) {
      expect(captions.style.left).toBe('60px');
      expect(captions.style.right).toBe('60px');
      expect(captions.style.bottom).toBe('280px');
      expect(captions.style.maxHeight).toBe('320px');
    }
  });

  it('uses video for avatar MP4 assets and images for image assets', () => {
    const tree = render(<VerticalKnowledgeVideo timeline={{
      fps: 30,
      scenes: [
        {id: 'avatar', startFrame: 0, durationInFrames: 30, endFrame: 30},
        {id: 'image', startFrame: 30, durationInFrames: 30, endFrame: 60}
      ]
    }} scenes={[
      {id: 'avatar', layout: 'avatar_full', assetUri: 'tests/fixtures/avatar-source.mp4'},
      {id: 'image', layout: 'visual_full', assetUri: 'tests/fixtures/product.jpg'}
    ]}/>);

    expect(tree.getByTestId('media-avatar').tagName).toBe('VIDEO');
    expect(tree.getByTestId('media-image').tagName).toBe('IMG');
  });

  it('uses deterministic fallback media and renders the comparison asset', () => {
    const tree = render(<VerticalKnowledgeVideo timeline={{
      fps: 30,
      scenes: [
        {id: 'missing', startFrame: 0, durationInFrames: 30, endFrame: 30},
        {id: 'remote', startFrame: 30, durationInFrames: 30, endFrame: 60},
        {id: 'compare', startFrame: 60, durationInFrames: 30, endFrame: 90}
      ]
    }} scenes={[
      {id: 'missing', layout: 'visual_full'},
      {id: 'remote', layout: 'visual_full', assetUri: 'https://example.test/asset.jpg'},
      {id: 'compare', layout: 'comparison', comparisonAssetUri: 'tests/fixtures/product.jpg'}
    ]}/>);

    expect(tree.queryByTestId('media-missing')).toBeNull();
    expect(tree.getByTestId('fallback-missing').hasAttribute('hidden')).toBe(true);
    expect(tree.getByTestId('scene-missing').style.backgroundColor).toBe('rgb(37, 99, 235)');
    expect(tree.queryByTestId('media-remote')).toBeNull();
    expect(tree.getByTestId('comparison-media-compare').tagName).toBe('IMG');
  });

  it('rejects unknown layouts instead of rendering a blank scene', () => {
    expect(() => assertKnowledgeLayout('not-a-layout')).toThrow(/unsupported knowledge layout/i);
  });

  it('styles current and director-highlighted caption words independently', () => {
    const words = [
      {text: 'Build', startMs: 0, endMs: 100},
      {text: 'better', startMs: 100, endMs: 200},
      {text: 'videos', startMs: 200, endMs: 300}
    ];

    expect(captionPresentation({words, frame: 3, fps: 30, highlightWords: ['videos']})).toMatchObject({
      words: [
        {text: 'Build', color: '#FFFFFF', fontWeight: 700},
        {text: 'better', color: '#2DD4BF', fontWeight: 700},
        {text: 'videos', color: '#FFFFFF', fontWeight: 800}
      ],
      background: 'rgba(0, 0, 0, 0.52)',
      maxLines: 2
    });
  });
});
