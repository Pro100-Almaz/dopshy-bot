FROM python:3.12-slim

# Poetry config — no venv inside container, no prompts
ENV POETRY_VERSION=1.8.5 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TIKTOKEN_CACHE_DIR=/root/.tiktoken

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "poetry==$POETRY_VERSION"

WORKDIR /app

# Install deps first (layer cached unless pyproject/lock changes)
COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --only main

COPY . .

# Pre-download tiktoken cl100k_base encoding with retries (no retry logic in tiktoken itself)
RUN mkdir -p $TIKTOKEN_CACHE_DIR && \
    curl --retry 5 --retry-delay 3 --retry-all-errors -fL \
    "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken" \
    -o "$TIKTOKEN_CACHE_DIR/$(python3 -c "import hashlib; print(hashlib.sha1(b'https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken').hexdigest())")"

EXPOSE 5000

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60", "--access-logfile", "-"]
