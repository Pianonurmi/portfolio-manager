FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv/app
RUN useradd --system --uid 10001 --home /srv/app pm
# a Unicode TTF for the encrypted emergency PDF (ä, ö, €); fpdf2 bundles none
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

FROM base AS deps
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install ".[dev]" && pip uninstall -y portfolio-manager

FROM deps AS dev
COPY . .
USER pm
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM base AS prod
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install . && pip uninstall -y portfolio-manager
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
# originals of imported files and uploaded documents; a new named volume copies this ownership
RUN mkdir -p /srv/imports && chown pm /srv/imports
USER pm
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
