import {useLayoutEffect, useRef, useState} from 'react';
import {continueRender, delayRender, useCurrentFrame} from 'remotion';
import type {RendererScene, VerticalKnowledgeVideoProps} from '../packages/renderer/src/types.js';
import type {TimelineScene} from '@huangque/timeline';

const width = 1080;
const height = 1920;

const loadImage = (source: string): Promise<HTMLImageElement> => new Promise((resolve, reject) => {
  const image = new Image();
  image.onload = () => resolve(image);
  image.onerror = () => reject(new Error('fixture canvas could not decode a generated scene asset'));
  image.src = source;
});

const drawCover = (context: CanvasRenderingContext2D, image: HTMLImageElement): void => {
  const scale = Math.max(width / image.naturalWidth, height / image.naturalHeight);
  const drawnWidth = image.naturalWidth * scale;
  const drawnHeight = image.naturalHeight * scale;
  context.drawImage(image, (width - drawnWidth) / 2, (height - drawnHeight) / 2, drawnWidth, drawnHeight);
};

const wrappedLines = (context: CanvasRenderingContext2D, text: string, maximumWidth: number): string[] => {
  const words = text.trim().split(/\s+/u).filter(Boolean);
  const lines: string[] = [];
  let line = '';
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (line && context.measureText(candidate).width > maximumWidth) {
      lines.push(line);
      line = word;
    } else {
      line = candidate;
    }
  }
  if (line) lines.push(line);
  return lines.slice(0, 4);
};

const activeScene = (props: VerticalKnowledgeVideoProps, frame: number): {
  scene: RendererScene;
  timeline: TimelineScene;
} | undefined => {
  const timeline = props.timeline.scenes.find((candidate) => frame >= candidate.startFrame && frame < candidate.endFrame);
  const scene = timeline && props.scenes.find((candidate) => candidate.id === timeline.id);
  return scene && timeline ? {scene, timeline} : undefined;
};

const drawFrame = (
  context: CanvasRenderingContext2D,
  props: VerticalKnowledgeVideoProps,
  images: Map<string, HTMLImageElement>,
  frame: number
): void => {
  const active = activeScene(props, frame);
  context.fillStyle = '#07111f';
  context.fillRect(0, 0, width, height);
  if (!active) return;

  const image = images.get(active.scene.id);
  if (image) drawCover(context, image);
  const gradient = context.createLinearGradient(0, 0, 0, height);
  gradient.addColorStop(0, 'rgba(3, 10, 20, 0.78)');
  gradient.addColorStop(0.28, 'rgba(3, 10, 20, 0.10)');
  gradient.addColorStop(0.68, 'rgba(3, 10, 20, 0.08)');
  gradient.addColorStop(1, 'rgba(3, 10, 20, 0.84)');
  context.fillStyle = gradient;
  context.fillRect(0, 0, width, height);

  context.fillStyle = active.scene.layout.startsWith('avatar_') ? '#38bdf8' : '#fb923c';
  context.fillRect(72, 72, 12, 220);
  context.font = '700 28px "Microsoft YaHei", Arial, sans-serif';
  context.fillText(active.scene.layout.startsWith('avatar_') ? 'DIGITAL PRESENTER' : 'PRODUCT B-ROLL', 112, 106);
  context.font = '800 76px "Microsoft YaHei", Arial, sans-serif';
  context.fillStyle = '#ffffff';
  wrappedLines(context, active.scene.headline ?? active.scene.script ?? active.scene.id, 880)
    .forEach((line, index) => context.fillText(line, 112, 190 + index * 88));

  const relativeMs = (frame - active.timeline.startFrame) / props.timeline.fps * 1000;
  const words = active.timeline.words ?? [];
  const current = words.findIndex((word) => relativeMs >= word.startMs && relativeMs < word.endMs);
  context.fillStyle = 'rgba(3, 10, 20, 0.82)';
  context.fillRect(64, 1320, 952, 340);
  context.font = '700 54px "Microsoft YaHei", Arial, sans-serif';
  let x = 104;
  let y = 1420;
  words.forEach((word, index) => {
    const text = word.text ?? word.word ?? '';
    const measured = context.measureText(`${text} `).width;
    if (x + measured > 968) {
      x = 104;
      y += 76;
    }
    if (index === current) {
      context.fillStyle = '#facc15';
      context.fillRect(x - 8, y - 56, measured + 8, 68);
      context.fillStyle = '#07111f';
    } else {
      context.fillStyle = '#ffffff';
    }
    context.fillText(text, x, y);
    x += measured;
  });
  context.font = '600 28px Arial, sans-serif';
  context.fillStyle = '#cbd5e1';
  context.fillText(`SCENE ${props.timeline.scenes.indexOf(active.timeline) + 1} / ${props.timeline.scenes.length}`, 104, 1616);
};

export const FixtureCanvasVideo = (props: VerticalKnowledgeVideoProps) => {
  const frame = useCurrentFrame();
  const canvas = useRef<HTMLCanvasElement>(null);
  const [images, setImages] = useState<Map<string, HTMLImageElement>>();
  const [handle] = useState(() => delayRender('Loading generated fixture scene assets'));

  useLayoutEffect(() => {
    let active = true;
    void Promise.all(props.scenes.map(async (scene) => {
      if (!scene.assetUri) return undefined;
      return [scene.id, await loadImage(scene.assetUri)] as const;
    })).then((loaded) => {
      if (!active) return;
      setImages(new Map(loaded.filter((entry): entry is readonly [string, HTMLImageElement] => entry !== undefined)));
      continueRender(handle);
    });
    return () => { active = false; };
  }, [handle, props.scenes]);

  useLayoutEffect(() => {
    const context = canvas.current?.getContext('2d');
    if (context && images) drawFrame(context, props, images, frame);
  }, [frame, images, props]);

  return <canvas ref={canvas} width={width} height={height} style={{display: 'block', height: '100%', width: '100%'}}/>;
};
