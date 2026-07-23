FROM python:3.11-slim AS base

# Prevent Python from writing .pyc and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System dependencies (needed by asyncpg / psycopg, python-socks, etc.)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer caching)
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir .

# Copy application source
COPY src/ ./src/
COPY scripts/ ./scripts/

# Non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser
USER appuser

CMD ["python", "-m", "src.main"]
