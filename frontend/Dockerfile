FROM node:24-alpine

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY src ./src
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
