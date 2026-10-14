# Mirrors App1's two-stage build. Same shape, different language - which is
# the point: the pipeline treats them identically.
FROM python:3.12-slim AS deps
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

COPY --from=deps /install /usr/local
COPY main.py ./
COPY static ./static

# python:3.12-slim has no non-root user by default, so create one. A service
# that only serves HTTP has no need for root.
RUN useradd --create-home --uid 1001 appuser
USER appuser

EXPOSE 8080
CMD ["python", "main.py"]
