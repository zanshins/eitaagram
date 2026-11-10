"""Command line helper: ``python -m eitaagram <command>``.

* ``login``   -- log one account in (interactive) and store it in a SQLite file
* ``list``    -- show stored accounts (never prints tokens)
* ``doctor``  -- probe which ``layer`` / ``flags`` / endpoints the server accepts
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from . import Client, ClientProfile, EitaaError, SQLiteSession

_COMBOS = [(135, 32), (135, 128), (135, 0), (133, 0), (133, 32), (137, 0), (137, 32)]


async def _login(args: argparse.Namespace) -> int:
    client = Client(SQLiteSession(args.db, args.name), phone=args.phone, no_updates=True)
    try:
        await client.start(first_name=args.first_name)
        me = client.me
        print(f"logged in as {me.full_name if me else '?'} (id {client.me_id}), stored in {args.db} as {args.name!r}")
        return 0
    except EitaaError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.stop()


async def _list(args: argparse.Namespace) -> int:
    names = SQLiteSession.list_names(args.db)
    if not names:
        print("no accounts stored")
        return 0
    for name in names:
        session = SQLiteSession(args.db, name)
        await session.load()
        d = session.data
        print(f"{name:20} phone={d.phone or '-':14} user_id={d.user_id or '-':<12} authorized={bool(d.token)}")
    return 0


async def _doctor(args: argparse.Namespace) -> int:
    base = ClientProfile()
    print("== endpoint reachability (unauthenticated request, a rejection still proves the host is alive) ==")
    probe = Client(SQLiteSession(args.db, args.name), profile=base, no_updates=True, rate_limit=None, max_retries=0)
    await probe.connect()
    for url in base.client_endpoints:
        started = time.perf_counter()
        try:
            await probe.invoker.invoke("updates.getState", endpoint=url, retries=0)  # type: ignore[union-attr]
            verdict = "ok"
        except EitaaError as exc:
            verdict = f"{type(exc).__name__}: {exc}"[:90]
        print(f"{url:42} {(time.perf_counter() - started) * 1000:6.0f} ms  {verdict}")
    await probe.stop()

    session = SQLiteSession(args.db, args.name)
    await session.load()
    if not session.data.token:
        print("\nNo stored login for that name; run `login` first to calibrate layer/flags.")
        return 0
    print("\n== layer / flags calibration (users.getUsers self) ==")
    winners = []
    for layer, flags in _COMBOS:
        client = Client(
            SQLiteSession(args.db, args.name),
            profile=base.with_(layer=layer, flags=flags),
            no_updates=True,
            rate_limit=None,
            max_retries=0,
            auto_refresh_token=False,
            session_flush_interval=0,
        )
        try:
            await client.connect()
            await client.invoke("users.getUsers", {"id": [{"_": "inputUserSelf"}]})
            print(f"layer={layer} flags={flags:<4} OK")
            winners.append((layer, flags))
        except EitaaError as exc:
            print(f"layer={layer} flags={flags:<4} {type(exc).__name__}: {str(exc)[:80]}")
        finally:
            await client.disconnect()
        await asyncio.sleep(0.5)
    print(f"\naccepted combinations: {winners or 'none'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eitaagram")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    login = sub.add_parser("login", help="log an account in and store it")
    login.add_argument("--db", type=Path, default=Path("accounts.db"))
    login.add_argument("--name", required=True)
    login.add_argument("--phone", required=True)
    login.add_argument("--first-name", default=None, help="needed only if the number is not registered yet")
    ls = sub.add_parser("list", help="list stored accounts")
    ls.add_argument("--db", type=Path, default=Path("accounts.db"))
    doc = sub.add_parser("doctor", help="probe endpoints and accepted layer/flags")
    doc.add_argument("--db", type=Path, default=Path("accounts.db"))
    doc.add_argument("--name", default="default")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    handler = {"login": _login, "list": _list, "doctor": _doctor}[args.cmd]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
