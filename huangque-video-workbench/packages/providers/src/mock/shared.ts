import {createHash} from 'node:crypto';
import {copyFile, mkdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {isAbsolute, join, relative, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const canonicalize = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalize(record[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
};

export const hashInput = (input: unknown): string => createHash('sha256').update(canonicalize(input)).digest('hex');

const fixtures = {
  avatar: 'avatar-source.mp4',
  image: 'product.jpg'
} as const;

type Fixture = keyof typeof fixtures;

type CopyFixtureInput = {
  projectId: string;
  fixture: Fixture;
  inputHash: string;
};

const projectIdPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const inputHashPattern = /^[a-f0-9]{64}$/;

const assertContained = (root: string, candidate: string): void => {
  const relativePath = relative(root, candidate);
  if (relativePath === '..' || relativePath.startsWith(`..\\`) || relativePath.startsWith('../') || isAbsolute(relativePath)) {
    throw new Error('fixture path escapes its root');
  }
};

export const copyFixture = async ({projectId, fixture, inputHash}: CopyFixtureInput): Promise<string> => {
  if (!projectIdPattern.test(projectId)) throw new Error('invalid project identifier');
  if (!inputHashPattern.test(inputHash)) throw new Error('invalid input hash');
  if (!Object.hasOwn(fixtures, fixture)) throw new Error('unknown fixture');

  const fixtureRoot = resolve(process.cwd(), 'tests', 'fixtures');
  const fixtureName = fixtures[fixture];
  const fixturePath = resolve(fixtureRoot, fixtureName);
  const providersRoot = resolve(tmpdir(), 'huangque-video-workbench', 'providers');
  const outputDirectory = resolve(providersRoot, projectId, inputHash);
  const outputPath = resolve(outputDirectory, fixtureName);

  assertContained(fixtureRoot, fixturePath);
  assertContained(providersRoot, outputDirectory);
  assertContained(outputDirectory, outputPath);

  await mkdir(outputDirectory, {recursive: true});
  await copyFile(fixturePath, outputPath);

  return pathToFileURL(outputPath).href;
};
