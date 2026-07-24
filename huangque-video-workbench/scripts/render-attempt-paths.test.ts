import {describe, expect, it} from 'vitest';
import {renderAttemptObjectKeys} from './docker-worker.js';
import {fixtureAttemptDirectory} from './start-local-composition.js';

describe('render attempt artifact paths', () => {
  it('derives distinct immutable local and MinIO paths for competing owners', () => {
    expect(fixtureAttemptDirectory('runs', 'project_1', 'owner-one')).not.toBe(fixtureAttemptDirectory('runs', 'project_1', 'owner-two'));
    expect(renderAttemptObjectKeys('project_1', 'owner-one')).toEqual({
      preview: 'projects/project_1/attempts/owner-one/preview.mp4',
      report: 'projects/project_1/attempts/owner-one/quality.json'
    });
    expect(renderAttemptObjectKeys('project_1', 'owner-two')).not.toEqual(renderAttemptObjectKeys('project_1', 'owner-one'));
  });
});
