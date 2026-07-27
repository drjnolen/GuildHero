FROM node:22-slim AS sui-dependencies

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

FROM python:3.12-slim

WORKDIR /app

# The Telegram app remains Python; the official Sui SDK runs in a persistent
# Node child process so all chain access uses Sui's supported gRPC architecture.
COPY --from=sui-dependencies /usr/local/bin/node /usr/local/bin/node
COPY --from=sui-dependencies /app/node_modules ./node_modules

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY package.json package-lock.json ./
COPY main.py ./
COPY CityLedger/ ./CityLedger/

CMD ["python", "main.py"]
