# One container with FalkorDB and the CrewOps app, for free hosts that run a single
# container (Render). Build from the repository root:
#   docker build -f deploy/Dockerfile -t crewops-allinone .
FROM falkordb/falkordb:latest

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python 3.12
COPY src ./src
COPY data ./data
RUN uv sync --frozen --no-dev --python 3.12

COPY deploy/start.sh /usr/local/bin/start-crewops
RUN chmod +x /usr/local/bin/start-crewops

ENV FALKORDB_HOST=localhost FALKORDB_PORT=6379
EXPOSE 10000
ENTRYPOINT []
CMD ["start-crewops"]
