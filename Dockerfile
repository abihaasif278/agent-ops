FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY data ./data
RUN pip install --no-cache-dir -e ".[anthropic]" && useradd --create-home app && chown -R app /app
USER app
# Default: long-running scheduler. Override with `docker run ... agent-ops run --print` for a one-off run.
CMD ["agent-ops", "schedule"]
