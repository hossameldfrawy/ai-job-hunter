"""
The human-in-the-loop control plane: what a reply actually DOES.

`commands.py` turns "تعديل 1 الراتب: 15000" into a `Command`. This module is
what happens next -- resolve which draft it means, rewrite the record, run the
submission, and answer on both channels. It is split in two on purpose:

  ReviewController        pure orchestration over a `SecureStore`. No Telethon,
                          no event loop, no browser. Every branch -- approval,
                          edit, decline, ambiguity, refusal -- is reachable
                          from a plain function call, which is the only way a
                          flow that submits job applications in someone's name
                          can be tested honestly.

  TelegramCommandListener the transport. Reads Saved Messages, decides what is
                          even a candidate for execution, and hands the text to
                          the controller.

THE THREE GATES ON THE LISTENER
-------------------------------
Saved Messages is the user's private notebook, and this listener can submit a
job application. So a message is executed only if it clears all three:

  1. NOT OURS.   Every card this system emits carries `review.BOT_MARK`. Our
                 own review card contains the line 'done 7' as INSTRUCTIONS;
                 without this gate the listener would read its own card and
                 submit the application it had just asked about.
  2. RIGHT CHAT. Saved Messages only, and -- when `hitl.telegram_owner_id` is
                 configured -- from that account id only.
  3. RECENT.     A restart must not replay a "done" the user typed last week.

AMBIGUITY IS REFUSED, NOT GUESSED
---------------------------------
A bare "done" with several drafts pending resolves to nothing: the listener
lists them and asks for a number. Submitting an application cannot be undone,
so a one-in-three guess is not an acceptable default. A bare "done" with
exactly ONE pending draft does what it obviously means. Set
`hitl.confirm_when_ambiguous: false` to always take the newest instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from auto_apply import review
from auto_apply.commands import (
    ACTION_APPROVE, ACTION_DECLINE, ACTION_EDIT, ACTION_HELP, ACTION_STATUS,
    HELP_TEXT, Command, parse_command,
)
from auto_apply.review import BOT_MARK, DraftCard, draft_ref
from config import settings
from vault import (
    STATUS_DECLINED, STATUS_FAILED, STATUS_REVIEW, STATUS_SUBMITTED,
    SecureStore,
)

log = logging.getLogger(__name__)


def hitl_cfg() -> dict[str, Any]:
    return settings.raw.get("hitl", {}) or {}


def listener_enabled() -> bool:
    """Whether replies are read at all (`hitl.enabled` in config.yml).

    This gates the LISTENER, not the card. A drafted application is always
    pushed to both channels -- that is the review gate itself, and switching it
    off would mean drafting in silence. What this turns off is the ability to
    ACT by replying, for anyone who would rather keep approval in the terminal.
    """
    return bool(hitl_cfg().get("enabled", True))


@dataclass(slots=True)
class Reply:
    """What the controller did, and what it said about it."""

    handled: bool = False
    action: str = ""
    app_id: int | None = None
    ok: bool = False
    text: str = ""
    #: True when the controller sent a full review/outcome card itself, so the
    #: caller must not also echo `text` -- that is how a single approval turns
    #: into two confirmations.
    dispatched: bool = False
    detail: str = ""


HELP_SHORT = (
    f"{BOT_MARK}\U0001F916 *REVIEW COMMANDS*\n"
    "✅ *done 7* — submit draft #7\n"
    "✏️ *edit 7 salary: 15000*\n"
    "✏️ *edit 7 cover letter: <text>*\n"
    "❌ *decline 7*\n"
    "\U0001F4CB *status* — what is waiting\n"
    "بالعربي: موافق ٧ | "
    "تعديل ٧ الراتب: ١٥٠٠٠ | رفض ٧"
)


class ReviewController:
    """Executes review commands against the vault. No transport of its own."""

    def __init__(
        self, store: SecureStore, notifier: Any = None, *,
        submit_fn: Callable[..., bool] | None = None,
        approve_fn: Callable[..., bool] | None = None,
        decline_fn: Callable[..., bool] | None = None,
        revise_fn: Callable[..., str] | None = None,
    ) -> None:
        self.store = store
        self.notifier = notifier
        # Injected rather than imported at call time so the submission flow --
        # the one irreversible step in the system -- can be driven by a test
        # without a browser, and so a caller can substitute a dry-run.
        self._submit = submit_fn
        self._approve = approve_fn
        self._decline = decline_fn
        self._revise = revise_fn

    # -- lazily bound defaults ---------------------------------------------
    def _submit_fn(self) -> Callable[..., bool]:
        if self._submit is not None:
            return self._submit
        from auto_apply.engine import submit_application

        return submit_application

    def _approve_fn(self) -> Callable[..., bool]:
        if self._approve is not None:
            return self._approve
        from auto_apply.engine import approve

        return approve

    def _decline_fn(self) -> Callable[..., bool]:
        if self._decline is not None:
            return self._decline
        from auto_apply.engine import decline

        return decline

    def _revise_fn(self) -> Callable[..., str]:
        return self._revise if self._revise is not None else review.revise_cover_letter

    # -- entry point --------------------------------------------------------
    def handle(self, text: str) -> Reply:
        """Parse and execute one message. Never raises."""
        command = parse_command(text)
        if not command.recognised:
            return Reply(handled=False, action=command.action)
        return self.run(command)

    def run(self, command: Command) -> Reply:
        """Execute an ALREADY-PARSED command. Never raises.

        Split from `handle` so a caller that has to make decisions about the
        command before running it -- the listener's staleness gate does -- can
        parse once and act on what it learned, rather than parsing blind.
        """
        try:
            return self.execute(command)
        except Exception as exc:               # a listener must never die here
            log.exception("Review command failed: %s", exc)
            return self._say(
                Reply(True, command.action, command.draft_id, False,
                      detail=f"{type(exc).__name__}: {exc}"),
                f"{BOT_MARK}⚠️ That command could not be carried out: "
                f"{type(exc).__name__}: {str(exc)[:180]}",
            )

    def execute(self, command: Command) -> Reply:
        if command.action == ACTION_HELP:
            return self._help(command)
        if command.action == ACTION_STATUS:
            return self._status(command)
        if command.action == ACTION_APPROVE:
            return self._approve_and_submit(command)
        if command.action == ACTION_DECLINE:
            return self._decline_draft(command)
        if command.action == ACTION_EDIT:
            return self._edit(command)
        return Reply(handled=False, action=command.action)

    # -- shared plumbing ----------------------------------------------------
    def _say(self, reply: Reply, telegram: str, whatsapp: str | None = None) -> Reply:
        """Answer on both channels and record what was said."""
        reply.text = telegram
        if self.notifier is not None:
            review.dispatch_text(
                self.notifier, telegram,
                whatsapp if whatsapp is not None else telegram,
            )
        return reply

    def _pending(self) -> list[dict[str, Any]]:
        return self.store.applications_awaiting_review()

    def _resolve(self, command: Command, *, irreversible: bool
                 ) -> tuple[dict[str, Any] | None, str]:
        """Which draft does this command mean? ("", app) or (None, why not)."""
        if command.draft_id is not None:
            app = self.store.get_application(command.draft_id)
            if not app:
                return None, (
                    f"There is no draft #{command.draft_id}. "
                    f"Send *status* to see what is waiting."
                )
            return app, ""

        pending = self._pending()
        if not pending:
            return None, ("Nothing is waiting for review right now. "
                          "Run `python main.py --apply` to draft something.")

        ambiguous = irreversible and len(pending) > 1
        if ambiguous and bool(hitl_cfg().get("confirm_when_ambiguous", True)):
            listing = "\n".join(
                f"  {draft_ref(a['id'])} {str(a.get('company'))[:28]} — "
                f"{str(a.get('role'))[:34]}"
                for a in pending[:6]
            )
            return None, (
                f"{len(pending)} drafts are waiting, so I will not guess which "
                f"one you meant — submitting is not reversible.\n{listing}\n"
                f"Reply with the number, e.g. *done {pending[0]['id']}*."
            )
        return pending[0], ""

    # -- actions ------------------------------------------------------------
    def _help(self, command: Command) -> Reply:
        return self._say(Reply(True, ACTION_HELP, command.draft_id, True),
                         f"{BOT_MARK}{HELP_TEXT}", HELP_SHORT)

    def _status(self, command: Command) -> Reply:
        pending = self._pending()
        submitted = self.store.applications_by_status(STATUS_SUBMITTED)
        if not pending:
            body = "Nothing is waiting for review."
        else:
            body = "\n".join(
                f"{draft_ref(a['id'])} {a.get('status')} — "
                f"{str(a.get('company'))[:30]} / {str(a.get('role'))[:40]}"
                for a in pending[:10]
            )
        telegram = (
            f"{BOT_MARK}\U0001F4CB *APPLICATIONS AWAITING YOU*\n{body}\n\n"
            f"Submitted so far: {len(submitted)}\n"
            f"Reply *done <id>* to submit one, or *help* for the full syntax."
        )
        whatsapp = (
            f"{BOT_MARK}\U0001F4CB *AWAITING YOU:* {len(pending)}\n"
            + "\n".join(
                f"{draft_ref(a['id'])} {str(a.get('company'))[:24]}"
                for a in pending[:6]
            )
            + f"\n\U0001F4E4 Submitted: {len(submitted)}"
        )
        return self._say(Reply(True, ACTION_STATUS, None, True), telegram, whatsapp)

    def _approve_and_submit(self, command: Command) -> Reply:
        app, problem = self._resolve(command, irreversible=True)
        if problem:
            return self._say(Reply(True, ACTION_APPROVE, command.draft_id, False,
                                   detail=problem),
                             f"{BOT_MARK}\U0001F914 {problem}")
        assert app is not None
        app_id = int(app["id"])

        if app["status"] == STATUS_SUBMITTED:
            return self._say(
                Reply(True, ACTION_APPROVE, app_id, False,
                      detail="already submitted"),
                f"{BOT_MARK}✅ {draft_ref(app_id)} was already submitted"
                f" — {app.get('company')} / {app.get('role')}. Nothing to do.",
            )
        if app["status"] == STATUS_DECLINED:
            return self._say(
                Reply(True, ACTION_APPROVE, app_id, False, detail="declined"),
                f"{BOT_MARK}❌ {draft_ref(app_id)} was declined earlier, so it "
                f"will not be submitted. Re-draft it with "
                f"`python main.py --apply`.",
            )

        self._approve_fn()(app_id, self.store)
        log.info("HITL: approved #%d, submitting now.", app_id)

        # `submit_application` owns the outcome message on both channels --
        # success card with the screenshot, or the failure card with the link
        # to apply by hand. Echoing anything here would double it.
        try:
            ok = bool(self._submit_fn()(app_id, self.store, self.notifier,
                                        dry_run=settings.dry_run))
        except Exception as exc:
            # A PRE-CONDITION refusal: no confirmed application form, a CV the
            # form requires and we do not have, a platform that is never
            # automated. The engine raises these before it opens a browser, so
            # it has not recorded or announced anything -- and over this channel
            # there is no terminal to read the traceback in. Record it and say
            # what to do instead, or the user is left with an approval that
            # appeared to do nothing.
            return self._refused(app_id, app, exc)

        return Reply(True, ACTION_APPROVE, app_id, ok, dispatched=True,
                     detail="submitted" if ok else "submission failed",
                     text=f"{draft_ref(app_id)} "
                          f"{'submitted' if ok else 'FAILED — see the card'}")

    def _refused(self, app_id: int, app: dict[str, Any], exc: Exception) -> Reply:
        reason = str(exc) or type(exc).__name__
        log.error("HITL: submission of #%d was refused: %s", app_id, reason)
        self.store.set_application_status(
            app_id, STATUS_FAILED, failure_reason=reason[:300]
        )
        review.dispatch_failure(self.notifier, app_id, app, reason)
        return Reply(True, ACTION_APPROVE, app_id, False, dispatched=True,
                     detail=reason[:200],
                     text=f"{draft_ref(app_id)} refused: {reason[:120]}")

    def _decline_draft(self, command: Command) -> Reply:
        app, problem = self._resolve(command, irreversible=True)
        if problem:
            return self._say(Reply(True, ACTION_DECLINE, command.draft_id, False,
                                   detail=problem),
                             f"{BOT_MARK}\U0001F914 {problem}")
        assert app is not None
        app_id = int(app["id"])
        if app["status"] == STATUS_SUBMITTED:
            return self._say(
                Reply(True, ACTION_DECLINE, app_id, False,
                      detail="already submitted"),
                f"{BOT_MARK}⚠️ {draft_ref(app_id)} has already been submitted; "
                f"declining it now would change nothing.",
            )
        self._decline_fn()(app_id, self.store)
        return self._say(
            Reply(True, ACTION_DECLINE, app_id, True, detail="declined"),
            f"{BOT_MARK}❌ {draft_ref(app_id)} discarded — "
            f"{app.get('company')} / {app.get('role')}. It will not be submitted.",
        )

    # -- the in-line edit engine -------------------------------------------
    def _edit(self, command: Command) -> Reply:
        app, problem = self._resolve(command, irreversible=False)
        if problem:
            return self._say(Reply(True, ACTION_EDIT, command.draft_id, False,
                                   detail=problem),
                             f"{BOT_MARK}\U0001F914 {problem}")
        assert app is not None
        app_id = int(app["id"])

        if app["status"] == STATUS_SUBMITTED:
            return self._say(
                Reply(True, ACTION_EDIT, app_id, False, detail="already submitted"),
                f"{BOT_MARK}⚠️ {draft_ref(app_id)} has already been submitted, "
                f"so it can no longer be edited.",
            )
        if app["status"] == STATUS_DECLINED:
            return self._say(
                Reply(True, ACTION_EDIT, app_id, False, detail="declined"),
                f"{BOT_MARK}❌ {draft_ref(app_id)} was declined. Re-draft it "
                f"with `python main.py --apply` if you want it back.",
            )

        payload = review.payload_of(app)
        previous_cover = str(app.get("cover_letter_text") or "")
        field_name, value = command.field, command.value

        if not value:
            return self._say(
                Reply(True, ACTION_EDIT, app_id, False, detail="no value"),
                f"{BOT_MARK}✏️ Tell me what to change it to, e.g. "
                f"*edit {app_id} {field_name.replace('_', ' ')}: <new value>*",
            )

        note_only = ""
        if field_name == review.FIELD_NOTE:
            # A free-form instruction. Ask the model to apply it to the letter;
            # if that is switched off or fails, record it as a note rather than
            # dropping what the user asked for.
            revised = ""
            if bool(hitl_cfg().get("allow_ai_revision", True)) and previous_cover:
                revised = self._revise_fn()(
                    previous_cover, value, DraftCard.from_row(app)
                ) or ""
            if revised:
                # Keep the instruction alongside the rewrite: the card should
                # show WHY it changed, not just that it did.
                payload = review.apply_edit(payload, review.FIELD_NOTE, value).payload
                field_name, value = review.FIELD_COVER_LETTER, revised
            else:
                note_only = ("recorded as a note — name a field to change the "
                             "draft itself, e.g. *cover letter:* or *salary:*")

        result = review.apply_edit(
            payload, field_name, value,
            answer_index=command.answer_index, previous_cover=previous_cover,
        )
        if not result.ok:
            return self._say(
                Reply(True, ACTION_EDIT, app_id, False, detail=result.description),
                f"{BOT_MARK}✏️ {draft_ref(app_id)} unchanged — "
                f"{result.description}.",
            )

        # An edit ALWAYS returns the draft to review_pending. An application
        # that was already approved must not stay approved on the strength of
        # text the user has not seen since; the point of the edit is that the
        # next "done" confirms the version actually read.
        self.store.update_application_draft(
            app_id, payload=result.payload, cover_letter=result.cover_letter,
            status=STATUS_REVIEW,
        )

        fresh = self.store.get_application(app_id) or app
        card = DraftCard.from_row(fresh)
        review.dispatch_review(self.notifier, card)
        log.info("HITL: edited #%d (%s).", app_id, result.description)

        detail = result.description
        if note_only:
            detail = f"{detail}; {note_only}"
        elif result.warning:
            detail = f"{detail}; {result.warning}"
        return Reply(True, ACTION_EDIT, app_id, True, dispatched=True,
                     detail=detail,
                     text=f"{draft_ref(app_id)} updated: {detail}")


# ---------------------------------------------------------------------------
# Transport: Telegram Saved Messages
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ListenerStats:
    seen: int = 0
    skipped_own: int = 0
    skipped_stale: int = 0
    skipped_foreign: int = 0
    unrecognised: int = 0
    executed: int = 0
    errors: list[str] = field(default_factory=list)


class TelegramCommandListener:
    """Reads review commands out of your own Telegram Saved Messages.

    Two ways to run it, because the two deployments are different animals:

      LIVE  `run_forever()` holds an MTProto connection and reacts the instant
            you send a reply. Needs a process that stays alive -- your machine,
            a VPS, Docker.
      POLL  `poll_once()` reads what arrived since a stored cursor and exits.
            Works from cron or a scheduled task, and is the mode a test can
            drive end-to-end without an event loop.

    Both funnel into `handle_message`, so the gates are enforced once.
    """

    #: meta key holding the newest Saved-Messages id already processed
    CURSOR_KEY = "hitl:last_message_id"

    def __init__(self, controller: ReviewController, *, db: Any = None,
                 owner_id: int | None = None,
                 max_age_minutes: int | None = None) -> None:
        self.controller = controller
        self.db = db
        cfg = hitl_cfg()
        raw_owner = owner_id if owner_id is not None else cfg.get("telegram_owner_id")
        self.owner_id = int(raw_owner) if raw_owner not in (None, "", 0) else None
        self.max_age_minutes = int(
            max_age_minutes if max_age_minutes is not None
            else cfg.get("max_command_age_minutes", 180)
        )
        self.stats = ListenerStats()

    # -- gates --------------------------------------------------------------
    @staticmethod
    def is_own_card(text: str) -> bool:
        """True for anything this system emitted.

        Load-bearing. Our review card literally contains the line
        `✅ Approve & submit:  done 7`; without this the listener would read its
        own card out of Saved Messages and submit the application it had just
        asked the user about.
        """
        return BOT_MARK in (text or "")

    def is_fresh(self, when: Any) -> bool:
        """False for a message old enough that replaying it would be wrong."""
        if when is None or self.max_age_minutes <= 0:
            return True
        try:
            from datetime import datetime, timedelta, timezone

            stamp = when if isinstance(when, datetime) else None
            if stamp is None:
                return True
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - stamp
            return age <= timedelta(minutes=self.max_age_minutes)
        except Exception:
            return True

    def allows_sender(self, sender_id: Any) -> bool:
        """Only the configured account may drive this. Unset means no check."""
        if self.owner_id is None or sender_id is None:
            return True
        try:
            return int(sender_id) == self.owner_id
        except (TypeError, ValueError):
            return False

    # -- the funnel every mode goes through --------------------------------
    def handle_message(self, text: str, *, sender_id: Any = None,
                       when: Any = None, message_id: int = 0) -> Reply | None:
        """Apply the gates, then execute. None means "not for us".

        PARSING COMES BEFORE THE AGE GATE, deliberately. Parsing is pure and
        costs nothing, and doing it first is what lets every later decision --
        and every log line -- be about something real. With the gate first, a
        Saved Messages full of ordinary old notes produced a stream of
        "ignoring a review command older than 180 minutes", which is alarming
        and false: nothing had been parsed, so nothing was known to be a
        command. Measured on this account: 117 of 120 real messages are not
        commands at all.
        """
        self.stats.seen += 1
        # Whatever happens below, this message has now been INSPECTED. Marking
        # it so is what lets the event stream and the poll backstop run in the
        # same process without ever executing one command twice.
        self._advance_cursor(message_id)

        if self.is_own_card(text):
            self.stats.skipped_own += 1
            return None
        if not self.allows_sender(sender_id):
            self.stats.skipped_foreign += 1
            log.warning("Ignoring a message from sender %r.", sender_id)
            return None

        command = parse_command(text)
        if not command.recognised:
            self.stats.unrecognised += 1
            return None

        # Now it IS a command, so saying we are dropping one is accurate.
        if not self.is_fresh(when):
            self.stats.skipped_stale += 1
            log.info(
                "Ignoring a stale '%s' command (older than %d minutes) -- "
                "re-send it if you still mean it.",
                command.action, self.max_age_minutes,
            )
            return None

        reply = self.controller.run(command)
        self.stats.executed += 1
        if not reply.ok and reply.detail:
            self.stats.errors.append(reply.detail[:200])
        return reply

    # -- cursor -------------------------------------------------------------
    def _cursor(self) -> int:
        if not self.db:
            return 0
        try:
            return int(self.db.get_meta(self.CURSOR_KEY, "0") or 0)
        except (TypeError, ValueError):
            return 0

    def _save_cursor(self, message_id: int) -> None:
        if self.db and message_id:
            self.db.set_meta(self.CURSOR_KEY, str(message_id))

    def _advance_cursor(self, message_id: int) -> None:
        """Move the cursor forward only. Never backwards.

        Both modes call this, and the poll backstop reads messages the event
        stream may already have handled. Going backwards would re-run an
        approval that has already been executed.
        """
        try:
            message_id = int(message_id or 0)
        except (TypeError, ValueError):
            return
        if message_id > self._cursor():
            self._save_cursor(message_id)

    # -- POLL mode ----------------------------------------------------------
    async def poll_once_async(self, client: Any, limit: int = 40) -> int:
        """Read Saved Messages newer than the cursor. Returns commands executed.

        The cursor is advanced even for messages that were skipped, so a note
        the user wrote to themselves is inspected exactly once and a restart
        never re-runs an approval.
        """
        cursor = self._cursor()
        newest = cursor
        executed = 0
        batch: list[Any] = []
        async for message in client.iter_messages("me", limit=limit,
                                                  min_id=cursor):
            batch.append(message)
        import asyncio

        # iter_messages yields newest-first; execute in the order they were
        # written so "edit 7 salary: 1" then "done 7" cannot run backwards.
        for message in reversed(batch):
            newest = max(newest, int(getattr(message, "id", 0) or 0))
            # OFF the event loop, and awaited one at a time so the order above
            # is preserved. Executing a command inline here would block the
            # loop for as long as a Playwright submission takes -- and worse,
            # anything it called that used `asyncio.run` would raise, which is
            # exactly how every Telegram reply in this mode silently failed
            # while WhatsApp went out fine.
            reply = await asyncio.to_thread(
                self.handle_message,
                getattr(message, "message", "") or getattr(message, "text", "") or "",
                sender_id=getattr(message, "sender_id", None),
                when=getattr(message, "date", None),
                message_id=int(getattr(message, "id", 0) or 0),
            )
            if reply is not None:
                executed += 1
        self._save_cursor(newest)
        return executed

    def poll_once(self, limit: int = 40) -> int:
        """Blocking one-shot poll. Builds and tears down its own client."""
        import asyncio

        from scrapers.telegram_user_client import build_client, ensure_authorized

        async def _run() -> int:
            client = build_client()
            await ensure_authorized(client)
            try:
                return await self.poll_once_async(client, limit)
            finally:
                await client.disconnect()

        return asyncio.run(_run())

    # -- LIVE mode ----------------------------------------------------------
    async def run(self) -> None:
        import asyncio

        from telethon import events

        from scrapers.telegram_user_client import build_client, ensure_authorized

        client = build_client()
        await ensure_authorized(client)
        me = await client.get_me()
        my_id = int(getattr(me, "id", 0) or 0)
        if self.owner_id is None and my_id:
            # Nothing else can reach Saved Messages, but pinning the id makes
            # the gate explicit rather than incidental.
            self.owner_id = my_id
        log.info(
            "HITL listener attached to Telegram Saved Messages as %s (id %s). "
            "Reply 'done <id>' to submit a draft.",
            getattr(me, "username", None) or getattr(me, "first_name", "?"), my_id,
        )

        @client.on(events.NewMessage(chats="me"))
        async def handler(event: Any) -> None:      # noqa: ANN401
            try:
                text = (event.raw_text or "").strip()
                # The controller does database work and can launch a browser.
                # Off the event loop, or the listener stops consuming updates
                # for as long as a submission takes.
                await asyncio.to_thread(
                    self.handle_message, text,
                    sender_id=getattr(event, "sender_id", None),
                    when=getattr(event.message, "date", None),
                    message_id=int(getattr(event, "id", 0) or 0),
                )
            except Exception as exc:
                log.exception("HITL listener handler error: %s", exc)

        # A POLL BACKSTOP, running alongside the event stream.
        #
        # The event path is the fast one and is what makes a reply feel
        # instant. It is also the one this project could not verify from the
        # inside: Telegram does not deliver a NewMessage update for a message
        # the SAME session sent, so a self-test can never prove the handler is
        # wired up -- only a message typed on your phone can, and by then it is
        # too late to find out it was not. Silent inaction on an approval is
        # the worst failure this component has, so it does not rely on one
        # mechanism. The cursor is shared and forward-only, so whichever route
        # sees a message first is the only one that acts on it.
        async def backstop() -> None:
            interval = max(15, int(hitl_cfg().get("poll_interval_seconds", 60)))
            batch = int(hitl_cfg().get("poll_batch", 40))
            while True:
                await asyncio.sleep(interval)
                try:
                    executed = await self.poll_once_async(client, batch)
                    if executed:
                        log.info("Poll backstop picked up %d command(s) the "
                                 "event stream did not deliver.", executed)
                except Exception as exc:
                    log.warning("Poll backstop failed this cycle: %s: %s",
                                type(exc).__name__, exc)

        task = asyncio.ensure_future(backstop())
        try:
            await client.run_until_disconnected()
        finally:
            task.cancel()
            await client.disconnect()

    def run_forever(self) -> None:
        """Blocking entry point, with reconnect-on-drop."""
        import asyncio
        import time

        from scrapers.telegram_user_client import TelegramAuthError

        backoff = 5
        while True:
            try:
                asyncio.run(self.run())
                log.warning("HITL listener disconnected; reconnecting...")
            except TelegramAuthError as exc:
                log.error("%s", exc)
                return
            except KeyboardInterrupt:
                return
            except Exception as exc:
                log.error("HITL listener crashed: %s: %s", type(exc).__name__, exc)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


__all__ = [
    "ReviewController", "Reply", "TelegramCommandListener", "ListenerStats",
    "HELP_SHORT", "hitl_cfg", "listener_enabled",
]
