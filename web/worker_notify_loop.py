#worker_notify_loop.py
import os
import asyncio
import time
from redeem_web import check_and_send_notify

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    print(f"[Worker] 啟動中，INTERNAL_SECRET 頭五碼：{os.getenv('INTERNAL_SECRET')[:5]}...")
    while True:
        try:
            loop.run_until_complete(check_and_send_notify())
        except Exception as e:
            print(f"[Worker Error] {e}")
        time.sleep(30)
