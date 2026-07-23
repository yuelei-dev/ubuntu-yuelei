import {writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {bundle} from '@remotion/bundler';
import {build as viteBuild} from 'vite';

const main = async (): Promise<void> => {
  const [repositoryRoot, runDirectory] = process.argv.slice(2);
  if (!repositoryRoot || !runDirectory) throw new Error('usage: prepare-fixture-bundles <repository-root> <run-directory>');

  const result = await viteBuild({
    configFile: false,
    root: repositoryRoot,
    logLevel: 'silent',
    build: {
      write: false,
      minify: false,
      target: 'es2022',
      rollupOptions: {input: resolve(repositoryRoot, 'scripts', 'fixture-web-entry.tsx')}
    }
  });
  if ('close' in result) throw new Error('Vite unexpectedly returned a build watcher');
  const outputs = Array.isArray(result) ? result : [result];
  const entry = outputs.flatMap((output) => output.output).find((output) => output.type === 'chunk' && output.isEntry);
  if (!entry || entry.type !== 'chunk') throw new Error('Vite did not produce a fixture web entry bundle');
  const clientBundlePath = resolve(runDirectory, 'fixture-client.js');
  await writeFile(clientBundlePath, entry.code, 'utf8');

  const serveUrl = await bundle({
    entryPoint: resolve(repositoryRoot, 'scripts', 'fixture-remotion-entry.tsx'),
    outDir: resolve(runDirectory, 'remotion-bundle'),
    rootDir: repositoryRoot,
    enableCaching: false
  });
  process.stdout.write(`${JSON.stringify({clientBundlePath, serveUrl})}\n`);
};

void main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
  process.exitCode = 1;
});
