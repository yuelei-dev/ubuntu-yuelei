import {AUDIO_NORMALIZATION_FILTER} from './normalize-video.js';
import {runMediaCommand} from './command.js';
import {withOwnedTempOutput} from './paths.js';

export const normalizeAudio = async (input: string, output: string): Promise<void> => {
  await withOwnedTempOutput(input, output, async (temporaryOutput) => {
    await runMediaCommand('ffmpeg', [
      '-nostdin', '-n', '-v', 'error', '-i', input,
      '-vn', '-af', AUDIO_NORMALIZATION_FILTER,
      '-c:a', 'aac', '-ar', '48000',
      temporaryOutput
    ]);
  });
};
