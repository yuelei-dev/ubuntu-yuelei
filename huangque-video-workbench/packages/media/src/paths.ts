import {access, mkdir, rename, rm} from 'node:fs/promises';
import {dirname, extname, resolve} from 'node:path';
import {randomUUID} from 'node:crypto';

export const validateMediaPath = (path: string): string => {
  if (!path || path.includes('\0')) throw new Error('invalid media path');
  return resolve(path);
};

export const assertReadableMediaPath = async (path: string): Promise<string> => {
  const resolved = validateMediaPath(path);
  await access(resolved);
  return resolved;
};

export const withOwnedTempOutput = async (input: string, output: string, write: (temporaryOutput: string) => Promise<void>): Promise<void> => {
  const source = await assertReadableMediaPath(input);
  const destination = validateMediaPath(output);
  if (source === destination) throw new Error('input and output paths must differ');

  await mkdir(dirname(destination), {recursive: true});
  try {
    await access(destination);
    throw new Error('output path already exists');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }

  const extension = extname(destination) || '.media';
  const temporaryOutput = `${destination}.huangque-${randomUUID()}${extension}`;
  try {
    await write(temporaryOutput);
    await rename(temporaryOutput, destination);
  } finally {
    await rm(temporaryOutput, {force: true});
  }
};
