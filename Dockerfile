FROM python:3.12-slim

# Poetry config — no venv inside container, no prompts
ENV POETRY_VERSION=1.8.5 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir "poetry==$POETRY_VERSION"

WORKDIR /app

# Install deps first (layer cached unless pyproject/lock changes)
COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --only main

COPY . .

EXPOSE 5000

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60", "--access-logfile", "-"]
