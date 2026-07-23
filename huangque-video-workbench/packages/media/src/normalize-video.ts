import {runMediaCommand} from './command.js';
import {withOwnedTempOutput} from './paths.js';
import {probeMedia} from './probe.js';

export const VIDEO_NORMALIZATION_FILTER = 'scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,fps=30';
export const AUDIO_NORMALIZATION_FILTER = 'loudnorm=I=-16:LRA=11:TP=-1.5';

export const normalizeVideo = async (input: string, output: string): Promise<void> => {
  const inputProbe = await probeMedia(input);
  const audioInput = inputProbe.audio
    ? []
    : ['-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000'];
  const audioMap = inputProbe.audio ? '0:a:0' : '1:a:0';
  await withOwnedTempOutput(input, output, async (temporaryOutput) => {
    await runMediaCommand('ffmpeg', [
      '-nostdin', '-n', '-v', 'error', '-i', input,
      ...audioInput,
      '-map', '0:v:0', '-map', audioMap,
      '-vf', VIDEO_NORMALIZATION_FILTER,
      '-af', AUDIO_NORMALIZATION_FILTER,
      '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', '30',
      '-c:a', 'aac', '-ar', '48000', '-movflags', '+faststart',
      '-shortest',
      temporaryOutput
    ]);
  });
};
