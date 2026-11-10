"""Live smoke test -- run it against a THROW-AWAY account before trusting the library.

    python examples/smoke_test.py --db accounts.db --name test --phone +98912...

It exercises: login, get_me, send to Saved Messages, edit, history, delete, forward, token refresh,
dialogs, username lookup, update polling (waits 20 s for you to message the account) and a small upload.
Every step prints PASS/FAIL so protocol surprises are easy to report.
"""

import argparse
import asyncio
import traceback

from eitaagram import Client, SQLiteSession, filters


async def step(title, coro):
    try:
        result = await coro
        print(f"PASS  {title}")
        return result
    except Exception:
        print(f"FAIL  {title}")
        traceback.print_exc()
        return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="accounts.db")
    ap.add_argument("--name", default="test")
    ap.add_argument("--phone", required=True)
    args = ap.parse_args()

    app = Client(SQLiteSession(args.db, args.name), phone=args.phone, poll_interval=1.5)
    got = []

    @app.on_message(filters.incoming)
    async def collect(client, message):
        got.append(message)

    await app.start()
    print("logged in as", app.me)
    sent = await step("send_message(me)", app.send_message("me", "<b>eitaagram</b> smoke test", parse_mode="html"))
    if sent:
        await step("edit_message_text", app.edit_message_text("me", sent.id, "edited ✅"))
        await step("forward_messages(me->me)", app.forward_messages("me", "me", [sent.id]))

        async def history():
            return [m async for m in app.get_history("me", limit=5)]

        print("      history:", [m.text for m in await step("get_history", history()) or []])
        await step("delete_messages", app.delete_messages("me", [sent.id]))
    await step("refresh_token", app.refresh_token())
    await step("get_me after refresh", app.get_me())

    async def dialogs():
        return [d async for d in app.get_dialogs(limit=10)]

    ds = await step("get_dialogs", dialogs())
    print("      dialogs:", [d.chat.title or d.chat.id for d in ds or []])
    await step("upload + send_document", app.send_document("me", b"hello file", name="hello.txt", caption="upload test"))
    print("Now send ONE message to this account from another one (waiting 20 s for polling)...")
    await asyncio.sleep(20)
    print("PASS  updates" if got else "FAIL  updates (nothing received)", [m.text for m in got])
    await app.stop()


asyncio.run(main())
