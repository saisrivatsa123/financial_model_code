
FROM python:3.12-slim

WORKDIR /app

# Limit compilation parallelism
ENV CMAKE_BUILD_PARALLEL_LEVEL=1
ENV MAX_JOBS=1
ENV MAKEFLAGS="-j1"
ENV PYTHONUNBUFFERED=1

# Install build tools
RUN apt-get update && apt-get install -y \
    --no-install-recommends \
    build-essential \
    cmake \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first
COPY requirements.txt .

# Install dependencies with limited build parallelism

RUN pip install --no-cache-dir \
    --prefer-binary \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu \
    -r requirements.txt

# Copy application code
COPY . .

RUN mkdir -p /app/data

EXPOSE 10000

CMD ["sh", "-c", "streamlit run app.py --server.address=0.0.0.0 --server.port=${PORT:-10000} --server.headless=true"]
