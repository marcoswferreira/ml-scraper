FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install system dependencies required by Playwright's bundled node driver (libatomic1)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && playwright install --with-deps chromium \
    && mkdir -p /app/.state /app/output

VOLUME ["/app/.state", "/app/output"]

ENTRYPOINT ["ml-automation"]
CMD ["watch", "--interval-minutes", "15", "--batch-size", "5", "--headless"]
