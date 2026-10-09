FROM python:3.11-slim

WORKDIR /app

# 安裝基本相依套件
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式原始碼、題庫資料庫及插圖
COPY practice.py index.html question_bank.db ./
COPY uploads/ ./uploads/

# 雲端自動分配 Port
ENV PORT=8080
EXPOSE 8080

CMD ["python", "practice.py"]
