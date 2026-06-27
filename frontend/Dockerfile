FROM node:20-alpine

WORKDIR /app

COPY package.json ./
# --legacy-peer-deps: 兼容 eslint-config-next 等老包的 peer 依赖声明
RUN npm install --legacy-peer-deps

COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev"]
