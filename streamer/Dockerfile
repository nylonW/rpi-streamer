FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    v4l-utils \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
COPY . .

EXPOSE 5001

CMD ["python", "app.py"]