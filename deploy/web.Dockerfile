# Builds the SPA and serves it (plus the API proxy) with Caddy.
FROM node:22-alpine AS build
WORKDIR /app
ENV NPM_CONFIG_UPDATE_NOTIFIER=false
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# release notes, at the same relative path as in the repo (frontend/src/lib/changelog.ts)
COPY CHANGELOG.md /CHANGELOG.md
RUN npx tsc -b --noEmit && npx vite build

FROM caddy:2.10.2-alpine
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv/web
