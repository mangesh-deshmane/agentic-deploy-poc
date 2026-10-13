# Two stages: dependencies resolve once and the runtime layer stays small.
# Final image lands around 150MB, well inside what Cloud Run pulls quickly.
FROM node:20-slim AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
# npm ci when a lockfile exists (reproducible), npm install when it does not.
RUN if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi

FROM node:20-slim
WORKDIR /app

ENV NODE_ENV=production \
    PORT=8080

COPY --from=deps /app/node_modules ./node_modules
COPY package.json server.js ./
COPY public ./public

# node:20-slim ships a non-root `node` user. Running as root inside a
# container is an unnecessary privilege for a service that only serves HTTP.
USER node

EXPOSE 8080
CMD ["node", "server.js"]
