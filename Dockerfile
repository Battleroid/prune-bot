# 3.12 is the newest Python discord.py 2.7.1 claims in its classifiers.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so source edits do not invalidate the install layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps . \
 && useradd --uid 10001 --system --shell /usr/sbin/nologin prunebot \
 && mkdir -p /data /config \
 && chown -R prunebot /data

USER prunebot

ENV PRUNE_DB_PATH=/data/prune.db \
    PRUNE_CONFIG_PATH=/config/config.toml

# A dead bot is a bot that is not counting messages, so let the runtime restart it.
CMD ["python", "-m", "prunebot"]
