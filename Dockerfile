FROM python:3.11

WORKDIR /app

COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install ccxt pandas numpy requests ta

COPY . .

CMD ["python", "crypto_signal_bot_ultima.py"]
