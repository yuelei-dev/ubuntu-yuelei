import type {GeneratedAsset, ImageProvider, ImageRequest} from '../types.js';
import {copyFixture, hashInput} from './shared.js';

export class MockImageProvider implements ImageProvider {
  async generate(request: ImageRequest): Promise<GeneratedAsset> {
    const inputHash = hashInput(request);
    return {
      uri: await copyFixture({projectId: request.projectId, fixture: 'image', inputHash}),
      width: request.width,
      height: request.height,
      provenance: 'generated',
      inputHash
    };
  }
}
