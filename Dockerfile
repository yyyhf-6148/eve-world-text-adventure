FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 下载星系数据（Fuzzwork CSV）
RUN python download_universe.py

RUN mkdir -p /app/data

EXPOSE 8765

CMD ["python", "main.py"]