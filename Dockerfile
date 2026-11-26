# syntax=docker/dockerfile:1
# Multi-stage image for the knowledge-graph library, CLI and MCP server.
#   docker build -t repo-kg .
#   docker build --build-arg EXTRAS=cpp,observability -t repo-kg:light .   # without torch
ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS builder
ARG EXTRAS=st,cpp,weaviate,observability
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md ./
COPY repo_knowledge_graph ./repo_knowledge_graph
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir --extra-index-url "${TORCH_INDEX_URL}" ".[${EXTRAS}]"

FROM python:${PYTHON_VERSION}-slim
# git is needed to clone repositories (--repo-url) and for git history tools
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown app:app /data
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/data/hf-cache \
    LANCEDB_PATH=/data/index
WORKDIR /app
COPY --chown=app:app . /app
USER app
EXPOSE 4000
ENTRYPOINT ["python", "-m", "repo_knowledge_graph"]
CMD ["serve", "--transport", "http", "--port", "4000"]
