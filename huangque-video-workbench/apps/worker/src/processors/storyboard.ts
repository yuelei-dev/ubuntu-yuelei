import type {Storyboard} from '@huangque/contracts';

export const buildProjectStoryboard = (
  buildStoryboard: (input: {title: string; script: string}) => Storyboard,
  project: {title: string; script: string}
): Storyboard => buildStoryboard({title: project.title, script: project.script});
