FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

USER root
RUN playwright install --with-deps \
 && apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*
USER pwuser

COPY . .
CMD ["python", "bot/gua_gua_bot.py"]