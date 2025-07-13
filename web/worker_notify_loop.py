# worker_notify_loop.py
import os
import time
import requests

if __name__ == "__main__":
    secret = os.getenv("INTERNAL_SECRET")
    url = os.getenv("INTERNAL_PUSH_URL")  # 新增這個環境變數

    if not secret or not url:
        print("❌ INTERNAL_SECRET 或 INTERNAL_PUSH_URL 未設定")
        exit(1)

    print(f"[Worker] 啟動中，推播模式切換為呼叫 internal_push_notify URL：{url}")

    while True:
        try:
            resp = requests.post(url, json={"secret": secret}, timeout=10)
            print(f"[Worker] internal_push_notify 呼叫完成：{resp.status_code} {resp.text}")
        except Exception as e:
            print(f"[Worker Error] {e}")
        time.sleep(30)