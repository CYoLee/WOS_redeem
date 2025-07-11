import asyncio
from gua_gua_bot import run_push_notify

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        try:
            loop.run_until_complete(run_push_notify())
        except Exception as e:
            print(f"[Worker Error] {e}")
        time.sleep(30)
