FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY crypto_signal_bot_advanced.py .

CMD ["python", "-u", "crypto_signal_bot_advanced.py"]
