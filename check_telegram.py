"""
Telegram user-client verification tool.

Proves the MTProto login works, shows exactly which of your chats the bot will
watch, and demonstrates the hiring filter on your real message history -- so you
can see what it would have caught before trusting it to run unattended.

    python check_telegram.py              connect + dialogs + sample scan
    python check_telegram.py --dialogs    just list what you have joined
    python check_telegram.py --scan 24    scan the last 24 hours for job posts
    python check_telegram.py --live 60    watch live for 60 seconds
    python check_telegram.py --pipeline   show the JobPost records produced
    python check_telegram.py --scan 168 --suggest
                                          rank chats by hiring output and emit
                                          a narrowed include_chats list

Read-only throughout: it never sends, joins or reacts to anything.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from config import settings
from scrapers.telegram_user_client import (
    ChatFilter,
    TelegramAuthError,
    build_client,
    ensure_authorized,
    is_hiring_post,
    message_to_job,
    tech_hits,
    telethon_available,
)

GREEN, RED, YELLOW, CYAN, DIM, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[1m", "\033[0m"
)
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = CYAN = DIM = BOLD = RESET = ""


def ok(m: str) -> None:
    print(f"  {GREEN}[ OK ]{RESET} {m}")


def fail(m: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {m}")


def warn(m: str) -> None:
    print(f"  {YELLOW}[WARN]{RESET} {m}")


def info(m: str) -> None:
    print(f"  {DIM}       {m}{RESET}")


def header(t: str) -> None:
    print(f"\n{BOLD}{t}{RESET}\n" + "-" * max(40, len(t)))


def _short(text: str, n: int = 88) -> str:
    flat = " ".join((text or "").split())
    return flat[:n] + ("..." if len(flat) > n else "")


async def run_checks(args: argparse.Namespace) -> int:
    header("1. Prerequisites")
    if not telethon_available():
        fail("Telethon is not installed -- run: pip install -r requirements.txt")
        return 1
    ok("Telethon installed")

    if not settings.telegram_api_id or not settings.telegram_api_hash:
        fail("TELEGRAM_API_ID / TELEGRAM_API_HASH missing from .env")
        return 1
    ok(f"api_id {settings.telegram_api_id}")

    if not settings.telegram_ready:
        fail("No Telegram session yet.")
        info("Run the one-time login first:  python auth_telegram.py")
        return 1
    ok("Session available (" +
       ("TELEGRAM_STRING_SESSION" if settings.telegram_session else "local .session file")
       + ")")

    header("2. Connection")
    try:
        client = build_client()
        await ensure_authorized(client)
    except TelegramAuthError as exc:
        fail(str(exc))
        return 1
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        return 1

    try:
        me = await client.get_me()
        ok(f"Signed in as {getattr(me, 'first_name', '')} "
           f"(@{getattr(me, 'username', None) or 'no-username'}, id {me.id})")
        if getattr(me, "phone", None):
            info(f"phone +{me.phone}")

        # -- dialogs -------------------------------------------------------
        header("3. Your chats")
        cfg = settings.source("telegram_user")
        chat_filter = ChatFilter.from_config(cfg)
        max_dialogs = int(cfg.get("max_dialogs", 250))

        kinds: Counter[str] = Counter()
        watched, skipped = [], []
        async for dialog in client.iter_dialogs(limit=max_dialogs):
            if dialog.is_user:
                kind = "private"
            elif dialog.is_group:
                kind = "group"
            elif dialog.is_channel:
                kind = "channel"
            else:
                kind = "other"
            kinds[kind] += 1
            (watched if chat_filter.allows(dialog) else skipped).append((kind, dialog))

        ok(f"{sum(kinds.values())} dialogs visible -- " +
           ", ".join(f"{n} {k}" for k, n in kinds.most_common()))
        ok(f"{len(watched)} will be monitored, {len(skipped)} filtered out")

        if args.dialogs or args.verbose:
            print()
            print(f"  {'TYPE':<9}{'MEMBERS':>9}  CHAT")
            print("  " + "-" * 74)
            for kind, dialog in watched[: args.limit]:
                entity = dialog.entity
                members = getattr(entity, "participants_count", None)
                username = getattr(entity, "username", None)
                label = dialog.name or "(untitled)"
                tag = f" @{username}" if username else ""
                print(f"  {CYAN}{kind:<9}{RESET}{str(members or '-'):>9}  "
                      f"{_short(label, 52)}{DIM}{tag}{RESET}")
            if len(watched) > args.limit:
                info(f"... and {len(watched) - args.limit} more")

        if args.dialogs and not args.suggest:
            return 0

        # -- history scan --------------------------------------------------
        hours = args.scan
        header(f"4. Hiring-post scan (last {hours}h)")
        require_tech = bool(cfg.get("require_tech_match", True))
        info(f"filter: hiring keyword {'AND' if require_tech else 'OR (tech optional)'}"
             f" technical keyword")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        per_dialog = int(cfg.get("messages_per_dialog", 60))
        scanned = 0
        matches: list[tuple[str, str | None, object]] = []

        from telethon.errors import FloodWaitError

        for _kind, dialog in watched:
            try:
                async for msg in client.iter_messages(dialog.entity, limit=per_dialog):
                    when = getattr(msg, "date", None)
                    if when and when.astimezone(timezone.utc) < cutoff:
                        break
                    scanned += 1
                    text = (msg.message or "").strip()
                    if is_hiring_post(text, require_tech):
                        matches.append((
                            dialog.name or "?",
                            getattr(dialog.entity, "username", None),
                            msg,
                        ))
            except FloodWaitError as exc:
                warn(f"flood-wait {exc.seconds}s -- stopping the scan early")
                break
            except Exception:
                continue
            await asyncio.sleep(0.25)

        ok(f"Scanned {scanned} message(s) across {len(watched)} chat(s)")
        if matches:
            ok(f"{GREEN}{len(matches)} look like technical job posts{RESET}")
        else:
            warn("No matching posts in that window.")
            info("That is normal for a quiet period. Try --scan 168 (a week),")
            info("or set require_tech_match: false in config.yml to widen it.")

        for chat, _username, msg in matches[: args.limit]:
            text = (msg.message or "").strip()
            when = getattr(msg, "date", None)
            stamp = when.strftime("%Y-%m-%d %H:%M") if when else "?"
            print(f"\n  {BOLD}{_short(chat, 40)}{RESET} {DIM}{stamp}{RESET}")
            print(f"    {_short(text, 150)}")
            hits = tech_hits(text)
            if hits:
                print(f"    {CYAN}tech:{RESET} {', '.join(hits)}")

        # -- what the pipeline would receive --------------------------------
        if args.pipeline and matches:
            header("5. JobPost records handed to the pipeline")
            from relevance import score_job

            for chat, username, msg in matches[:5]:
                job = message_to_job(
                    (msg.message or "").strip(),
                    chat_name=chat,
                    username=username,
                    chat_id=msg.chat_id,
                    message_id=msg.id,
                    posted_at=getattr(msg, "date", None),
                )
                score, _why = score_job(job, settings.profile)
                verdict = (f"{GREEN}passes{RESET}" if score >= 2
                           else f"{YELLOW}dropped by pre-filter{RESET}")
                print(f"\n  source : {job.source}")
                print(f"  title  : {_short(job.title, 70)}")
                print(f"  link   : {job.url}")
                print(f"  prefilter score {score} -> {verdict}")

        # -- suggest a narrower include_chats --------------------------------
        if args.suggest:
            header("5. Which chats are actually worth watching")
            print(f"  {DIM}Ranking your {len(watched)} monitored chats by how many "
                  f"hiring posts they\n  actually produced in the last {hours}h.{RESET}\n")

            tally: dict[str, dict] = {}
            for chat, _username, msg in matches:
                row = tally.setdefault(chat, {"hits": 0, "tech": 0})
                row["hits"] += 1
                row["tech"] += len(tech_hits(msg.message or ""))

            productive = sorted(
                tally.items(), key=lambda kv: (-kv[1]["hits"], -kv[1]["tech"])
            )
            if not productive:
                warn("No chat produced a hiring post in that window.")
                info("Try a longer window: python check_telegram.py --scan 168 --suggest")
            else:
                print(f"  {'HITS':>5}{'TECH':>6}  CHAT")
                print("  " + "-" * 62)
                for chat, row in productive:
                    print(f"  {row['hits']:>5}{row['tech']:>6}  {_short(chat, 48)}")

                idle = len(watched) - len(productive)
                print()
                ok(f"{len(productive)} chat(s) produced jobs; {idle} produced nothing")
                if idle:
                    info(f"Watching all {len(watched)} costs ~{len(watched) // 3}s per run "
                         f"and risks flood-waits for no gain.")
                print(f"\n  {BOLD}Paste this into config.yml under telegram_user:{RESET}\n")
                print("  include_chats:")
                for chat, row in productive:
                    safe = chat.replace('"', "'")
                    print(f'    - "{safe}"    # {row["hits"]} hiring post(s)')
                print(f"\n  {DIM}Matching is a case-insensitive substring of the chat "
                      f"title,\n  so a distinctive fragment is enough.{RESET}")

        # -- live watch ------------------------------------------------------
        if args.live:
            header(f"6. Live listener ({args.live}s)")
            from telethon import events

            seen = {"total": 0, "hits": 0}
            print(f"  {DIM}Waiting for new messages... post something in a watched")
            print(f"  group to test it. Ctrl-C to stop early.{RESET}\n")

            @client.on(events.NewMessage)
            async def _handler(event):  # noqa: ANN001, ANN202
                seen["total"] += 1
                text = (event.raw_text or "").strip()
                if is_hiring_post(text, require_tech):
                    seen["hits"] += 1
                    chat = await event.get_chat()
                    name = getattr(chat, "title", None) or getattr(chat, "first_name", "?")
                    print(f"  {GREEN}HIT{RESET} {_short(name, 30)}: {_short(text, 80)}")

            try:
                await asyncio.wait_for(
                    client.run_until_disconnected(), timeout=args.live
                )
            except (asyncio.TimeoutError, KeyboardInterrupt):
                pass
            ok(f"Live: saw {seen['total']} message(s), {seen['hits']} matched the filter")

    finally:
        await client.disconnect()

    header("Result")
    ok("Telegram user client is working.")
    print("\n  It is already wired into the pipeline. Next run picks it up:")
    print(f"    {BOLD}python main.py --dry-run{RESET}      full pipeline, sends nothing")
    print(f"    {BOLD}python main.py --live{RESET}         real-time mode\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Verify the Telegram user client and preview what it catches.",
    )
    p.add_argument("--dialogs", action="store_true", help="only list your chats")
    p.add_argument("--scan", type=int, default=48, metavar="HOURS",
                   help="how far back to scan for job posts (default 48)")
    p.add_argument("--live", type=int, default=0, metavar="SECONDS",
                   help="watch for new messages for N seconds")
    p.add_argument("--pipeline", action="store_true",
                   help="show the JobPost records the pipeline would receive")
    p.add_argument("--suggest", action="store_true",
                   help="rank your chats by hiring output and emit an "
                        "include_chats list for config.yml")
    p.add_argument("--limit", type=int, default=25,
                   help="max rows to print per section (default 25)")
    p.add_argument("-v", "--verbose", action="store_true", help="list every chat")
    args = p.parse_args()

    print(f"\n{BOLD}{'=' * 62}")
    print("  TELEGRAM USER CLIENT -- VERIFICATION")
    print(f"{'=' * 62}{RESET}")

    try:
        return asyncio.run(run_checks(args))
    except KeyboardInterrupt:
        print("\n  Interrupted.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
