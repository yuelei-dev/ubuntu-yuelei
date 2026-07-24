import {spawn} from 'node:child_process';

const maxOutputBytes = 2 * 1024 * 1024;

export class MediaCommandError extends Error {
  constructor(message: string, readonly executable: string, readonly arguments_: readonly string[], readonly stderr: string) {
    super(message);
    this.name = 'MediaCommandError';
  }
}

export type CommandOutput = {
  stdout: string;
  stderr: string;
};

export const runMediaCommand = async (executable: string, arguments_: readonly string[], options: {signal?: AbortSignal} = {}): Promise<CommandOutput> => new Promise((resolve, reject) => {
  options.signal?.throwIfAborted();
  const child = spawn(executable, [...arguments_], {shell: false, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']});
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  let outputSize = 0;
  let settled = false;

  const fail = (error: Error): void => {
    if (settled) return;
    settled = true;
    reject(error);
  };

  const collect = (target: Buffer[]) => (chunk: Buffer): void => {
    outputSize += chunk.length;
    if (outputSize > maxOutputBytes) {
      child.kill();
      fail(new MediaCommandError('media command produced too much output', executable, arguments_, Buffer.concat(stderr).toString('utf8')));
      return;
    }
    target.push(chunk);
  };

  child.stdout?.on('data', collect(stdout));
  child.stderr?.on('data', collect(stderr));
  options.signal?.addEventListener('abort', () => {
    child.kill();
    fail(options.signal?.reason instanceof Error ? options.signal.reason : new Error('media command aborted'));
  }, {once: true});
  child.on('error', (error) => fail(new MediaCommandError(error.message, executable, arguments_, Buffer.concat(stderr).toString('utf8'))));
  child.on('close', (code) => {
    if (settled) return;
    const output = {stdout: Buffer.concat(stdout).toString('utf8'), stderr: Buffer.concat(stderr).toString('utf8')};
    if (code === 0) {
      settled = true;
      resolve(output);
      return;
    }
    fail(new MediaCommandError(`media command failed with exit code ${code ?? 'unknown'}`, executable, arguments_, output.stderr));
  });
});
