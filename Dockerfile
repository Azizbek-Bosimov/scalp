FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY uuf_fixed6.py .

ENV PORT=8080
ENV DATA_DIR=/data

CMD ["gunicorn", "uuf_fixed6:app", "--worker-class", "aiohttp.GunicornWebWorker", "--workers", "1", "--bind", "0.0.0.0:8080", "--timeout", "120"]