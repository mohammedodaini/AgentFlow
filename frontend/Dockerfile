# =============================================================================
# Production image for the frontend (M16).
#
# Three stages, and the middle one is the reason: `next build` needs every
# dependency, the runtime needs almost none, and `output: "standalone"` is what
# lets the final image contain a traced subset instead of ~1.2GB of node_modules.
# =============================================================================
FROM node:26-slim AS deps

# corepack ships with node and pins pnpm from package.json's `packageManager`
# field, so the image builds with the same pnpm the lockfile was written by.
RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
# `--frozen-lockfile`: a lockfile that does not match package.json fails the
# build rather than silently resolving something else. The backend image learned
# the same lesson at M16 (see backend/Dockerfile).
#
# `--config.node-linker=hoisted` is the one that took a failed container to find.
# pnpm's default layout is a content-addressed store plus a tree of symlinks, and
# Next's `output: "standalone"` traces the files it needs and *copies* them — so
# the copy lands full of links pointing at paths that were never copied. The build
# succeeds, the image builds, and the container crash-loops with
# `MODULE_NOT_FOUND: @swc/helpers` from inside next's own require hook.
#
# Set here rather than in a committed `.npmrc`, so local development keeps pnpm's
# fast, deduplicated layout and only the image pays for a flat one.
RUN pnpm install --frozen-lockfile --config.node-linker=hoisted

FROM node:26-slim AS builder
RUN corepack enable
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Next reads `BACKEND_URL` at *runtime*, not build time — it has no
# NEXT_PUBLIC_ prefix and every read is server-side (src/lib/config.ts), so the
# image is not baked to one environment.
RUN pnpm build

FROM node:26-slim

# Never run containers as root.
RUN useradd --create-home appuser
WORKDIR /app

# The traced server, plus the two directories standalone does not copy: `static`
# is fingerprinted CSS and JS, and `public` is whatever the app serves directly.
# Omitting either produces a site that renders as unstyled HTML — which looks
# like a broken deploy and is a missing COPY.
COPY --from=builder --chown=appuser:appuser /app/.next/standalone ./
COPY --from=builder --chown=appuser:appuser /app/.next/static ./.next/static
COPY --from=builder --chown=appuser:appuser /app/public ./public

USER appuser
ENV NODE_ENV=production
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

EXPOSE 3000
CMD ["node", "server.js"]
