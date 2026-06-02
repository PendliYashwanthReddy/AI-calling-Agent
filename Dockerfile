FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libglib2.0-0 \
        libsndfile1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

# Normalise line endings so the shell script runs on Linux even if it was
# authored on Windows (CRLF would otherwise break `sh start.sh`).
RUN sed -i 's/\r$//' start.sh

# Storage is Supabase (no local sqlite). All configuration comes from the
# runtime environment — no secrets are baked into this image.

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["sh", "start.sh"]
