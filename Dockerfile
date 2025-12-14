FROM python:3.12-slim

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV INBOX_DIR=/app/inbox
ENV TEXT_DIR=/app/extracted_text

CMD ["python", "main.py"]
