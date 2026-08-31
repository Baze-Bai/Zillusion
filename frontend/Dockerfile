FROM node:20-alpine

WORKDIR /app

# package-lock.json is committed, so install from it: `npm ci` reproduces the
# locked tree, while `npm install` re-resolves against the registry and can
# build a different image from the same commit — the same drift the Python side
# pins away with constraints.txt.
COPY package.json package-lock.json ./
# --legacy-peer-deps: tolerates the peer-dependency ranges declared by older
# packages (eslint-config-next among them).
RUN npm ci --legacy-peer-deps

COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev"]
