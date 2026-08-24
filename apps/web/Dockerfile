FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# No env vars needed at build time -- every page is `export const dynamic =
# "force-dynamic"`, so Next skips prerendering them and never calls
# lib/api-client.ts's requireEnv() during `next build`. Real values
# (APP_SHARED_TOKEN, API_BASE_URL) are supplied at container run time via
# docker-compose's environment:, never baked into the image.
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
# The standalone server binds to the HOSTNAME env var's value if set, which
# defaults to the container's own interface address, not 0.0.0.0 -- without
# this, the server is unreachable from outside the container (or even via
# 127.0.0.1 from inside it). Well-known Next.js standalone-Docker gotcha.
ENV HOSTNAME=0.0.0.0
ENV PORT=3000
# next.config.ts sets output: "standalone" -- this copies a minimal,
# self-contained server bundle (its own node_modules subset included),
# not the full node_modules tree from the builder stage.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
# Standalone output does NOT automatically include public/ (a well-known Next.js gotcha) --
# needed for real files (manifest.json/sw.js/icons/, Milestone 9 Layer 1's PWA push support) to
# actually be served.
COPY --from=builder /app/public ./public

EXPOSE 3000
CMD ["node", "server.js"]
