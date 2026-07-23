declare module 'node:crypto' {
  export function createHash(algorithm: string): {
    update(value: string): {digest(encoding: 'hex'): string};
  };
}

declare module 'node:fs/promises' {
  export function copyFile(source: string, destination: string): Promise<void>;
  export function mkdir(path: string, options: {recursive: true}): Promise<string | undefined>;
}

declare module 'node:os' {
  export function tmpdir(): string;
}

declare module 'node:path' {
  export function isAbsolute(path: string): boolean;
  export function join(...paths: string[]): string;
  export function relative(from: string, to: string): string;
  export function resolve(...paths: string[]): string;
}

declare module 'node:url' {
  export function pathToFileURL(path: string): {href: string};
}

declare const process: {
  cwd(): string;
};
