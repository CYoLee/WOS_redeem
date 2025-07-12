FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt
RUN apt-get update && apt-get install -y curl && apt-get clean
RUN playwright install --with-deps
RUN echo "force rebuild 20250712"
COPY . .
