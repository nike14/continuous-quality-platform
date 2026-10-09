# One image for every CQP service; docker-compose picks the command.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /bin/uv

WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY policy ./policy
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
