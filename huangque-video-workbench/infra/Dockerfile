FROM node:22-bookworm-slim AS application-source
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps ./apps
COPY packages ./packages
RUN npm ci
COPY tsconfig.json tsconfig.base.json drizzle.config.ts ./
COPY scripts ./scripts
# Deterministic mocks are development/test-only and must never enter a
# production image, even though the source workspace contains them.
RUN rm -rf ./packages/providers/src/mock ./packages/providers/src/development.ts \
  ./scripts/start-local-composition.ts ./scripts/prepare-fixture-bundles.ts \
  ./scripts/FixtureCanvasVideo.tsx ./scripts/fixture-web-entry.tsx \
  ./scripts/fixture-remotion-entry.tsx ./scripts/video-visual-analysis.ts \
  ./scripts/run-fixture-project.ts \
  && find ./apps ./packages ./scripts \( -name '*.test.ts' -o -name '*.test.tsx' -o -name '*.spec.ts' -o -name '*.spec.tsx' \) -delete

FROM node:22-bookworm-slim
RUN apt-get update \
  && apt-get install -y --no-install-recommends chromium ffmpeg \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=application-source /app /app

ENV HUANGQUE_CHROME_PATH=/usr/bin/chromium
EXPOSE 4173
