FROM python:3.12-slim

# Poetry config — no venv inside container, no prompts
ENV POETRY_VERSION=1.8.5 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TIKTOKEN_CACHE_DIR=/root/.tiktoken

# Give network operations more room before giving up.
ENV POETRY_REQUESTS_TIMEOUT=60 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=10

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "poetry==$POETRY_VERSION"

WORKDIR /app

# Install deps first (layer cached unless pyproject/lock changes).
# Retry with increasing backoff so a flaky network doesn't fail the build.
COPY pyproject.toml poetry.lock* ./
RUN n=0; until poetry install --no-root --only main; do \
        n=$((n+1)); \
        if [ "$n" -ge 5 ]; then echo "poetry install failed after $n attempts"; exit 1; fi; \
        echo "poetry install failed (attempt $n) — retrying in $((n*15))s..."; \
        sleep $((n*15)); \
    done

COPY . .

# Pre-download tiktoken cl100k_base encoding with retries (no retry logic in tiktoken itself).
# Non-fatal: if the download can't complete during the build (e.g. blocked/slow
# network), tiktoken will fetch it lazily at runtime instead of failing the image.
RUN mkdir -p $TIKTOKEN_CACHE_DIR && \
    curl --retry 8 --retry-delay 5 --retry-all-errors --connect-timeout 20 --max-time 300 -fL \
    "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken" \
    -o "$TIKTOKEN_CACHE_DIR/$(python3 -c "import hashlib; print(hashlib.sha1(b'https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken').hexdigest())")" \
    || echo "WARN: tiktoken pre-download failed during build — it will be fetched at runtime."

EXPOSE 5000

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60", "--access-logfile", "-"]
