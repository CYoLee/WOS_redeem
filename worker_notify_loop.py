import asyncio
from redeem_web import check_and_send_notify

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        try:
            loop.run_until_complete(check_and_send_notify())
        except Exception as e:
            print(f"[Worker Error] {e}")
        time.sleep(30)
