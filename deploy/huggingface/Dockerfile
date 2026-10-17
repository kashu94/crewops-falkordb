# One container with FalkorDB and the CrewOps console, for Hugging Face Spaces (Docker SDK).
# The app code is downloaded from GitHub at build time, so this file and README.md are
# the only files the Space needs. Rebuild the Space to pick up new commits.
FROM falkordb/falkordb:latest

ARG REPO_TARBALL=https://github.com/kashu94/crewops-falkordb/archive/refs/heads/main.tar.gz

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

ADD ${REPO_TARBALL} /tmp/src.tar.gz
RUN mkdir -p /app && tar -xzf /tmp/src.tar.gz -C /app --strip-components=1 && rm /tmp/src.tar.gz \
 && cd /app && uv sync --frozen --no-dev --python 3.12 \
 && chmod -R a+rX /opt/python /app

# FalkorDB in the background (no persistence; the seed reloads on every start), then the app.
RUN printf '%s\n' \
  '#!/bin/sh' \
  'set -e' \
  'redis-server --loadmodule /var/lib/falkordb/bin/falkordb.so --dir /tmp --save "" --appendonly no --daemonize yes' \
  'until redis-cli ping >/dev/null 2>&1; do sleep 0.2; done' \
  'exec /app/.venv/bin/crewops serve --host 0.0.0.0 --port "${PORT:-7860}"' \
  > /usr/local/bin/start-crewops && chmod +x /usr/local/bin/start-crewops

# Spaces run the container as user 1000
RUN mkdir -p /home/user && chown 1000:1000 /home/user
USER 1000:1000
ENV HOME=/home/user FALKORDB_HOST=localhost FALKORDB_PORT=6379
WORKDIR /app
EXPOSE 7860
ENTRYPOINT []
CMD ["start-crewops"]
