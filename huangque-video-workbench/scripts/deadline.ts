export class FixtureTimeoutError extends Error {
  readonly operation: string;
  readonly timeoutMs: number;

  constructor(operation: string, timeoutMs: number, options?: ErrorOptions) {
    super(`${operation} exceeded its ${timeoutMs}ms deadline`, options);
    this.name = 'FixtureTimeoutError';
    this.operation = operation;
    this.timeoutMs = timeoutMs;
  }
}

export const withDeadline = async <T>(
  operation: string,
  timeoutMs: number,
  work: (signal: AbortSignal) => Promise<T>,
  cleanup: () => Promise<void> = async () => undefined
): Promise<T> => {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new RangeError(`deadline for ${operation} must be a positive number`);
  }

  const controller = new AbortController();
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      const error = new FixtureTimeoutError(operation, timeoutMs);
      controller.abort(error);
      try {
        void cleanup().catch((cleanupError) => {
          process.stderr.write(`${operation} cleanup failed after timeout: ${cleanupError instanceof Error ? cleanupError.message : String(cleanupError)}\n`);
        });
      } catch (cleanupError) {
        process.stderr.write(`${operation} cleanup failed after timeout: ${cleanupError instanceof Error ? cleanupError.message : String(cleanupError)}\n`);
      }
      reject(error);
    }, timeoutMs);
  });

  try {
    return await Promise.race([Promise.resolve().then(() => work(controller.signal)), timeout]);
  } finally {
    if (timer) clearTimeout(timer);
  }
};
