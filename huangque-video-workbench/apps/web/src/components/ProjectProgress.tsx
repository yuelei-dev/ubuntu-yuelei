const progressByStatus: Record<string, number> = {
  CREATED: 0, STORYBOARDING: 10, GENERATING_ASSETS: 30, GENERATING_AVATAR: 45,
  ALIGNING_TIMELINE: 65, RENDERING: 80, QUALITY_CHECK: 90, COMPLETED: 100
};

export const ProjectProgress = ({status}: {status: string}) => <section aria-labelledby="project-progress-heading">
  <h2 id="project-progress-heading">Project progress</h2>
  <label htmlFor="project-progress">Current status: <output aria-live="polite">{status}</output></label>
  <progress id="project-progress" max={100} value={progressByStatus[status] ?? 0}>{progressByStatus[status] ?? 0}%</progress>
</section>;
