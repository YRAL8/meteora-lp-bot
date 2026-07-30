# Stage 1: собрать TS-прослойку (exec.ts/cli.ts -> ts/dist/*.js).
FROM node:20-slim AS ts-build
WORKDIR /app/ts
COPY ts/package.json ts/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ts/tsconfig.json ./
COPY ts/src ./src
RUN npx tsc -p tsconfig.json

# Stage 2: рантайм — Python (мозги/Telegram) + node runtime только для готового JS.
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY --from=ts-build /app/ts/dist ./ts/dist
COPY --from=ts-build /app/ts/node_modules ./ts/node_modules
COPY ts/package.json ./ts/package.json

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/state \
    && chown -R appuser:appuser /app

USER appuser

# Do NOT declare VOLUME /app/state here: that creates an anonymous volume when
# -v is forgotten, which looks "persistent" until the container is recreated.
# Mount explicitly: -v <named-or-host-path>:/app/state

# .env читается через python-dotenv из рабочей директории; WALLET_KEYPAIR_PATH
# монтируется отдельным volume (см. docker run/compose) — секрет не входит в образ.
CMD ["python", "-u", "main.py"]
