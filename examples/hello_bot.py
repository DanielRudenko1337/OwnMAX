import asyncio
import logging
import os
from pathlib import Path

import aiohttp
from ownmax.settings import DATA_DIR

STATE = Path(os.getenv("OWNMAX_BOT_STATE", str(DATA_DIR / "hello-bot.marker")))
BASE = os.getenv("OWNMAX_BOT_API", "http://127.0.0.1:9183/botapi/").rstrip("/") + "/"


def answer_for(body):
    text = body.get("text", "").strip().casefold()
    if text in ("привет", "привет!", "hello", "hi"):
        return "Привет!"
    started = any(
        item.get("_type") == "CONTROL" and item.get("event") == "botStarted"
        for item in body.get("attachments", [])
    )
    if started or text == "/start":
        return "Привет! Напиши «привет» или используй /help."
    if text == "/help":
        return "Команды: /start, /help, /ping. Напиши «привет» — я отвечу."
    if text == "/ping":
        return "pong"
    return None


async def run():
    token = os.environ["OWNMAX_BOT_TOKEN"]
    marker = int(STATE.read_text()) if STATE.exists() else 0
    async with aiohttp.ClientSession(
        headers={"Authorization": token}, timeout=aiohttp.ClientTimeout(total=15)
    ) as http:
        while True:
            try:
                async with http.get(
                    BASE + "updates", params={"marker": marker, "limit": 50}
                ) as response:
                    if response.status == 401:
                        raise RuntimeError("Bot token revoked")
                    response.raise_for_status()
                    result = await response.json()
                for update in result["updates"]:
                    message = update["message"]
                    body = message["body"]
                    answer = answer_for(body)
                    if answer:
                        async with http.post(
                            BASE + "messages",
                            params={"chat_id": message["recipient"]["chat_id"]},
                            json={"text": answer},
                        ) as response:
                            response.raise_for_status()
                    marker = max(marker, int(body["mid"]))
                    STATE.parent.mkdir(parents=True, exist_ok=True)
                    temporary = STATE.with_suffix(".tmp")
                    temporary.write_text(str(marker))
                    temporary.replace(STATE)
                await asyncio.sleep(1)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                logging.warning("Bot API unavailable; retrying")
                await asyncio.sleep(3)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
