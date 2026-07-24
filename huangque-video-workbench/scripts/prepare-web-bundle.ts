import {writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {build as viteBuild} from 'vite';

const main = async (): Promise<void> => {
  const [repositoryRoot, runDirectory] = process.argv.slice(2);
  if (!repositoryRoot || !runDirectory) throw new Error('usage: prepare-web-bundle <repository-root> <run-directory>');
  const result = await viteBuild({
    configFile: false, root: repositoryRoot, logLevel: 'silent',
    build: {
      write: false, minify: true, target: 'es2022',
      rollupOptions: {input: resolve(repositoryRoot, 'scripts', 'web-entry.tsx')}
    }
  });
  if ('close' in result) throw new Error('Vite unexpectedly returned a build watcher');
  const outputs = Array.isArray(result) ? result : [result];
  const entry = outputs.flatMap((output) => output.output).find((output) => output.type === 'chunk' && output.isEntry);
  if (!entry || entry.type !== 'chunk') throw new Error('Vite did not produce the web entry bundle');
  const clientBundlePath = resolve(runDirectory, 'workbench-client.js');
  await writeFile(clientBundlePath, entry.code, 'utf8');
  process.stdout.write(`${JSON.stringify({clientBundlePath})}\n`);
};

void main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
  process.exitCode = 1;
});
