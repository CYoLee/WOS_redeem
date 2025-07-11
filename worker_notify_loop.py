import time
from redeem_web import run_push_notify

if __name__ == "__main__":
    while True:
        try:
            run_push_notify()
        except Exception as e:
            print(f"[Worker Error] {e}")
        time.sleep(30)  # 每 30 秒偵測一次
