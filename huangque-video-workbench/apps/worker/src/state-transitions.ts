import type {ProjectStatus} from '@huangque/contracts';

const transitions: Readonly<Record<ProjectStatus, readonly ProjectStatus[]>> = {
  CREATED: ['STORYBOARDING', 'CANCELLED'],
  STORYBOARDING: ['GENERATING_ASSETS', 'NEEDS_USER_INPUT', 'FAILED', 'CANCELLED'],
  GENERATING_ASSETS: ['GENERATING_AVATAR', 'ALIGNING_TIMELINE', 'RETRYING', 'PARTIALLY_FAILED', 'NEEDS_USER_INPUT', 'FAILED', 'CANCELLED'],
  GENERATING_AVATAR: ['ALIGNING_TIMELINE', 'RETRYING', 'PARTIALLY_FAILED', 'NEEDS_USER_INPUT', 'FAILED', 'CANCELLED'],
  ALIGNING_TIMELINE: ['RENDERING', 'NEEDS_USER_INPUT', 'FAILED', 'CANCELLED'],
  RENDERING: ['QUALITY_CHECK', 'RETRYING', 'FAILED', 'CANCELLED'],
  QUALITY_CHECK: ['COMPLETED', 'RETRYING', 'FAILED', 'CANCELLED'],
  COMPLETED: [],
  RETRYING: ['GENERATING_ASSETS', 'GENERATING_AVATAR', 'RENDERING', 'FAILED', 'CANCELLED'],
  PARTIALLY_FAILED: ['GENERATING_ASSETS', 'NEEDS_USER_INPUT', 'FAILED', 'CANCELLED'],
  NEEDS_USER_INPUT: ['STORYBOARDING', 'GENERATING_ASSETS', 'CANCELLED'],
  FAILED: ['RETRYING', 'CANCELLED'],
  CANCELLED: []
};

export class IllegalProjectTransitionError extends Error {
  readonly name = 'IllegalProjectTransitionError';

  constructor(readonly from: ProjectStatus, readonly to: ProjectStatus) {
    super(`cannot transition project from ${from} to ${to}`);
  }
}

export const transitionProject = (from: ProjectStatus, to: ProjectStatus): ProjectStatus => {
  if (!transitions[from].includes(to)) throw new IllegalProjectTransitionError(from, to);
  return to;
};
