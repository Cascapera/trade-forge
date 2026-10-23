# The frontend, built once and served as static files by nginx — `compose up` runs the app as it
# would be deployed, not a dev server. Context is the repo root: the web app imports the shared
# `@tradeforge/schema` package (the generated DSL types) from the npm workspace.
#
# Two stages: Node builds the bundle, nginx serves it and proxies /api to the api service, exactly
# as the vite dev proxy does for local development.

FROM node:22-bookworm-slim AS build

WORKDIR /app

# Manifests first, so `npm ci` is cached until a dependency actually changes. The workspace has two
# Node members (apps/web, packages/schema); npm needs every member's manifest to install.
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/
COPY packages/schema/package.json packages/schema/
RUN npm ci

# Then the sources the build reads: the web app and the schema package it imports.
COPY apps/web/ apps/web/
COPY packages/schema/ packages/schema/
RUN npm run build -w @tradeforge/web

FROM nginx:1.27-alpine AS runtime

COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/apps/web/dist /usr/share/nginx/html
EXPOSE 80
