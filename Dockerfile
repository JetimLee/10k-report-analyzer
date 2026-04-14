FROM python:3.10.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VERSION=1.8.3 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1 \
    BRUIN_VERSION=0.11.528 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://raw.githubusercontent.com/bruin-data/bruin/main/install.sh \
    | sh -s -- -b /usr/local/bin "v${BRUIN_VERSION}"

RUN pip install "poetry==${POETRY_VERSION}"

WORKDIR /app

COPY pyproject.toml poetry.lock ./
# Creates /app/.venv with Linux-native binaries. The anonymous volume in
# docker-compose.yml inherits this venv and keeps it isolated from the
# host's macOS .venv/.
RUN poetry install --no-root --only main \
    && test -x /app/.venv/bin/python

# Pre-download the sentence-transformers model so the first pipeline run
# doesn't stall on a 90MB HuggingFace download inside the container.
RUN /app/.venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

COPY . .

EXPOSE 8501

CMD ["streamlit", "run", "dashboard.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true"]
