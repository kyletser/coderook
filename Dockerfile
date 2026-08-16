# CodeRook 守护进程容器镜像：python:3.12-slim + uv 按锁文件安装
FROM python:3.12-slim

# 复制 uv 二进制，按 uv.lock 精确复现依赖环境
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 先复制依赖清单以利用 Docker 构建缓存
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# 复制项目源码并安装本包
COPY src ./src
RUN uv sync --frozen --no-dev

# 用户级状态目录（会话、runtime.db 等）建议挂载持久卷
VOLUME ["/root/.coderook"]

# 7437 = IPC JSON-RPC（CLI/TUI）；7438 = 持久运行时 HTTP/SSE API
EXPOSE 7437 7438

# 默认前台启动守护进程；同一镜像内可用 `uv run coderook <子命令>` 调用 CLI
ENTRYPOINT ["uv", "run", "coderook-core"]
