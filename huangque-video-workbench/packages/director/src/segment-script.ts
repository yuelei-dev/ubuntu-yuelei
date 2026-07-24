export const segmentScript = (script: string): string[] =>
  script
    .split(/(?<=[。！？])/u)
    .map((segment) => segment.trim())
    .filter(Boolean);
