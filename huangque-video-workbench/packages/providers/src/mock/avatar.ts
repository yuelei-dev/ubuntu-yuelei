import type {AvatarProvider, AvatarRequest, GeneratedAsset} from '../types.js';
import {copyFixture, hashInput} from './shared.js';

export class MockAvatarProvider implements AvatarProvider {
  async generate(request: AvatarRequest): Promise<GeneratedAsset> {
    const inputHash = hashInput(request);
    return {
      uri: await copyFixture({projectId: request.projectId, fixture: 'avatar', inputHash}),
      width: request.width,
      height: request.height,
      provenance: 'generated',
      inputHash
    };
  }
}
