# eitaagram

Async, typed, **multi-account** Eitaa client library for Python 3.11+ with a Telethon / Pyrogram-style API.
Built from the protocol knowledge in **eitaa-cli**, **eitaa-serializer-laravel** and **EitaaSharp** (see `NOTICE.md`).

> **Status: 0.1 – beta.** 74 offline tests pass against an in-process fake gateway that speaks the real binary
> protocol. It has **not** been run against the live Eitaa servers. Run `examples/smoke_test.py` on a throw-away
> account and `python -m eitaagram doctor` before using it on accounts you care about.

## Install

```bash
pip install -e .            # needs httpx[http2]
pip install -e .[crypto]    # optional: encrypted sessions (Fernet)
```

## Quick start

```python
import asyncio
from eitaagram import Client, filters

async def main():
    async with Client("alice", phone="+989121234567") as app:   # asks for the code on first run
        await app.send_message("me", "<b>hello</b>", parse_mode="html")

        @app.on_message(filters.private & filters.command("ping"))
        async def ping(client, message):
            await message.reply("pong")

        await app.idle()

asyncio.run(main())
```

## Many accounts (tabchi / self-bot farms)

```bash
python -m eitaagram login --db accounts.db --name acc1 --phone +98912...
python -m eitaagram login --db accounts.db --name acc2 --phone +98935...
python -m eitaagram list  --db accounts.db
```

```python
pool = AccountPool("accounts.db", global_rate=20, account_rate=3,
                   proxies={"acc2": "socks5://user:pass@host:1080"})
pool.load_stored()

@pool.on_message(filters.private & filters.incoming)       # registered on every account
async def handler(client, message): ...

states = await pool.start()                                 # one bad account never blocks the rest
results = await pool.map(lambda c: c.get_me(), concurrency=3)   # errors come back per account
await pool.run()
```

Pool features: per-account health state (`ready / flood / unauthorized / banned / error`), shared + per-account
rate limiting, FloodWait-aware fan-out (flooded accounts are skipped, not hammered), per-account proxy, pool-wide
handlers, listeners (`flood`, `unauthorized`, `token_refreshed`, `state`), `status()` snapshots without secrets,
session export/import as strings.

## What is inside

| Area | Notes |
|---|---|
| TL codec | Schema-driven (layer 135, 1008 constructors / 431 methods), strict decoding, gzip/rpc_result unwrapping, verified against a golden fixture |
| RPC engine | Envelope handling, **single-flight token refresh**, FloodWait sleep/fail-fast with a per-method guard, backoff for transport/5xx, `INVALID_CONSTRUCTOR` learning |
| Transport | HTTP/2 via httpx, endpoint health + cooldown + failover, upload host pinning, per-account proxy |
| Sessions | `MemorySession`, `StringSession` (`eg1:` portable string), `FileSession` (atomic, 0600), `SQLiteSession` (many accounts per file), optional Fernet encryption |
| Updates | `getDifference` polling with persisted cursor, channel difference, de-duplication, handler groups, `StopPropagation` |
| API | auth (send/resend/sign-in/sign-up/guided `authorize`), messages (send/edit/delete/forward/history/search/read), chats (dialogs/join/leave/members), files (upload/download, beta), account |

Chat ids follow the Pyrogram convention: users `id`, basic groups `-id`, channels/supergroups `-1000000000000 - id`.
Raw access is always available: `await app.invoke("method.name", {...})`.

## Known limitations (honest list)

* **Not validated live.** Wire details the reference projects disagree on (`layer` 133/135/137, envelope `flags`
  0/32/128, OTP delivery channel) are configurable through `ClientProfile`; `python -m eitaagram doctor` probes
  which combination your server accepts.
* **Two-step verification (cloud password) is not implemented**; such accounts raise `SessionPasswordNeeded`.
* **Polling only** (no socket): updates arrive with `poll_interval` latency; typing indicators are unsupported.
* Channel gap recovery is best-effort when the local channel cursor is unknown.
* File upload/download follows the reference captures but is marked beta.
* Typed wrappers cover the common objects; everything else is available as raw dicts (`message.raw`).

## Responsible use

Accounts keep one stable device identity each and the library obeys server waits. It ships no stealth or
anti-detection features; mass unsolicited messaging violates Eitaa's terms and can get accounts banned.

## Development

```bash
PYTHONPATH=src:tests python -m unittest discover -s tests     # or: pytest
```
