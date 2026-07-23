import type {GeneratedAsset, VideoProvider, VideoRequest} from '../types.js';
import {copyFixture, hashInput} from './shared.js';

export class MockVideoProvider implements VideoProvider {
  async generate(request: VideoRequest): Promise<GeneratedAsset> {
    const inputHash = hashInput(request);
    return {
      uri: await copyFixture({projectId: request.projectId, fixture: 'avatar', inputHash}),
      durationMs: request.durationMs,
      width: request.width,
      height: request.height,
      provenance: 'generated',
      inputHash
    };
  }
}
