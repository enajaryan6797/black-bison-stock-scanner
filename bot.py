import os
import time

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
CHAT_ID = os.getenv("CHAT_ID")

print("BLACK BISON STOCK SCANNER TEST STARTED", flush=True)
print("BOT_TOKEN FOUND:", bool(BOT_TOKEN), flush=True)
print("ALPACA_API_KEY FOUND:", bool(ALPACA_API_KEY), flush=True)
print("ALPACA_SECRET_KEY FOUND:", bool(ALPACA_SECRET_KEY), flush=True)
print("CHAT_ID:", CHAT_ID, flush=True)

while True:
    print("STOCK BOT IS ALIVE", flush=True)
    time.sleep(30)
