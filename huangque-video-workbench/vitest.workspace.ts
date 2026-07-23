import {defineWorkspace} from 'vitest/config';

export default defineWorkspace([
  {
    test: {
      include: ['packages/**/*.test.ts', 'packages/**/*.test.tsx', 'apps/**/*.test.ts', 'apps/**/*.test.tsx']
    }
  }
]);
