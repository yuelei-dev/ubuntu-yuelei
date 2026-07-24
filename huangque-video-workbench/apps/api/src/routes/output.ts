import type {Readable} from 'node:stream';
import type {ProjectDetail, ProjectRecord} from '../services/project-service.js';

/** Reads a private object after the caller has completed authorization. */
export type ObjectReader = {
  open(objectKey: string): Promise<Readable>;
};

export class ObjectReadTimeoutError extends Error {
  readonly name = 'ObjectReadTimeoutError';
}

/** Output keys are persisted by the worker; URLs are never persisted or public. */
export const outputObjectKey = (project: ProjectRecord): string | undefined => project.previewUrl ?? project.downloadUrl;

/** Converts persisted private object references into owner-authenticated API aliases. */
export const projectForClient = (project: ProjectDetail, publicApiBasePath = '/api'): Omit<ProjectDetail, 'qualityReportPath'> => {
  const {qualityReportPath: _qualityReportPath, previewUrl, downloadUrl, ...safeProject} = project;
  const normalizedBase = `/${publicApiBasePath.split('/').filter(Boolean).join('/')}`;
  const outputUrl = `${normalizedBase}/projects/${project.id}/output`;
  return {
    ...safeProject,
    ...(previewUrl ? {previewUrl: outputUrl} : {}),
    ...(downloadUrl ? {downloadUrl: outputUrl} : {})
  };
};
