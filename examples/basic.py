"""One account: log in once (the session is stored), send a message, listen for replies."""

import asyncio

from eitaagram import Client, filters


async def main() -> None:
    app = Client("alice", phone="+989121234567")  # session file: ./alice.session

    @app.on_message(filters.private & filters.incoming & filters.command("ping"))
    async def ping(client, message):
        await message.reply("pong")

    async with app:  # first run asks for the login code on stdin
        await app.send_message("me", "hello from eitaagram")
        await app.idle()


asyncio.run(main())
