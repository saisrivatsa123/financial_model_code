
FROM python:3.12-slim

WORKDIR /app

# Install dependencies for Python packages
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create runtime data directory
RUN mkdir -p /app/data

# Streamlit port
EXPOSE 10000

# Launch Streamlit
CMD ["sh", "-c", "streamlit run app.py --server.address=0.0.0.0 --server.port=${PORT:-10000} --server.headless=true"]
