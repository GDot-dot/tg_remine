FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# 對齊原 LINE 版本：gunicorn 1 worker 8 threads timeout 0
CMD ["python", "-m", "gunicorn", "bot:app", "--workers", "1", "--threads", "8", "--timeout", "0", "--bind", "0.0.0.0:8080"]
