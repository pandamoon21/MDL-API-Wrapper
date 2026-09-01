FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# deps layer first for better build caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app code
COPY mdlaw.py .

# non-root user (production hygiene)
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "mdlaw:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
