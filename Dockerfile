FROM python:3.12-slim

WORKDIR /app

# 動態貼圖轉換需要 ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "gunicorn", "bot:app", "--workers", "1", "--threads", "8", "--timeout", "0", "--bind", "0.0.0.0:8080"]
