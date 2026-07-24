type GeneratedScene = {status: 'PENDING' | 'GENERATING' | 'READY' | 'FALLBACK_ACCEPTED' | 'NEEDS_USER_INPUT' | 'FAILED'};

/** Timeline alignment is permitted only after every required scene has a usable result. */
export const allRequiredScenesReady = (scenes: GeneratedScene[]): boolean =>
  scenes.length > 0 && scenes.every((scene) => scene.status === 'READY' || scene.status === 'FALLBACK_ACCEPTED');
