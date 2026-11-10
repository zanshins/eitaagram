"""Self-bot: react to *your own* outgoing messages (e.g. a ``.ping`` command in any chat)."""

import asyncio

from eitaagram import Client, filters


async def main() -> None:
    app = Client("me", phone="+989121234567")

    @app.on_message(filters.me & filters.regex(r"^\.ping$"))
    async def ping(client, message):
        await message.edit("pong ✅")

    await app.run()


asyncio.run(main())
