FROM python:3.12-slim

WORKDIR /app

# 安裝依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製程式碼
COPY . .

# 使用 PORT 環境變數（Fly.io 預設 8080）
ENV PORT=8080

CMD ["python", "bot.py"]
