FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt
RUN apt-get update && apt-get install -y curl && apt-get clean

RUN apt-get install -y fonts-noto-cjk && apt-get clean  
# 建議：playwright 有時候中文字問題，加入 Noto CJK 字型

RUN playwright install --with-deps

COPY . .

CMD ["python", "bot/gua_gua_bot.py"]  
# 預設給 A Service 用，B、C 再自行 override