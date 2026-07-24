export type GeneratedAsset = {
  uri: string;
  durationMs?: number;
  width: number;
  height: number;
  provenance: 'uploaded' | 'enterprise' | 'licensed' | 'generated' | 'fallback';
  inputHash: string;
};

export type AvatarRequest = {
  projectId: string;
  sceneId: string;
  text: string;
  avatarId: string;
  voiceId: string;
  audioUri?: string;
  width: number;
  height: number;
  signal?: AbortSignal;
};

export type ImageRequest = {
  projectId: string;
  sceneId: string;
  prompt: string;
  width: number;
  height: number;
  signal?: AbortSignal;
};

export type VideoRequest = ImageRequest & {
  durationMs?: number;
};

export interface AvatarProvider {
  generate(request: AvatarRequest): Promise<GeneratedAsset>;
}

export interface ImageProvider {
  generate(request: ImageRequest): Promise<GeneratedAsset>;
}

export interface VideoProvider {
  generate(request: VideoRequest): Promise<GeneratedAsset>;
}
