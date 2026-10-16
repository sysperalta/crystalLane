# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && useradd --system --uid 10001 --no-create-home appuser
COPY app ./app
COPY tms_sim ./tms_sim
COPY openapi.yaml .

# --- test image: docker build --target test -t clf-adapter:test . && docker run --rm clf-adapter:test
FROM base AS test
COPY requirements-dev.txt pytest.ini ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
USER 10001
CMD ["python", "-m", "pytest", "-q"]

# --- runtime image (default target)
FROM base AS runtime
USER 10001
ENV PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 CMD ["python", "-m", "app.healthcheck"]
# Single worker on purpose: call state (OTP attempts, rounds, approvals) is in memory.
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log --timeout-graceful-shutdown 5"]
