FROM docker:29.1.3-cli AS docker-cli
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON_INSTALL_DIR=/opt/coderook/python
RUN uv python install 3.12 \
    && UV_PYTHON_PREFERENCE=only-managed uv venv --python 3.12 /opt/coderook/venv
COPY pyproject.toml uv.lock README.md LICENSE /build/
COPY src /build/src
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_HTTP_TIMEOUT=120 UV_PROJECT_ENVIRONMENT=/opt/coderook/venv \
    uv sync --project /build --frozen --no-dev --no-editable --python /opt/coderook/venv/bin/python
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_HTTP_TIMEOUT=120 uv pip install --system swebench==5.0.2 pyarrow==21.0.0
COPY scripts/swebench_container_agent.py /opt/coderook/agent.py
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /evidence
