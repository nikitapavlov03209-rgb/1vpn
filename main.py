import asyncio
import logging
import uvicorn
from uvicorn import Config, Server
from app.webhooks import app
from app.bot.launcher import run_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

async def run_web():
    config = Config(app=app, host="0.0.0.0", port=8000, loop="asyncio", lifespan="on")
    server = Server(config)
    await server.serve()

async def main():
    bot_task = asyncio.create_task(run_bot(), name="bot")
    api_task = asyncio.create_task(run_web(), name="web")
    done, pending = await asyncio.wait({bot_task, api_task}, return_when=asyncio.FIRST_EXCEPTION)
    for t in done:
        exc = t.exception()
        if exc:
            logging.exception("Task failed", exc_info=exc)
    for t in pending:
        t.cancel()
    if any(t.exception() for t in done):
        raise SystemExit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
