import hmac
import hashlib
import base64
import requests
import json

# 請將這裡的值改成您實際使用的
CHANNEL_SECRET = "8436c282a1fc13505d38750d7e0b93cb"
WEBHOOK_URL = "https://wosredeem-production.up.railway.app/line_webhook"

body = {
    "events": [{
        "type": "message",
        "message": {"type": "text", "text": "/查看清單"},
        "replyToken": "測試用",
        "source": {"groupId": "C58bd3b35d69cb4514c002ff78ba1a49e"}
    }]
}

# 簽名計算需使用最純粹的 JSON 字串
raw_body = json.dumps(body, separators=(',', ':')).encode("utf-8")

# 生成 X-Line-Signature
signature = hmac.new(CHANNEL_SECRET.encode("utf-8"), raw_body, hashlib.sha256).digest()
signature_b64 = base64.b64encode(signature).decode()

print("X-Line-Signature:", signature_b64)

# 發送 POST
resp = requests.post(WEBHOOK_URL, data=raw_body, headers={
    "Content-Type": "application/json",
    "X-Line-Signature": signature_b64
})

print("Status:", resp.status_code)
print("Response:", resp.text)
