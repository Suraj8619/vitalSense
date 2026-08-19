# Minimal image for the ingest server.
FROM node:22-alpine

WORKDIR /app

# Dependencies first, so a source edit does not re-download them.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY src ./src
COPY public ./public

# Never run as root: this process is reachable from the network, and there is nothing it
# needs to do that requires privilege.
USER node

EXPOSE 8080
CMD ["node", "src/index.js"]
