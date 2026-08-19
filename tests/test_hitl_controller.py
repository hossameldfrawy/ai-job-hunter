"""
What a reply actually DOES: the approval flow, the in-line edit engine, and
the three gates on the listener that reads them.

This is the file that guards an irreversible action. A message in Telegram
Saved Messages can cause a job application to be submitted in the user's name,
so the tests here are written around the ways that could go wrong rather than
around the happy path:

  * The bot must not obey its OWN review card, which literally contains the
    line "done 7" as instructions.
  * A bare "done" with three drafts pending must not pick one at random.
  * An edit must return the draft to review_pending, so the next "done"
    confirms the version the user actually read.
  * A restart must not replay last week's approval.
  * Nothing may reach the real Telegram or WhatsApp -- asserted by using a
    recorder and by conftest's transport stubs underneath it.

Run:  python -m pytest tests/test_hitl_controller.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_apply.control import (                                 # noqa: E402
    ListenerStats, ReviewController, TelegramCommandListener, hitl_cfg,
)
from auto_apply.review import BOT_MARK, format_review_telegram, DraftCard  # noqa: E402
from conftest import RecordingNotifier                           # noqa: E402
from config import settings                                      # noqa: E402
from vault import (                                              # noqa: E402
    STATUS_APPROVED, STATUS_DECLINED, STATUS_REVIEW, STATUS_SUBMITTED,
    SecureStore,
)

COVER = ("I have run Issabel and Asterisk PBX estates for three years, "
         "including SIP trunk migration.")

FIELD_MAP = [
    {"selector": "#cover", "kind": "cover_letter", "input_type": "textarea",
     "label": "Why do you want this role?", "required": False},
    {"selector": "#salary", "kind": "salary", "input_type": "text",
     "label": "Expected salary", "required": False},
    {"selector": "#sip", "kind": "unknown", "input_type": "text",
     "label": "Years with SIP?", "required": False},
]

DRAFT = {
    "cover_letter": COVER,
    "answers": [{"question": "Years with SIP?", "answer": "Three",
                 "confident": True}],
    "salary_expectation": "12,000 AED",
}


def _payload(**overrides):
    payload = {
        "fields": {"#cover": COVER, "#salary": "12,000 AED", "#sip": "Three"},
        "field_map": FIELD_MAP,
        "draft": json.loads(json.dumps(DRAFT)),
        "experience": "3 years",
        "match_score": 88,
        "form_ok": True,
        "form_note": "found cover_letter, salary",
    }
    payload.update(overrides)
    return payload


class ControllerHarness(unittest.TestCase):
    """A real vault on a temp path, a recording notifier, injected actions."""

    def setUp(self):
        self.store = SecureStore(Path(tempfile.mkdtemp()) / "vault.db")
        self.notifier = RecordingNotifier()
        self.submitted: list[tuple[int, bool]] = []
        self.declined: list[int] = []
        self.approved: list[int] = []
        self.submit_result = True
        self.revisions: list[tuple[str, str]] = []
        self.revision_result = ""

        def fake_submit(app_id, store, notifier, dry_run=False):
            self.submitted.append((app_id, dry_run))
            if self.submit_result:
                store.set_application_status(
                    app_id, STATUS_SUBMITTED,
                    screenshot_path=f"/shots/app{app_id}.png",
                )
            return self.submit_result

        def fake_approve(app_id, store):
            self.approved.append(app_id)
            store.set_application_status(app_id, STATUS_APPROVED)
            return True

        def fake_decline(app_id, store):
            self.declined.append(app_id)
            store.set_application_status(app_id, STATUS_DECLINED)
            return True

        def fake_revise(current, instruction, card=None):
            self.revisions.append((current, instruction))
            return self.revision_result

        self.controller = ReviewController(
            self.store, self.notifier, submit_fn=fake_submit,
            approve_fn=fake_approve, decline_fn=fake_decline,
            revise_fn=fake_revise,
        )

    def tearDown(self):
        self.store.close()

    def _draft(self, fingerprint="fp-1", company="Etisalat",
               role="VoIP Engineer", status=STATUS_REVIEW, **payload_kw):
        return self.store.record_application(
            job_fingerprint=fingerprint, job_id=101, company=company, role=role,
            platform="tanqeeb:uae",
            job_url="https://uae.tanqeeb.com/jobs/1.html",
            payload=_payload(**payload_kw), cover_letter=COVER, status=status,
        )


# ---------------------------------------------------------------------------
class TestApprovalFlow(ControllerHarness):
    def test_done_with_an_id_submits_that_draft(self):
        app_id = self._draft()
        reply = self.controller.handle(f"done {app_id}")
        self.assertTrue(reply.handled)
        self.assertTrue(reply.ok)
        self.assertEqual(self.approved, [app_id])
        self.assertEqual([a for a, _ in self.submitted], [app_id])
        self.assertEqual(self.store.get_application(app_id)["status"],
                         STATUS_SUBMITTED)

    def test_arabic_approval_works_identically(self):
        app_id = self._draft()
        reply = self.controller.handle(f"موافق {app_id}")
        self.assertTrue(reply.ok)
        self.assertEqual([a for a, _ in self.submitted], [app_id])

    def test_arabic_indic_digits_reach_the_right_draft(self):
        first = self._draft(fingerprint="fp-a")
        second = self._draft(fingerprint="fp-b")
        self.assertEqual((first, second), (1, 2))
        self.controller.handle("اعتمد ٢")
        self.assertEqual([a for a, _ in self.submitted], [2])

    def test_a_bare_done_submits_the_only_pending_draft(self):
        app_id = self._draft()
        self.controller.handle("done")
        self.assertEqual([a for a, _ in self.submitted], [app_id])

    def test_a_bare_done_refuses_to_guess_between_several(self):
        """Submitting cannot be undone, so a one-in-three guess is not a default."""
        self._draft(fingerprint="fp-a", company="Etisalat")
        self._draft(fingerprint="fp-b", company="Vodafone")
        self._draft(fingerprint="fp-c", company="Orange")
        reply = self.controller.handle("done")
        self.assertTrue(reply.handled)
        self.assertFalse(reply.ok)
        self.assertEqual(self.submitted, [], "an application was submitted on "
                                             "a guess")
        self.assertIn("3 drafts are waiting", reply.text)
        for company in ("Etisalat", "Vodafone", "Orange"):
            self.assertIn(company, reply.text)

    def test_the_ambiguity_guard_can_be_switched_off(self):
        self._draft(fingerprint="fp-a")
        newest = self._draft(fingerprint="fp-b")
        original = dict(hitl_cfg())
        settings.raw["hitl"] = dict(original, confirm_when_ambiguous=False)
        try:
            self.controller.handle("done")
        finally:
            settings.raw["hitl"] = original
        self.assertEqual([a for a, _ in self.submitted], [newest],
                         "with the guard off, the newest draft should win")

    def test_an_unknown_id_is_reported_not_silently_dropped(self):
        reply = self.controller.handle("done 99")
        self.assertTrue(reply.handled)
        self.assertFalse(reply.ok)
        self.assertIn("no draft #99", reply.text.lower())
        self.assertEqual(self.submitted, [])

    def test_nothing_pending_says_so(self):
        reply = self.controller.handle("done")
        self.assertFalse(reply.ok)
        self.assertIn("Nothing is waiting", reply.text)

    def test_an_already_submitted_draft_is_not_resubmitted(self):
        app_id = self._draft(status=STATUS_SUBMITTED)
        reply = self.controller.handle(f"done {app_id}")
        self.assertFalse(reply.ok)
        self.assertEqual(self.submitted, [])
        self.assertIn("already submitted", reply.text)

    def test_a_declined_draft_cannot_be_revived_by_approving_it(self):
        app_id = self._draft(status=STATUS_DECLINED)
        reply = self.controller.handle(f"done {app_id}")
        self.assertFalse(reply.ok)
        self.assertEqual(self.submitted, [])
        self.assertIn("declined", reply.text)

    def test_a_failed_submission_is_reported_as_failed(self):
        self.submit_result = False
        app_id = self._draft()
        reply = self.controller.handle(f"done {app_id}")
        self.assertTrue(reply.handled)
        self.assertFalse(reply.ok)
        self.assertTrue(reply.dispatched,
                        "submit_application owns the failure card")

    def test_the_controller_does_not_double_announce_the_outcome(self):
        """`submit_application` sends the confirmation. A second one here would
        put two 'submitted' cards in front of the user for one action."""
        app_id = self._draft()
        before = len(self.notifier.telegram)
        self.controller.handle(f"done {app_id}")
        self.assertEqual(len(self.notifier.telegram), before,
                         "the controller echoed an outcome the engine already "
                         "sent")

    def test_dry_run_is_passed_through_to_the_submitter(self):
        app_id = self._draft()
        self.controller.handle(f"done {app_id}")
        self.assertEqual(self.submitted[0][1], settings.dry_run)


# ---------------------------------------------------------------------------
class TestDeclineFlow(ControllerHarness):
    def test_decline_marks_the_draft_and_answers_on_both_channels(self):
        app_id = self._draft()
        reply = self.controller.handle(f"decline {app_id}")
        self.assertTrue(reply.ok)
        self.assertEqual(self.declined, [app_id])
        self.assertEqual(self.store.get_application(app_id)["status"],
                         STATUS_DECLINED)
        self.assertIn("discarded", self.notifier.last_telegram)
        self.assertIn("discarded", self.notifier.last_whatsapp)

    def test_arabic_decline(self):
        app_id = self._draft()
        self.controller.handle(f"رفض {app_id}")
        self.assertEqual(self.declined, [app_id])

    def test_declining_a_submitted_application_changes_nothing(self):
        app_id = self._draft(status=STATUS_SUBMITTED)
        reply = self.controller.handle(f"decline {app_id}")
        self.assertFalse(reply.ok)
        self.assertEqual(self.declined, [])

    def test_declining_is_ambiguity_guarded_too(self):
        self._draft(fingerprint="fp-a")
        self._draft(fingerprint="fp-b")
        reply = self.controller.handle("decline")
        self.assertFalse(reply.ok)
        self.assertEqual(self.declined, [])


# ---------------------------------------------------------------------------
class TestEditFlow(ControllerHarness):
    def test_a_cover_letter_edit_changes_what_will_be_submitted(self):
        app_id = self._draft()
        reply = self.controller.handle(
            f"edit {app_id} cover letter: A completely new letter."
        )
        self.assertTrue(reply.ok)
        app = self.store.get_application(app_id)
        self.assertEqual(app["cover_letter_text"], "A completely new letter.")
        stored = json.loads(app["submitted_payload_json"])
        self.assertEqual(
            stored["fields"]["#cover"], "A completely new letter.",
            "the form payload still holds the old letter -- the edit is "
            "cosmetic and the ORIGINAL would be submitted",
        )

    def test_an_arabic_salary_edit_lands_in_both_halves(self):
        app_id = self._draft()
        reply = self.controller.handle(f"تعديل {app_id} الراتب: 18000 درهم")
        self.assertTrue(reply.ok)
        stored = json.loads(
            self.store.get_application(app_id)["submitted_payload_json"]
        )
        self.assertEqual(stored["draft"]["salary_expectation"], "18000 درهم")
        self.assertEqual(stored["fields"]["#salary"], "18000 درهم")

    def test_an_edit_re_dispatches_the_card_to_both_channels(self):
        app_id = self._draft()
        self.notifier.clear()
        self.controller.handle(f"edit {app_id} salary: 20000")
        self.assertEqual(len(self.notifier.telegram), 1)
        self.assertEqual(len(self.notifier.whatsapp), 1)
        self.assertIn("20000", self.notifier.last_telegram)
        self.assertIn("20000", self.notifier.last_whatsapp)
        self.assertIn(f"done {app_id}", self.notifier.last_telegram,
                      "the updated card must still say how to approve it")

    def test_an_edit_returns_an_approved_draft_to_review_pending(self):
        """The next "done" must confirm the version the user actually read."""
        app_id = self._draft(status=STATUS_APPROVED)
        self.controller.handle(f"edit {app_id} salary: 20000")
        self.assertEqual(self.store.get_application(app_id)["status"],
                         STATUS_REVIEW)

    def test_each_edit_bumps_the_revision_counter(self):
        app_id = self._draft()
        self.controller.handle(f"edit {app_id} salary: 1")
        self.controller.handle(f"edit {app_id} salary: 2")
        self.assertEqual(self.store.get_application(app_id)["revision"], 2)
        self.assertIn("2 edit(s) applied", self.notifier.last_telegram)

    def test_an_answer_edit_by_number(self):
        app_id = self._draft()
        reply = self.controller.handle(f"edit {app_id} answer 1: Five years")
        self.assertTrue(reply.ok)
        stored = json.loads(
            self.store.get_application(app_id)["submitted_payload_json"]
        )
        self.assertEqual(stored["draft"]["answers"][0]["answer"], "Five years")
        self.assertEqual(stored["fields"]["#sip"], "Five years")

    def test_edit_then_done_submits_the_edited_version(self):
        """The whole point of the loop."""
        app_id = self._draft()
        self.controller.handle(f"edit {app_id} cover letter: Version two.")
        self.controller.handle(f"done {app_id}")
        self.assertEqual([a for a, _ in self.submitted], [app_id])
        app = self.store.get_application(app_id)
        self.assertEqual(app["status"], STATUS_SUBMITTED)
        self.assertEqual(app["cover_letter_text"], "Version two.")

    def test_a_bare_edit_uses_the_latest_pending_draft(self):
        self._draft(fingerprint="fp-a")
        newest = self._draft(fingerprint="fp-b")
        self.controller.handle("edit salary: 30000")
        stored = json.loads(
            self.store.get_application(newest)["submitted_payload_json"]
        )
        self.assertEqual(stored["fields"]["#salary"], "30000")

    def test_editing_a_submitted_application_is_refused(self):
        app_id = self._draft(status=STATUS_SUBMITTED)
        reply = self.controller.handle(f"edit {app_id} salary: 1")
        self.assertFalse(reply.ok)
        self.assertIn("already been submitted", reply.text)
        self.assertEqual(self.store.get_application(app_id)["revision"], 0)

    def test_editing_a_declined_draft_is_refused(self):
        app_id = self._draft(status=STATUS_DECLINED)
        reply = self.controller.handle(f"edit {app_id} salary: 1")
        self.assertFalse(reply.ok)
        self.assertIn("declined", reply.text)

    def test_naming_a_field_with_no_value_asks_for_one(self):
        app_id = self._draft()
        reply = self.controller.handle(f"edit {app_id} salary")
        self.assertFalse(reply.ok)
        self.assertIn("Tell me what to change it to", reply.text)
        self.assertEqual(self.store.get_application(app_id)["revision"], 0)

    def test_a_field_the_form_lacks_is_stored_with_an_honest_warning(self):
        app_id = self._draft(field_map=[
            f for f in FIELD_MAP if f["kind"] != "salary"
        ])
        reply = self.controller.handle(f"edit {app_id} salary: 25000")
        self.assertTrue(reply.ok)
        self.assertIn("no salary input", reply.detail)


# ---------------------------------------------------------------------------
class TestFreeFormEdit(ControllerHarness):
    def test_a_free_form_instruction_is_applied_by_the_model(self):
        self.revision_result = "A shorter, warmer letter mentioning Asterisk."
        app_id = self._draft()
        reply = self.controller.handle(f"edit {app_id}: make it shorter")
        self.assertTrue(reply.ok)
        self.assertEqual(self.revisions[0][1], "make it shorter")
        app = self.store.get_application(app_id)
        self.assertEqual(app["cover_letter_text"], self.revision_result)
        stored = json.loads(app["submitted_payload_json"])
        self.assertEqual(stored["fields"]["#cover"], self.revision_result)
        self.assertEqual(stored["notes"], ["make it shorter"],
                         "the card should show WHY it changed, not just that "
                         "it did")

    def test_arabic_free_form_instruction(self):
        self.revision_result = "نص جديد"
        app_id = self._draft()
        reply = self.controller.handle("تعديل: اجعله اقصر")
        self.assertTrue(reply.ok)
        self.assertEqual(self.revisions[0][1], "اجعله اقصر")
        self.assertEqual(
            self.store.get_application(app_id)["cover_letter_text"], "نص جديد"
        )

    def test_a_failed_revision_degrades_to_a_recorded_note(self):
        """A model outage must not lose what the user asked for."""
        self.revision_result = ""            # the model could not answer
        app_id = self._draft()
        reply = self.controller.handle(f"edit {app_id}: make it shorter")
        self.assertTrue(reply.ok)
        app = self.store.get_application(app_id)
        self.assertEqual(app["cover_letter_text"], COVER,
                         "the letter must be left alone, not blanked")
        self.assertEqual(json.loads(app["submitted_payload_json"])["notes"],
                         ["make it shorter"])
        self.assertIn("recorded as a note", reply.detail)

    def test_ai_revision_can_be_switched_off(self):
        self.revision_result = "should never be used"
        app_id = self._draft()
        original = dict(hitl_cfg())
        settings.raw["hitl"] = dict(original, allow_ai_revision=False)
        try:
            reply = self.controller.handle(f"edit {app_id}: make it shorter")
        finally:
            settings.raw["hitl"] = original
        self.assertTrue(reply.ok)
        self.assertEqual(self.revisions, [], "the model was called anyway")
        self.assertEqual(
            self.store.get_application(app_id)["cover_letter_text"], COVER
        )


# ---------------------------------------------------------------------------
class TestStatusAndHelp(ControllerHarness):
    def test_status_lists_what_is_waiting_on_both_channels(self):
        self._draft(fingerprint="fp-a", company="Etisalat")
        self._draft(fingerprint="fp-b", company="Vodafone")
        self._draft(fingerprint="fp-c", status=STATUS_SUBMITTED)
        reply = self.controller.handle("status")
        self.assertTrue(reply.ok)
        self.assertIn("Etisalat", self.notifier.last_telegram)
        self.assertIn("Vodafone", self.notifier.last_telegram)
        self.assertIn("Submitted so far: 1", self.notifier.last_telegram)
        self.assertIn("[DRAFT #1]", self.notifier.last_whatsapp)

    def test_status_with_nothing_pending(self):
        reply = self.controller.handle("الحالة")
        self.assertTrue(reply.ok)
        self.assertIn("Nothing is waiting", self.notifier.last_telegram)

    def test_help_answers_on_both_channels_and_fits_whatsapp(self):
        from urllib.parse import quote

        from notifier import MAX_URL_LENGTH, _URL_OVERHEAD

        self.controller.handle("help")
        self.assertIn("done", self.notifier.last_telegram)
        short = self.notifier.last_whatsapp
        self.assertLessEqual(len(quote(short)) + _URL_OVERHEAD, MAX_URL_LENGTH,
                             "the WhatsApp help card would be dropped whole")

    def test_arabic_help(self):
        self.controller.handle("مساعدة")
        self.assertIn("تعديل", self.notifier.last_telegram)


# ---------------------------------------------------------------------------
class TestControllerRobustness(ControllerHarness):
    def test_an_unrecognised_message_is_ignored_silently(self):
        """Saved Messages is a notebook. Answering every note would be noise."""
        self._draft()
        reply = self.controller.handle("remember to renew the passport")
        self.assertFalse(reply.handled)
        self.assertEqual(self.notifier.telegram, [])
        self.assertEqual(self.submitted, [])

    def test_a_refused_submission_gets_a_real_failure_card_on_both_channels(self):
        """The engine raises pre-condition refusals (no application form on the
        page, a required CV that is missing) BEFORE opening a browser, so it has
        neither recorded nor announced anything. Over this channel there is no
        terminal to read the traceback in -- an approval that appears to do
        nothing is the worst possible outcome."""
        def exploding_submit(*args, **kwargs):
            raise RuntimeError("playwright is not installed")

        controller = ReviewController(
            self.store, self.notifier, submit_fn=exploding_submit,
            approve_fn=lambda app_id, store: True,
        )
        app_id = self._draft()
        reply = controller.handle(f"done {app_id}")
        self.assertTrue(reply.handled)
        self.assertFalse(reply.ok)
        self.assertIn("APPLICATION FAILED", self.notifier.last_telegram)
        self.assertIn("APPLICATION FAILED", self.notifier.last_whatsapp)
        self.assertIn("playwright", self.notifier.last_telegram)
        self.assertIn("Apply by hand", self.notifier.last_telegram)

        app = self.store.get_application(app_id)
        self.assertEqual(app["status"], "failed")
        self.assertIn("playwright", app["failure_reason"])
        self.assertTrue(app["cover_letter_text"],
                        "the drafted letter must survive a refusal")

    def test_a_crash_outside_the_submit_path_is_reported_not_propagated(self):
        """A listener that dies on one bad command stops answering entirely."""
        def exploding_approve(app_id, store):
            raise RuntimeError("the vault is locked")

        controller = ReviewController(
            self.store, self.notifier, submit_fn=lambda *a, **k: True,
            approve_fn=exploding_approve,
        )
        self._draft()
        reply = controller.handle("done 1")
        self.assertTrue(reply.handled)
        self.assertFalse(reply.ok)
        self.assertIn("could not be carried out", self.notifier.last_telegram)
        self.assertIn("vault is locked", self.notifier.last_telegram)

    def test_a_controller_with_no_notifier_still_acts(self):
        controller = ReviewController(
            self.store, None, submit_fn=lambda *a, **k: True,
            approve_fn=lambda app_id, store: True,
        )
        app_id = self._draft()
        self.assertTrue(controller.handle(f"done {app_id}").ok)

    def test_the_listener_switch_is_read_from_config(self):
        """`hitl.enabled` gates the LISTENER, not the card -- drafting in
        silence would remove the review gate rather than relocate it."""
        from auto_apply.control import listener_enabled

        original = dict(hitl_cfg())
        try:
            self.assertTrue(listener_enabled())
            settings.raw["hitl"] = dict(original, enabled=False)
            self.assertFalse(listener_enabled())
        finally:
            settings.raw["hitl"] = original

    def test_the_default_actions_resolve_to_the_engine(self):
        """The injected doubles must not be hiding a broken default wiring."""
        from auto_apply import engine

        controller = ReviewController(self.store, self.notifier)
        self.assertIs(controller._submit_fn(), engine.submit_application)
        self.assertIs(controller._approve_fn(), engine.approve)
        self.assertIs(controller._decline_fn(), engine.decline)


# ---------------------------------------------------------------------------
class TestListenerGates(ControllerHarness):
    def setUp(self):
        super().setUp()
        self.listener = TelegramCommandListener(
            self.controller, owner_id=1443640499, max_age_minutes=180
        )

    def test_the_listener_never_obeys_its_own_review_card(self):
        """The card contains the line "done 7" as INSTRUCTIONS.

        Without the marker gate the listener reads its own card out of Saved
        Messages and submits the application it had just asked about.
        """
        app_id = self._draft()
        card = format_review_telegram(
            DraftCard.from_row(self.store.get_application(app_id))
        )
        self.assertIn(f"done {app_id}", card, "precondition: the card says it")
        self.assertIsNone(self.listener.handle_message(card))
        self.assertEqual(self.submitted, [])
        self.assertEqual(self.listener.stats.skipped_own, 1)

    def test_every_emitted_card_shape_is_skipped(self):
        for text in (BOT_MARK + "anything at all",
                     "prefix " + BOT_MARK + " done 1"):
            with self.subTest(text=text[:20]):
                self.assertTrue(self.listener.is_own_card(text))
        self.assertFalse(self.listener.is_own_card("done 1"))

    def test_a_foreign_sender_is_refused(self):
        app_id = self._draft()
        self.assertIsNone(self.listener.handle_message(
            f"done {app_id}", sender_id=999
        ))
        self.assertEqual(self.submitted, [])
        self.assertEqual(self.listener.stats.skipped_foreign, 1)

    def test_the_configured_owner_is_accepted(self):
        app_id = self._draft()
        reply = self.listener.handle_message(f"done {app_id}",
                                             sender_id=1443640499)
        self.assertIsNotNone(reply)
        self.assertEqual([a for a, _ in self.submitted], [app_id])

    def test_the_owner_defaults_to_config_when_not_passed(self):
        """`owner_id=None` means "read config.yml", not "accept anyone"."""
        listener = TelegramCommandListener(self.controller)
        self.assertEqual(listener.owner_id,
                         int(hitl_cfg()["telegram_owner_id"]))

    def test_no_owner_configured_anywhere_means_no_sender_check(self):
        original = dict(hitl_cfg())
        settings.raw["hitl"] = {k: v for k, v in original.items()
                                if k != "telegram_owner_id"}
        try:
            listener = TelegramCommandListener(self.controller,
                                               max_age_minutes=0)
            self.assertIsNone(listener.owner_id)
            app_id = self._draft()
            self.assertIsNotNone(listener.handle_message(f"done {app_id}",
                                                         sender_id=12345))
        finally:
            settings.raw["hitl"] = original

    def test_a_blank_owner_id_in_config_disables_the_check(self):
        original = dict(hitl_cfg())
        settings.raw["hitl"] = dict(original, telegram_owner_id="")
        try:
            self.assertIsNone(
                TelegramCommandListener(self.controller).owner_id
            )
        finally:
            settings.raw["hitl"] = original

    def test_a_missing_sender_id_is_not_treated_as_a_stranger(self):
        """Telethon omits sender_id on some Saved-Messages shapes; a card the
        user typed must not be refused because of it."""
        app_id = self._draft()
        self.assertIsNotNone(self.listener.handle_message(f"done {app_id}",
                                                          sender_id=None))

    def test_a_non_numeric_sender_id_is_refused_rather_than_crashing(self):
        app_id = self._draft()
        self.assertIsNone(self.listener.handle_message(f"done {app_id}",
                                                        sender_id="nobody"))
        self.assertEqual(self.submitted, [])

    def test_a_stale_command_is_not_replayed_on_restart(self):
        """Reconnecting must not re-run last week's approval."""
        app_id = self._draft()
        old = datetime.now(timezone.utc) - timedelta(days=7)
        self.assertIsNone(self.listener.handle_message(
            f"done {app_id}", sender_id=1443640499, when=old
        ))
        self.assertEqual(self.submitted, [])
        self.assertEqual(self.listener.stats.skipped_stale, 1)

    def test_a_recent_command_passes_the_age_gate(self):
        app_id = self._draft()
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        self.assertIsNotNone(self.listener.handle_message(
            f"done {app_id}", sender_id=1443640499, when=recent
        ))

    def test_a_naive_timestamp_is_treated_as_utc_not_rejected(self):
        app_id = self._draft()
        naive = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertIsNotNone(self.listener.handle_message(
            f"done {app_id}", sender_id=1443640499, when=naive
        ))

    def test_an_unparseable_timestamp_does_not_block_the_command(self):
        app_id = self._draft()
        self.assertIsNotNone(self.listener.handle_message(
            f"done {app_id}", sender_id=1443640499, when="not a date"
        ))

    def test_age_gate_disabled_by_zero(self):
        listener = TelegramCommandListener(self.controller, owner_id=None,
                                           max_age_minutes=0)
        app_id = self._draft()
        ancient = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.assertIsNotNone(listener.handle_message(f"done {app_id}",
                                                     when=ancient))

    def test_an_old_note_is_not_reported_as_an_ignored_command(self):
        """Regression: the age gate ran BEFORE parsing, so every ordinary old
        note logged "ignoring a review command older than 180 minutes" --
        alarming, and false. Measured on the real account: 117 of 120 messages
        are not commands at all."""
        old = datetime.now(timezone.utc) - timedelta(days=7)
        with self.assertNoLogs("auto_apply.control", level="INFO"):
            self.assertIsNone(self.listener.handle_message(
                "remember to renew the passport",
                sender_id=1443640499, when=old,
            ))
        self.assertEqual(self.listener.stats.unrecognised, 1)
        self.assertEqual(self.listener.stats.skipped_stale, 0)

    def test_a_genuinely_stale_command_still_says_so(self):
        app_id = self._draft()
        old = datetime.now(timezone.utc) - timedelta(days=7)
        with self.assertLogs("auto_apply.control", level="INFO") as captured:
            self.assertIsNone(self.listener.handle_message(
                f"done {app_id}", sender_id=1443640499, when=old,
            ))
        joined = "\n".join(captured.output)
        self.assertIn("stale", joined)
        self.assertIn("approve", joined, "the log should name what was dropped")
        self.assertEqual(self.submitted, [])

    def test_ordinary_notes_are_counted_but_not_acted_on(self):
        self._draft()
        self.assertIsNone(self.listener.handle_message("buy milk"))
        self.assertEqual(self.listener.stats.unrecognised, 1)
        self.assertEqual(self.listener.stats.executed, 0)

    def test_stats_track_what_happened(self):
        app_id = self._draft()
        self.listener.handle_message(BOT_MARK + "card")
        self.listener.handle_message("note to self")
        self.listener.handle_message(f"done {app_id}")
        stats = self.listener.stats
        self.assertEqual(
            (stats.seen, stats.skipped_own, stats.unrecognised, stats.executed),
            (3, 1, 1, 1),
        )
        self.assertIsInstance(ListenerStats(), ListenerStats)


# ---------------------------------------------------------------------------
class FakeMessage:
    def __init__(self, message_id, text, sender_id=1443640499, when=None):
        self.id = message_id
        self.message = text
        self.sender_id = sender_id
        self.date = when or datetime.now(timezone.utc)


class FakeTelethonClient:
    """Enough of the Telethon surface for the poll path."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.queries: list[dict] = []

    def iter_messages(self, entity, limit=40, min_id=0):
        self.queries.append({"entity": entity, "limit": limit,
                             "min_id": min_id})
        newest_first = sorted(
            (m for m in self.messages if m.id > min_id),
            key=lambda m: m.id, reverse=True,
        )[:limit]

        async def _gen():
            for message in newest_first:
                yield message

        return _gen()


class TestPollMode(ControllerHarness):
    """Cursor-based polling: the mode a cron job (or a test) can drive."""

    def setUp(self):
        super().setUp()
        from db import Database

        self.db = Database(Path(tempfile.mkdtemp()) / "poll.db")
        self.listener = TelegramCommandListener(
            self.controller, db=self.db, owner_id=None, max_age_minutes=0
        )

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def _poll(self, client):
        import asyncio

        return asyncio.run(self.listener.poll_once_async(client))

    def test_commands_run_in_the_order_they_were_written(self):
        """iter_messages yields newest-first; running that order would apply
        "done" before the edit it was confirming."""
        app_id = self._draft()
        client = FakeTelethonClient([
            FakeMessage(10, f"edit {app_id} cover letter: Version two."),
            FakeMessage(11, f"done {app_id}"),
        ])
        self.assertEqual(self._poll(client), 2)
        app = self.store.get_application(app_id)
        self.assertEqual(app["status"], STATUS_SUBMITTED)
        self.assertEqual(app["cover_letter_text"], "Version two.")

    def test_the_cursor_stops_a_command_running_twice(self):
        app_id = self._draft()
        client = FakeTelethonClient([FakeMessage(10, f"done {app_id}")])
        self.assertEqual(self._poll(client), 1)
        self.assertEqual(self._poll(client), 0, "the approval ran twice")
        self.assertEqual([a for a, _ in self.submitted], [app_id])

    def test_the_cursor_advances_past_ignored_notes_too(self):
        """A note the user wrote to themselves is inspected exactly once."""
        client = FakeTelethonClient([
            FakeMessage(10, "buy milk"), FakeMessage(11, "call mum"),
        ])
        self._poll(client)
        self.assertEqual(int(self.db.get_meta(self.listener.CURSOR_KEY)), 11)
        self.assertEqual(self.listener.stats.seen, 2)
        self._poll(client)
        self.assertEqual(self.listener.stats.seen, 2, "re-inspected old notes")

    def test_the_cursor_is_passed_to_telegram_as_min_id(self):
        client = FakeTelethonClient([FakeMessage(10, "status")])
        self._poll(client)
        self.assertEqual(client.queries[0]["min_id"], 0)
        self._poll(client)
        self.assertEqual(client.queries[1]["min_id"], 10)

    def test_polling_without_a_database_still_works(self):
        """No cursor storage: every poll re-reads, which is correct for a
        one-shot run and must not crash."""
        listener = TelegramCommandListener(self.controller, db=None,
                                           owner_id=None, max_age_minutes=0)
        app_id = self._draft()
        import asyncio

        client = FakeTelethonClient([FakeMessage(10, "status")])
        self.assertEqual(asyncio.run(listener.poll_once_async(client)), 1)
        self.assertEqual(app_id, 1)

    def test_a_command_the_event_stream_already_ran_is_not_re_run(self):
        """Live mode runs an event handler AND a poll backstop in one process.

        The backstop exists because Telegram never delivers a NewMessage update
        for a message the SAME session sent, so the event path cannot be
        self-tested -- and silent inaction on an approval is this component's
        worst failure. Two routes only work if they share one forward-only
        cursor.
        """
        app_id = self._draft()
        # The event stream sees it first.
        self.listener.handle_message(f"done {app_id}", message_id=10)
        self.assertEqual([a for a, _ in self.submitted], [app_id])

        # The backstop then sweeps the same window.
        client = FakeTelethonClient([FakeMessage(10, f"done {app_id}")])
        self.assertEqual(self._poll(client), 0, "the approval ran twice")
        self.assertEqual([a for a, _ in self.submitted], [app_id])

    def test_the_cursor_only_ever_moves_forward(self):
        self.listener.handle_message("note", message_id=50)
        self.assertEqual(self.listener._cursor(), 50)
        self.listener.handle_message("note", message_id=20)
        self.assertEqual(self.listener._cursor(), 50,
                         "an older message wound the cursor back")

    def test_a_skipped_message_still_advances_the_cursor(self):
        """An own-card or a stale command must not be re-inspected forever."""
        self.listener.handle_message(BOT_MARK + "our own card", message_id=31)
        self.assertEqual(self.listener._cursor(), 31)

    def test_a_missing_or_junk_message_id_is_survivable(self):
        for bad in (0, None, "not a number"):
            with self.subTest(message_id=bad):
                self.assertIsNone(
                    self.listener.handle_message("note", message_id=bad)
                )

    def test_an_empty_mailbox_is_a_clean_pass(self):
        self.assertEqual(self._poll(FakeTelethonClient([])), 0)
        self.assertEqual(self.listener.stats.seen, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
