"""Run many accounts from one SQLite file.

First add accounts (once per account):
    python -m eitaagram login --db accounts.db --name acc1 --phone +98912...
    python -m eitaagram login --db accounts.db --name acc2 --phone +98935...
"""

import asyncio
import logging

from eitaagram import AccountPool, AccountState, filters

logging.basicConfig(level=logging.INFO)

pool = AccountPool(
    "accounts.db",
    global_rate=20,  # calls/second shared by all accounts
    account_rate=3,  # calls/second per account
    proxies={},  # {"acc2": "socks5://user:pass@host:1080"} -> that account uses its own proxy
)


@pool.on_message(filters.private & filters.incoming & filters.command("whoami"))
async def whoami(client, message):  # runs for whichever account received the message
    await message.reply(f"account: {client.name}")




async def main() -> None:
    pool.load_stored()  # every account in accounts.db
    pool.add_listener("unauthorized", lambda acc, exc: logging.warning("%s needs login again: %s", acc.name, exc))
    pool.add_listener("flood", lambda acc, method, wait: logging.warning("%s: %s must rest %ss", acc.name, method, wait))

    states = await pool.start()
    for name, state in states.items():
        print(f"{name:12} {state.value}")

    # Run something on every healthy account (a failing account never stops the others):
    results = await pool.map(lambda c: c.get_me(), concurrency=3)
    for name, result in results.items():
        print(name, "->", result if isinstance(result, BaseException) else result.full_name)

    ready = [a.name for a in pool.ready()]
    print("ready:", ready, "| not ready:", [n for n, s in states.items() if s is not AccountState.READY])
    await pool.idle()
    await pool.stop()


asyncio.run(main())
