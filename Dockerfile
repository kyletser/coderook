FROM python:3.12-slim AS builder

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim

RUN useradd --create-home --uid 10001 coderook
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

USER coderook
WORKDIR /workspace
VOLUME ["/home/coderook/.coderook", "/workspace"]
EXPOSE 7438

ENV CODEROOK_API_HOST=0.0.0.0
CMD ["coderook-core"]
