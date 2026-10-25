# Owner screen: built with Vite, served by nginx, /api proxied to the owner-view API.
# Build context: the repository root (the demo payloads in eval/demo are bundled as the fallback).
FROM node:22-slim AS build
WORKDIR /repo/dashboard/owner
COPY dashboard/owner/package.json dashboard/owner/package-lock.json ./
RUN npm ci
COPY dashboard/owner/ ./
COPY eval/demo/ /repo/eval/demo/
RUN npm run build

FROM nginx:1.27-alpine
COPY dashboard/owner/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /repo/dashboard/owner/dist /usr/share/nginx/html
EXPOSE 80
