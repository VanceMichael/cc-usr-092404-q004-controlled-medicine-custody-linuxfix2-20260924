FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations
ENV DATABASE_PATH=/app/data/pharmacy_identity.sqlite3
CMD ["sh", "-c", "python -m alembic upgrade head && gunicorn --bind 0.0.0.0:${PORT:-8080} 'pharmacy_identity:create_app()'"]
