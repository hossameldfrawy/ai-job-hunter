"""
The review card on both channels, and the in-line edit engine underneath it.

Three properties are load-bearing here, and each has cost a real failure mode
somewhere in this codebase already:

  1. THE WHATSAPP CARD MUST FIT. CallMeBot carries the message percent-encoded
     in a query string and DROPS what overflows rather than truncating. Arabic
     costs ~5.6 URL characters per character, so a card that measures fine in
     characters can be double the URL limit. An over-budget review card is not
     a truncated card -- it is a silently missing one.

  2. BOTH CARDS MUST QUOTE THE SAME REFERENCE. "done 7" resolves against the
     printed id. A reference that differs between channels is a reference that
     submits the wrong application.

  3. AN EDIT MUST CHANGE WHAT IS SUBMITTED, not just what is displayed. The
     card reads `cover_letter_text`; the browser types
     `submitted_payload_json["fields"]`. Updating one without the other
     produces a draft that looks edited and submits the original.

Run:  python -m pytest tests/test_hitl_review.py -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_apply.review import (                                  # noqa: E402
    BOT_MARK, DraftCard, EditResult, FIELD_ANSWER, FIELD_COVER_LETTER,
    FIELD_EXPERIENCE, FIELD_NOTE, FIELD_SALARY, apply_edit, dispatch_failure,
    dispatch_review, dispatch_submitted, draft_ref, enabled_channels,
    field_map,
    format_failure_telegram, format_failure_whatsapp, format_review_telegram,
    format_review_whatsapp, format_submitted_telegram, format_submitted_whatsapp,
    payload_of, revise_cover_letter,
)
from conftest import RecordingNotifier                           # noqa: E402
from models import Evaluation                                    # noqa: E402
from notifier import MAX_URL_LENGTH, _URL_OVERHEAD               # noqa: E402


def _encoded_len(text: str) -> int:
    """What CallMeBot actually measures: the percent-encoded URL length."""
    return len(quote(text)) + _URL_OVERHEAD


DRAFT = {
    "cover_letter": "I have run Issabel and Asterisk PBX estates for three "
                    "years, including SIP trunk migration and IVR design.",
    "answers": [
        {"question": "Years with SIP?", "answer": "Three", "confident": True},
        {"question": "Expected salary?", "answer": "Unclear from the CV",
         "confident": False},
    ],
    "salary_expectation": "12,000 AED",
    "_model": "gemini-2.5-flash",
}

FIELD_MAP = [
    {"selector": "#cv", "kind": "resume", "label": "Upload CV",
     "input_type": "file", "required": True},
    {"selector": "#cover", "kind": "cover_letter",
     "label": "Why do you want this role?", "input_type": "textarea",
     "required": False},
    {"selector": "#salary", "kind": "salary", "label": "Expected salary",
     "input_type": "text", "required": False},
    {"selector": "#sip", "kind": "unknown", "label": "Years with SIP?",
     "input_type": "text", "required": False},
]


def _row(**overrides):
    payload = {
        "fields": {
            "#cover": DRAFT["cover_letter"],
            "#salary": "12,000 AED",
            "#sip": "Three",
        },
        "field_map": FIELD_MAP,
        "draft": json.loads(json.dumps(DRAFT)),
        "experience": "3 years",
        "match_score": 88,
        "form_ok": True,
        "form_note": "found resume, cover_letter",
    }
    row = {
        "id": 7,
        "company": "Etisalat",
        "role": "VoIP Engineer",
        "platform": "tanqeeb:uae",
        "job_url": "https://uae.tanqeeb.com/jobs/021136159.html",
        "submitted_payload_json": json.dumps(payload, ensure_ascii=False),
        "cover_letter_text": DRAFT["cover_letter"],
        "status": "review_pending",
        "revision": 0,
        "screenshot_path": "",
    }
    row.update(overrides)
    return row


def _ev(**overrides):
    kwargs = dict(
        fingerprint="fp-review", ref_id=101, company_name="Etisalat",
        role_title="VoIP Engineer", location="Dubai", match_score=88,
        source_platform="tanqeeb:uae",
        direct_link="https://uae.tanqeeb.com/jobs/021136159.html",
        why_matched="Asterisk and SIP overlap.",
    )
    kwargs.update(overrides)
    return Evaluation(**kwargs)


# ---------------------------------------------------------------------------
class TestDraftCardNormalisation(unittest.TestCase):
    """Both sources of a card must produce the same card."""

    def test_from_draft_and_from_row_agree(self):
        from_draft = DraftCard.from_draft(7, _ev(), DRAFT, "tanqeeb:uae",
                                          experience="3 years", form_ok=True)
        from_row = DraftCard.from_row(_row())
        for attr in ("app_id", "company", "role", "platform", "match_score",
                     "salary", "experience", "cover_letter", "job_url",
                     "form_ok"):
            with self.subTest(attr=attr):
                self.assertEqual(getattr(from_draft, attr),
                                 getattr(from_row, attr))
        self.assertEqual(len(from_draft.answers), len(from_row.answers))

    def test_the_stored_column_beats_the_payload_copy(self):
        """After an edit, `cover_letter_text` is the truth."""
        row = _row(cover_letter_text="EDITED LETTER")
        self.assertEqual(DraftCard.from_row(row).cover_letter, "EDITED LETTER")

    def test_a_row_with_an_unreadable_payload_still_renders(self):
        """A card that refuses to render is a draft the user cannot act on."""
        card = DraftCard.from_row(_row(submitted_payload_json="{not json"))
        self.assertEqual(card.app_id, 7)
        self.assertEqual(card.company, "Etisalat")
        self.assertFalse(card.form_ok)
        self.assertIn("Etisalat", format_review_telegram(card))

    def test_an_empty_payload_is_treated_as_no_confirmed_form(self):
        card = DraftCard.from_row(_row(submitted_payload_json=""))
        self.assertFalse(card.form_ok, "an absent flag must not read as True")

    def test_payload_of_tolerates_every_stored_shape(self):
        self.assertEqual(payload_of({"submitted_payload_json": None}), {})
        self.assertEqual(payload_of({"submitted_payload_json": "[]"}), {})
        self.assertEqual(payload_of({"submitted_payload_json": {"a": 1}}),
                         {"a": 1})

    def test_needs_you_counts_only_explicit_false(self):
        card = DraftCard.from_row(_row())
        self.assertEqual(card.needs_you, 1)

    def test_missing_names_degrade_to_unknown_not_none(self):
        card = DraftCard.from_row(_row(company=None, role=None, platform=None))
        self.assertEqual(card.company, "Unknown")
        self.assertEqual(card.role, "Unknown")
        self.assertEqual(card.platform, "unknown")

    def test_an_unrecorded_score_is_absent_not_zero(self):
        """"0%" on a card whose entire job is to help you decide is not a
        missing value, it is a wrong one -- and drafts written before the score
        was stored alongside them genuinely have none."""
        card = DraftCard.from_row(_row(submitted_payload_json="{}"))
        self.assertIsNone(card.match_score)
        self.assertIn("not recorded", format_review_telegram(card))
        self.assertNotIn("0%", format_review_telegram(card))
        self.assertNotIn("%", format_review_whatsapp(card).splitlines()[0])

    def test_a_recorded_score_is_shown_on_both_cards(self):
        card = DraftCard.from_row(_row())
        self.assertEqual(card.match_score, 88)
        self.assertIn("88%", format_review_telegram(card))
        self.assertIn("88%", format_review_whatsapp(card))

    def test_a_corrupt_score_reads_as_unrecorded_rather_than_crashing(self):
        row = _row(submitted_payload_json=json.dumps({"match_score": "high"}))
        self.assertIsNone(DraftCard.from_row(row).match_score)


# ---------------------------------------------------------------------------
class TestTelegramReviewCard(unittest.TestCase):
    def setUp(self):
        self.card = DraftCard.from_row(_row())
        self.msg = format_review_telegram(self.card)

    def test_carries_every_field_the_spec_requires(self):
        for needle in ("[DRAFT #7]", "Etisalat", "VoIP Engineer",
                       "tanqeeb:uae", "88%", "12,000 AED", "3 years",
                       "Years with SIP?", "Issabel"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.msg)

    def test_low_confidence_answers_are_flagged(self):
        self.assertIn("NEEDS YOU", self.msg)

    def test_carries_the_clickable_link(self):
        self.assertIn("uae.tanqeeb.com", self.msg)

    def test_shows_both_reply_syntaxes(self):
        self.assertIn("done 7", self.msg)
        self.assertIn("موافق 7", self.msg)
        self.assertIn("edit 7 salary:", self.msg)
        self.assertIn("تعديل 7", self.msg)
        self.assertIn("decline 7", self.msg)

    def test_answers_are_numbered_so_they_can_be_edited_by_number(self):
        """"edit 7 answer 2" only means something if the card numbers them."""
        self.assertIn("1. Years with SIP?", self.msg)
        self.assertIn("2. Expected salary?", self.msg)

    def test_stays_inside_the_telegram_limit(self):
        huge = _row()
        payload = json.loads(huge["submitted_payload_json"])
        payload["draft"]["cover_letter"] = "لوريم إيبسوم " * 900
        payload["draft"]["answers"] = [
            {"question": f"Q{i} " * 20, "answer": "A" * 400, "confident": False}
            for i in range(30)
        ]
        huge["submitted_payload_json"] = json.dumps(payload, ensure_ascii=False)
        huge["cover_letter_text"] = payload["draft"]["cover_letter"]
        self.assertLessEqual(
            len(format_review_telegram(DraftCard.from_row(huge))), 4000
        )

    def test_a_missing_form_is_called_out_with_the_manual_link(self):
        row = _row()
        payload = json.loads(row["submitted_payload_json"])
        payload["form_ok"] = False
        payload["form_note"] = "this looks like the site's search widget"
        row["submitted_payload_json"] = json.dumps(payload)
        msg = format_review_telegram(DraftCard.from_row(row))
        self.assertIn("NO AUTO-SUBMITTABLE FORM FOUND", msg)
        self.assertIn("search widget", msg)
        self.assertIn("uae.tanqeeb.com", msg)

    def test_revisions_are_visible(self):
        msg = format_review_telegram(DraftCard.from_row(_row(revision=3)))
        self.assertIn("3 edit(s) applied", msg)

    def test_no_screening_questions_says_so(self):
        row = _row()
        payload = json.loads(row["submitted_payload_json"])
        payload["draft"]["answers"] = []
        row["submitted_payload_json"] = json.dumps(payload)
        self.assertIn("no screening questions",
                      format_review_telegram(DraftCard.from_row(row)))


# ---------------------------------------------------------------------------
class TestWhatsAppReviewCard(unittest.TestCase):
    def setUp(self):
        self.card = DraftCard.from_row(_row())
        self.msg = format_review_whatsapp(self.card)

    def test_fits_the_encoded_url_budget(self):
        self.assertLessEqual(_encoded_len(self.msg), MAX_URL_LENGTH)

    def test_carries_the_decision_critical_metadata(self):
        for needle in ("[DRAFT #7]", "Etisalat", "VoIP Engineer", "88%",
                       "12,000 AED", "3 years"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.msg)

    def test_never_carries_the_application_url(self):
        """The URL is what blows the budget; the reference replaces it."""
        self.assertNotIn("http", self.msg)
        self.assertIn("Telegram Saved Messages [DRAFT #7]", self.msg)

    def test_uses_whatsapp_single_asterisk_bold(self):
        self.assertIn("*Company:*", self.msg)
        self.assertNotIn("**", self.msg)

    def test_carries_the_reply_syntax(self):
        self.assertIn("done 7", self.msg)
        self.assertIn("edit 7", self.msg)

    def test_an_arabic_cover_letter_still_fits(self):
        """Arabic costs ~5.6 URL characters each. This is the case that breaks."""
        row = _row(cover_letter_text="مطلوب مهندس دعم فني للعمل في شركة اتصالات " * 30)
        msg = format_review_whatsapp(DraftCard.from_row(row))
        self.assertLessEqual(
            _encoded_len(msg), MAX_URL_LENGTH,
            "an Arabic review card would be dropped by CallMeBot entirely",
        )

    def test_metadata_survives_even_when_the_letter_is_pathological(self):
        """The elastic part is the preview; the facts and controls are not."""
        row = _row(cover_letter_text="لوريم إيبسوم دولار " * 400)
        msg = format_review_whatsapp(DraftCard.from_row(row))
        self.assertLessEqual(_encoded_len(msg), MAX_URL_LENGTH)
        for needle in ("[DRAFT #7]", "Etisalat", "done 7"):
            with self.subTest(needle=needle):
                self.assertIn(needle, msg)

    def test_answer_count_is_summarised_not_listed(self):
        self.assertIn("2 drafted", self.msg)
        self.assertIn("1 need you", self.msg)

    def test_says_so_when_telegram_did_not_arrive(self):
        """Otherwise the pointer sends the reader after a card that is not there."""
        msg = format_review_whatsapp(self.card, telegram_delivered=False)
        self.assertNotIn("Full card: Telegram", msg)
        self.assertIn("did not arrive", msg)

    def test_a_missing_form_is_warned_about_here_too(self):
        row = _row()
        payload = json.loads(row["submitted_payload_json"])
        payload["form_ok"] = False
        row["submitted_payload_json"] = json.dumps(payload)
        self.assertIn("No auto-submit form",
                      format_review_whatsapp(DraftCard.from_row(row)))


# ---------------------------------------------------------------------------
class TestBothCardsAgree(unittest.TestCase):
    def test_the_reference_is_identical(self):
        card = DraftCard.from_row(_row())
        ref = draft_ref(7)
        self.assertIn(ref, format_review_telegram(card))
        self.assertIn(ref, format_review_whatsapp(card))

    def test_every_card_carries_the_bot_marker(self):
        """The listener skips its own cards on this marker alone."""
        card = DraftCard.from_row(_row())
        app = {"company": "A", "role": "B", "platform": "c",
               "screenshot_path": "/x/s.png"}
        for msg in (
            format_review_telegram(card), format_review_whatsapp(card),
            format_submitted_telegram(7, app), format_submitted_whatsapp(7, app),
            format_failure_telegram(7, app, "boom"),
            format_failure_whatsapp(7, app, "boom"),
        ):
            with self.subTest(msg=msg[:40]):
                self.assertIn(BOT_MARK, msg)
                self.assertTrue(msg.startswith(BOT_MARK),
                                "the marker must lead, since every transport "
                                "truncates from the end")


# ---------------------------------------------------------------------------
class TestOutcomeCards(unittest.TestCase):
    APP = {
        "company": "Etisalat", "role": "VoIP Engineer", "platform": "tanqeeb",
        "job_url": "https://uae.tanqeeb.com/jobs/1.html",
        "screenshot_path": r"C:\shots\20260819-101500_app7.png",
    }

    def test_submitted_card_names_the_file_not_the_path(self):
        for msg in (format_submitted_telegram(7, self.APP),
                    format_submitted_whatsapp(7, self.APP)):
            with self.subTest(msg=msg[:30]):
                self.assertIn("20260819-101500_app7.png", msg)
                self.assertNotIn("C:\\shots", msg)

    def test_a_posix_path_is_split_too(self):
        """The screenshot is captured on Windows; the vault reads from Linux."""
        app = dict(self.APP, screenshot_path="/home/x/screenshots/shot.png")
        self.assertIn("shot.png", format_submitted_telegram(7, app))
        self.assertNotIn("/home/x", format_submitted_telegram(7, app))

    def test_no_screenshot_is_reported_honestly(self):
        app = dict(self.APP, screenshot_path="")
        self.assertIn("not captured", format_submitted_telegram(7, app))
        self.assertIn("not captured", format_submitted_whatsapp(7, app))

    def test_whatsapp_confirmation_fits_the_budget(self):
        self.assertLessEqual(
            _encoded_len(format_submitted_whatsapp(7, self.APP)), MAX_URL_LENGTH
        )

    def test_failure_card_keeps_the_link_to_apply_by_hand(self):
        msg = format_failure_telegram(7, self.APP, "Could not find a submit button")
        self.assertIn("uae.tanqeeb.com", msg)
        self.assertIn("draft is kept", msg)
        self.assertIn("submit button", msg)

    def test_failure_card_fits_whatsapp_even_with_a_huge_reason(self):
        msg = format_failure_whatsapp(7, self.APP, "خطأ فادح جدا " * 200)
        self.assertLessEqual(_encoded_len(msg), MAX_URL_LENGTH)


# ---------------------------------------------------------------------------
class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.n = RecordingNotifier()

    def test_review_goes_to_both_channels_with_different_content(self):
        dispatch_review(self.n, DraftCard.from_row(_row()))
        self.assertEqual(len(self.n.telegram), 1)
        self.assertEqual(len(self.n.whatsapp), 1)
        self.assertNotEqual(self.n.telegram[0], self.n.whatsapp[0],
                            "the two channels must carry tailored cards")
        self.assertIn("http", self.n.telegram[0])
        self.assertNotIn("http", self.n.whatsapp[0])

    def test_submission_pushes_the_screenshot_to_telegram(self):
        app = {"company": "A", "role": "B", "platform": "c",
               "screenshot_path": "/x/proof.png"}
        dispatch_submitted(self.n, 7, app)
        self.assertEqual([p[0] for p in self.n.photos], ["/x/proof.png"])
        self.assertIn("[DRAFT #7]", self.n.photos[0][1])

    def test_no_screenshot_means_no_photo_send(self):
        dispatch_submitted(self.n, 7, {"company": "A", "screenshot_path": ""})
        self.assertEqual(self.n.photos, [])

    def test_failure_reaches_both_channels(self):
        dispatch_failure(self.n, 7, {"company": "A", "role": "B"}, "boom")
        self.assertIn("APPLICATION FAILED", self.n.telegram[-1])
        self.assertIn("APPLICATION FAILED", self.n.whatsapp[-1])

    def test_a_none_notifier_is_a_no_op_not_a_crash(self):
        self.assertIsNone(dispatch_review(None, DraftCard.from_row(_row())))

    def test_switching_whatsapp_off_is_honoured(self):
        """A documented switch that is silently ignored is worse than no switch."""
        from config import settings

        original = dict(settings.raw.get("hitl", {}) or {})
        settings.raw["hitl"] = dict(original,
                                    channels={"telegram": True, "whatsapp": False})
        try:
            self.assertEqual(enabled_channels(), ("telegram",))
            dispatch_review(self.n, DraftCard.from_row(_row()))
        finally:
            settings.raw["hitl"] = original
        self.assertEqual(len(self.n.telegram), 1)
        self.assertEqual(self.n.whatsapp, [])

    def test_switching_telegram_off_is_honoured(self):
        from config import settings

        original = dict(settings.raw.get("hitl", {}) or {})
        settings.raw["hitl"] = dict(original,
                                    channels={"telegram": False, "whatsapp": True})
        try:
            self.assertEqual(enabled_channels(), ("whatsapp",))
            dispatch_review(self.n, DraftCard.from_row(_row()))
        finally:
            settings.raw["hitl"] = original
        self.assertEqual(self.n.telegram, [])
        self.assertEqual(len(self.n.whatsapp), 1)

    def test_switching_both_off_is_ignored_rather_than_silencing_the_gate(self):
        from config import settings

        original = dict(settings.raw.get("hitl", {}) or {})
        settings.raw["hitl"] = dict(
            original, channels={"telegram": False, "whatsapp": False}
        )
        try:
            self.assertEqual(enabled_channels(), ("telegram", "whatsapp"))
        finally:
            settings.raw["hitl"] = original

    def test_no_hitl_block_at_all_means_both_channels(self):
        from config import settings

        original = settings.raw.pop("hitl", None)
        try:
            self.assertEqual(enabled_channels(), ("telegram", "whatsapp"))
        finally:
            if original is not None:
                settings.raw["hitl"] = original

    def test_a_telegram_only_notifier_still_gets_the_card(self):
        """Losing the card entirely is worse than losing the tailoring."""
        class Legacy:
            def __init__(self):
                self.sent = []

            def send_via_telegram(self, message):
                self.sent.append(message)
                return True, "ok"

        legacy = Legacy()
        dispatch_review(legacy, DraftCard.from_row(_row()))
        self.assertEqual(len(legacy.sent), 1)
        self.assertIn("[DRAFT #7]", legacy.sent[0])


# ---------------------------------------------------------------------------
class TestRealSendDual(unittest.TestCase):
    """The notifier's own dual send, driven through conftest's recorders."""

    def setUp(self):
        from conftest import OUTBOX
        from notifier import WhatsAppNotifier

        self.outbox = OUTBOX
        self.outbox.clear()
        self.n = WhatsAppNotifier(None)
        self.n.dry_run = True

    def test_both_channels_get_their_own_text(self):
        result = self.n.send_dual("the long card", "the short card")
        self.assertTrue(result.delivered)
        self.assertEqual(result.channels, ["telegram", "whatsapp"])
        self.assertEqual(self.outbox.telegram, ["the long card"])
        self.assertEqual(self.outbox.whatsapp, ["the short card"])

    def test_whatsapp_defaults_to_the_telegram_text(self):
        self.n.send_dual("one card for both")
        self.assertEqual(self.outbox.whatsapp, ["one card for both"])

    def test_a_photo_rides_the_telegram_side_only(self):
        self.n.send_dual("card", "short", photo="/x/shot.png",
                         photo_caption="[DRAFT #7]")
        self.assertEqual(self.outbox.photos, [("/x/shot.png", "[DRAFT #7]")])

    def test_a_disabled_channel_reports_disabled_not_failed(self):
        result = self.n.send_dual("card", "short", channels=("telegram",))
        self.assertTrue(result.telegram_ok)
        self.assertFalse(result.whatsapp_ok)
        self.assertEqual(result.whatsapp_detail, "disabled")
        self.assertTrue(result.delivered, "one live channel is still delivery")
        self.assertEqual(self.outbox.whatsapp, [])

    def test_a_disabled_telegram_skips_the_photo_too(self):
        result = self.n.send_dual("card", "short", photo="/x/shot.png",
                                  channels=("whatsapp",))
        self.assertEqual(self.outbox.telegram, [])
        self.assertEqual(self.outbox.photos, [])
        self.assertEqual(result.photo_detail, "disabled")

    def test_no_channels_at_all_delivers_nothing_and_says_so(self):
        with self.assertLogs("notifier", level="WARNING"):
            result = self.n.send_dual("card", "short", channels=())
        self.assertFalse(result.delivered)
        self.assertEqual(self.outbox.total, 0)

    def test_telegram_goes_first(self):
        """The WhatsApp card points at the Telegram one by reference, so the
        pointer must only ever be printed once its target already exists.

        Asserted on the real call ORDER rather than on the source text -- the
        method's own docstring names both transports, so reading the source
        proves nothing about which one actually runs first.
        """
        from notifier import WhatsAppNotifier

        order: list[str] = []
        stub_tg = WhatsAppNotifier.send_via_telegram
        stub_wa = WhatsAppNotifier._send_callmebot
        WhatsAppNotifier.send_via_telegram = (
            lambda self, message: (order.append("telegram"), (True, "x"))[1]
        )
        WhatsAppNotifier._send_callmebot = (
            lambda self, message: (order.append("whatsapp"), (True, "x"))[1]
        )
        try:
            self.n.send_dual("card", "short")
        finally:
            WhatsAppNotifier.send_via_telegram = stub_tg
            WhatsAppNotifier._send_callmebot = stub_wa
        self.assertEqual(order, ["telegram", "whatsapp"])


# ---------------------------------------------------------------------------
class TestApplyEdit(unittest.TestCase):
    """The edit must change what is SUBMITTED, not just what is displayed."""

    def setUp(self):
        self.payload = payload_of(_row())

    def test_cover_letter_updates_both_the_draft_and_the_form_field(self):
        result = apply_edit(self.payload, FIELD_COVER_LETTER, "Brand new text.")
        self.assertTrue(result.ok)
        self.assertEqual(result.cover_letter, "Brand new text.")
        self.assertEqual(result.payload["draft"]["cover_letter"],
                         "Brand new text.")
        self.assertEqual(
            result.payload["fields"]["#cover"], "Brand new text.",
            "the browser types fields[]; leaving it stale submits the OLD "
            "letter while the card shows the new one",
        )
        self.assertEqual(result.selectors, ["#cover"])
        self.assertEqual(result.warning, "")

    def test_salary_updates_both_halves(self):
        result = apply_edit(self.payload, FIELD_SALARY, "18,000 AED")
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["draft"]["salary_expectation"],
                         "18,000 AED")
        self.assertEqual(result.payload["fields"]["#salary"], "18,000 AED")
        self.assertIsNone(result.cover_letter,
                          "a salary edit must not touch the cover letter")

    def test_the_original_payload_is_never_mutated(self):
        """The caller still needs the old value if the write fails."""
        before = json.dumps(self.payload, sort_keys=True)
        apply_edit(self.payload, FIELD_COVER_LETTER, "changed")
        self.assertEqual(json.dumps(self.payload, sort_keys=True), before)

    def test_an_answer_edit_finds_the_field_by_its_question(self):
        result = apply_edit(self.payload, FIELD_ANSWER, "Five years",
                            answer_index=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["draft"]["answers"][0]["answer"],
                         "Five years")
        self.assertEqual(result.payload["fields"]["#sip"], "Five years")

    def test_a_human_written_answer_stops_being_a_model_guess(self):
        result = apply_edit(self.payload, FIELD_ANSWER, "12,000 AED",
                            answer_index=2)
        self.assertIs(result.payload["draft"]["answers"][1]["confident"], True)

    def test_an_out_of_range_answer_is_refused(self):
        for index in (0, 3, 99, None):
            with self.subTest(index=index):
                result = apply_edit(self.payload, FIELD_ANSWER, "x",
                                    answer_index=index)
                self.assertFalse(result.ok)
                self.assertIn("between 1 and 2", result.description)

    def test_a_field_the_form_never_asked_for_is_recorded_with_a_warning(self):
        """Refusing would lose the decision; silence would imply a field."""
        payload = payload_of(_row())
        payload["field_map"] = [f for f in payload["field_map"]
                                if f["kind"] != "salary"]
        result = apply_edit(payload, FIELD_SALARY, "20,000")
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["draft"]["salary_expectation"], "20,000")
        self.assertEqual(result.selectors, [])
        self.assertIn("no salary input", result.warning)

    def test_experience_is_stored_where_the_card_reads_it(self):
        result = apply_edit(self.payload, FIELD_EXPERIENCE, "5 years")
        self.assertEqual(result.payload["experience"], "5 years")
        self.assertEqual(DraftCard.from_row(_row(
            submitted_payload_json=json.dumps(result.payload)
        )).experience, "5 years")

    def test_a_note_is_recorded_and_changes_nothing_else(self):
        result = apply_edit(self.payload, FIELD_NOTE, "make it warmer")
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["notes"], ["make it warmer"])
        self.assertEqual(result.payload["fields"],
                         self.payload["fields"],
                         "a note must never silently rewrite a form value")

    def test_notes_accumulate(self):
        first = apply_edit(self.payload, FIELD_NOTE, "one")
        second = apply_edit(first.payload, FIELD_NOTE, "two")
        self.assertEqual(second.payload["notes"], ["one", "two"])

    def test_blanking_a_cover_letter_is_refused(self):
        result = apply_edit(self.payload, FIELD_COVER_LETTER, "   ")
        self.assertFalse(result.ok)
        self.assertIn("nothing", result.description)

    def test_blanking_an_answer_is_refused(self):
        result = apply_edit(self.payload, FIELD_ANSWER, "  ", answer_index=1)
        self.assertFalse(result.ok)

    def test_an_unnamed_field_is_refused(self):
        self.assertFalse(apply_edit(self.payload, "", "x").ok)

    def test_a_legacy_draft_without_a_field_map_still_updates_the_form(self):
        """Drafts written before `field_map` existed must not silently submit
        the old letter."""
        payload = payload_of(_row())
        old_letter = payload["fields"]["#cover"]
        del payload["field_map"]
        result = apply_edit(payload, FIELD_COVER_LETTER, "NEW",
                            previous_cover=old_letter)
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["fields"]["#cover"], "NEW")

    def test_a_legacy_draft_with_no_match_warns_rather_than_pretending(self):
        payload = payload_of(_row())
        del payload["field_map"]
        result = apply_edit(payload, FIELD_COVER_LETTER, "NEW",
                            previous_cover="something that was never stored")
        self.assertTrue(result.ok)
        self.assertEqual(result.selectors, [])
        self.assertIn("no cover-letter input", result.warning)

    def test_editing_a_draft_with_no_answers_is_refused_clearly(self):
        payload = payload_of(_row())
        payload["draft"]["answers"] = []
        result = apply_edit(payload, FIELD_ANSWER, "x", answer_index=1)
        self.assertFalse(result.ok)
        self.assertIn("no screening answers", result.description)

    def test_edits_compose(self):
        """Two edits in a row must both survive."""
        first = apply_edit(self.payload, FIELD_SALARY, "20,000 AED")
        second = apply_edit(first.payload, FIELD_COVER_LETTER, "Second draft.")
        self.assertEqual(second.payload["fields"]["#salary"], "20,000 AED")
        self.assertEqual(second.payload["fields"]["#cover"], "Second draft.")

    def test_field_map_reader_tolerates_junk(self):
        self.assertEqual(field_map({}), [])
        self.assertEqual(field_map({"field_map": "nope"}), [])
        self.assertEqual(field_map({"field_map": [1, {"selector": "#a"}]}),
                         [{"selector": "#a"}])

    def test_edit_result_repr_is_readable(self):
        self.assertIn("EditResult", repr(EditResult(True, {}, None, "d")))


class TestCoverLetterRevision(unittest.TestCase):
    """The free-form path. Its contract is that failure is not an exception."""

    def _patch_generate(self, behaviour):
        import evaluator as evaluator_mod

        original = evaluator_mod.GeminiEvaluator._generate
        evaluator_mod.GeminiEvaluator._generate = behaviour
        self.addCleanup(setattr, evaluator_mod.GeminiEvaluator, "_generate",
                        original)

    @staticmethod
    def _envelope(text):
        return ({"candidates": [{"content": {"parts": [{"text": text}]}}]},
                "fake-model")

    def test_a_revision_returns_the_whole_rewritten_letter(self):
        self._patch_generate(
            lambda self, body: TestCoverLetterRevision._envelope(
                json.dumps({"cover_letter": "A shorter letter."})
            )
        )
        self.assertEqual(
            revise_cover_letter("The long original.", "make it shorter"),
            "A shorter letter.",
        )

    def test_the_instruction_and_the_letter_both_reach_the_model(self):
        seen = {}

        def capture(self, body):
            seen["prompt"] = body["contents"][0]["parts"][0]["text"]
            return TestCoverLetterRevision._envelope(
                json.dumps({"cover_letter": "x"})
            )

        self._patch_generate(capture)
        card = DraftCard.from_row(_row())
        revise_cover_letter("Original letter about Issabel.",
                            "mention Asterisk", card)
        self.assertIn("Original letter about Issabel.", seen["prompt"])
        self.assertIn("mention Asterisk", seen["prompt"])
        self.assertIn("Etisalat", seen["prompt"], "the job context was dropped")

    def test_a_model_failure_returns_empty_rather_than_raising(self):
        """A failed revision must degrade to "your note was recorded", never to
        a lost draft or a listener that dies on the next message."""
        def explode(self, body):
            raise RuntimeError("quota exhausted")

        self._patch_generate(explode)
        self.assertEqual(revise_cover_letter("letter", "shorter"), "")

    def test_unparseable_output_returns_empty(self):
        self._patch_generate(
            lambda self, body: TestCoverLetterRevision._envelope("not json")
        )
        self.assertEqual(revise_cover_letter("letter", "shorter"), "")

    def test_an_empty_letter_or_instruction_never_calls_the_model(self):
        called = []
        self._patch_generate(lambda self, body: called.append(1))
        self.assertEqual(revise_cover_letter("", "shorter"), "")
        self.assertEqual(revise_cover_letter("letter", "   "), "")
        self.assertEqual(called, [])

    def test_the_instruction_forbids_inventing_facts(self):
        from auto_apply.review import REVISE_INSTRUCTION

        self.assertIn("Never add experience", REVISE_INSTRUCTION)
        self.assertIn("stay in Arabic", REVISE_INSTRUCTION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
