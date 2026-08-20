FROM python:3.12-bookworm

ARG CODEROOK_SOURCE_COMMIT=unknown

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        cargo \
        cmake \
        default-jdk \
        git \
        golang \
        gradle \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv

WORKDIR /opt/coderook
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen

RUN useradd --create-home --uid 10001 benchmark \
    && mkdir -p /results \
    && chown -R benchmark:benchmark /opt/coderook /results

USER benchmark
ENV CODEROOK_BENCHMARK_CONTAINER=1
ENV CODEROOK_SOURCE_COMMIT=${CODEROOK_SOURCE_COMMIT}
ENV PATH="/opt/coderook/.venv/bin:${PATH}"

VOLUME ["/datasets", "/results", "/home/benchmark/.coderook"]
CMD ["python", "scripts/run_polyglot_benchmark.py", "--help"]
