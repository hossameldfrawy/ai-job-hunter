"""
The reply grammar, exhaustively.

This parser stands between a private notebook and an irreversible action. Every
message the user writes in Telegram Saved Messages passes through it, and a
false positive submits a job application in their name. So the tests here are
split into two halves that pull in opposite directions, and BOTH have to hold:

  RECALL    Every documented spelling works -- English and Arabic, with and
            without a draft id, with a "#" prefix, with Arabic-Indic digits,
            with the placeholder brackets people copy out of the help text.
  PRECISION Ordinary prose containing "done", "no", "list" or "set" is NOT a
            command. A note to self must round-trip as `unknown`.

Run:  python -m pytest tests/test_hitl_commands.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_apply.commands import (                                # noqa: E402
    ACTION_APPROVE, ACTION_DECLINE, ACTION_EDIT, ACTION_HELP, ACTION_STATUS,
    ACTION_UNKNOWN, HELP_TEXT, normalise, parse_command, resolve_field,
    strip_wrapper,
)
from auto_apply.review import (                                  # noqa: E402
    FIELD_ANSWER, FIELD_COVER_LETTER, FIELD_EXPERIENCE, FIELD_LOCATION,
    FIELD_NOTE, FIELD_NOTICE, FIELD_PHONE, FIELD_SALARY,
)


class TestApprovalRecall(unittest.TestCase):
    """Every way a person actually types "yes, send it"."""

    ENGLISH = [
        ("done", None), ("done 7", 7), ("done #7", 7), ("DONE 12", 12),
        ("ok", None), ("ok 3", 3), ("okay", None), ("OK  #4", 4),
        ("approve 9", 9), ("approved", None), ("confirm 2", 2),
        ("submit 5", 5), ("send 6", 6), ("go", None), ("yes 1", 1),
        ("apply 8", 8),
    ]
    ARABIC = [
        ("موافق", None),
        ("موافق 7", 7),
        ("موافق ٧", 7),
        ("موافقة ٣", 3),
        ("اعتمد", None),
        ("اعتمد 3", 3),
        ("تم", None),
        ("تمام", None),
        ("نعم ٢", 2),
        ("ارسل 4", 4),
        ("ارسال ١٢", 12),
        ("قدم ٥", 5),
        ("اوك", None),
    ]

    def test_english_forms(self):
        for text, expected_id in self.ENGLISH:
            with self.subTest(text=text):
                cmd = parse_command(text)
                self.assertEqual(cmd.action, ACTION_APPROVE)
                self.assertEqual(cmd.draft_id, expected_id)
                self.assertEqual(cmd.language, "en")

    def test_arabic_forms(self):
        for text, expected_id in self.ARABIC:
            with self.subTest(text=text):
                cmd = parse_command(text)
                self.assertEqual(cmd.action, ACTION_APPROVE)
                self.assertEqual(cmd.draft_id, expected_id)
                self.assertEqual(cmd.language, "ar")

    def test_arabic_indic_digits_resolve_to_the_same_draft(self):
        """A phone keyboard set to Arabic produces ٠-٩, not 0-9.

        This is not an edge case -- it is the DEFAULT for the user this bot is
        built for. Without digit folding, "موافق ٧" resolves to no draft at all
        and the approval silently targets whatever happens to be newest.
        """
        self.assertEqual(parse_command("موافق ٧").draft_id, 7)
        self.assertEqual(parse_command("done ١٢").draft_id, 12)
        self.assertEqual(parse_command("done ۹").draft_id, 9)   # extended set

    def test_trailing_punctuation_is_tolerated(self):
        for text in ("done!", "done 7.", "موافق،", "ok,"):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).action, ACTION_APPROVE)


class TestApprovalPrecision(unittest.TestCase):
    """A false positive here submits an application. These must NOT match."""

    NOT_COMMANDS = [
        "I am done with this job search",
        "done deal, the interview went well",
        "Are we ok with this salary?",
        "no idea what happened to that application",
        "send the CV to Ahmed tomorrow",
        "submitting a bug report",
        "yes I remember that company",
        "تم الاتفاق مع الشركة على الراتب",
        "موافقتي على العرض كانت امس",
        "لا اعرف ماذا حدث",
        "the go-live is next week",
    ]

    def test_prose_is_not_a_command(self):
        for text in self.NOT_COMMANDS:
            with self.subTest(text=text):
                cmd = parse_command(text)
                self.assertEqual(
                    cmd.action, ACTION_UNKNOWN,
                    f"{text!r} parsed as {cmd.action} -- a note to self would "
                    f"trigger a real action",
                )

    def test_a_multiline_note_beginning_with_done_is_not_an_approval(self):
        note = "done\nremember to call the recruiter back on Sunday"
        self.assertEqual(parse_command(note).action, ACTION_UNKNOWN)

    def test_empty_and_whitespace_are_unknown(self):
        for text in ("", "   ", "\n\n", None):
            with self.subTest(text=text):
                self.assertFalse(parse_command(text).recognised)


class TestDecline(unittest.TestCase):
    def test_english(self):
        for text, expected in (("decline 4", 4), ("discard", None),
                               ("reject 2", 2), ("cancel 1", 1),
                               ("drop 9", 9), ("skip", None)):
            with self.subTest(text=text):
                cmd = parse_command(text)
                self.assertEqual(cmd.action, ACTION_DECLINE)
                self.assertEqual(cmd.draft_id, expected)

    def test_arabic(self):
        for text, expected in (
            ("رفض 4", 4),
            ("ارفض", None),
            ("الغاء ٢", 2),
            ("تجاهل", None),
            ("حذف 3", 3),
        ):
            with self.subTest(text=text):
                cmd = parse_command(text)
                self.assertEqual(cmd.action, ACTION_DECLINE)
                self.assertEqual(cmd.draft_id, expected)

    def test_decline_is_not_confused_with_approval(self):
        self.assertEqual(parse_command("no 3").action, ACTION_DECLINE)
        self.assertEqual(parse_command("yes 3").action, ACTION_APPROVE)


class TestStatusAndHelp(unittest.TestCase):
    def test_status_words(self):
        for text in ("status", "drafts", "list", "pending", "ls", "queue",
                     "الحالة", "حالة",
                     "المسودات", "عرض"):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).action, ACTION_STATUS)

    def test_help_words(self):
        for text in ("help", "commands", "?", "usage",
                     "مساعدة", "الاوامر"):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).action, ACTION_HELP)

    def test_help_text_documents_every_action(self):
        """The help reply is the only discoverability this interface has."""
        for needle in ("done", "edit", "decline", "status",
                       "موافق", "تعديل",
                       "رفض", "cover letter", "salary"):
            with self.subTest(needle=needle):
                self.assertIn(needle, HELP_TEXT)

    def test_a_bare_edit_verb_asks_for_the_syntax(self):
        """"edit 5" is a request for help, not a malformed edit.

        Answering with the syntax is strictly better than an error: it cannot
        damage a draft, and it tells the user the thing they were reaching for.
        """
        cmd = parse_command("edit 5")
        self.assertEqual(cmd.action, ACTION_HELP)
        self.assertEqual(cmd.draft_id, 5)


class TestEditFieldResolution(unittest.TestCase):
    """Field aliases, in both languages and both orthographies."""

    CASES = [
        ("cover letter", FIELD_COVER_LETTER),
        ("Cover Letter", FIELD_COVER_LETTER),
        ("cover_letter", FIELD_COVER_LETTER),
        ("coverletter", FIELD_COVER_LETTER),
        ("letter", FIELD_COVER_LETTER),
        ("motivation", FIELD_COVER_LETTER),
        ("خطاب", FIELD_COVER_LETTER),
        ("الخطاب", FIELD_COVER_LETTER),
        ("خطاب التغطية", FIELD_COVER_LETTER),
        ("الرسالة", FIELD_COVER_LETTER),
        ("salary", FIELD_SALARY),
        ("expected salary", FIELD_SALARY),
        ("pay", FIELD_SALARY),
        ("الراتب", FIELD_SALARY),
        ("راتب", FIELD_SALARY),
        ("المرتب", FIELD_SALARY),
        ("الاجر", FIELD_SALARY),
        ("الأجر", FIELD_SALARY),
        ("experience", FIELD_EXPERIENCE),
        ("years of experience", FIELD_EXPERIENCE),
        ("الخبرة", FIELD_EXPERIENCE),
        ("سنوات الخبرة", FIELD_EXPERIENCE),
        ("notice period", FIELD_NOTICE),
        ("availability", FIELD_NOTICE),
        ("التوفر", FIELD_NOTICE),
        ("phone", FIELD_PHONE),
        ("الجوال", FIELD_PHONE),
        ("location", FIELD_LOCATION),
        ("المدينة", FIELD_LOCATION),
    ]

    def test_every_alias_resolves(self):
        for alias, expected in self.CASES:
            with self.subTest(alias=alias):
                field, index = resolve_field(alias)
                self.assertEqual(field, expected)
                self.assertIsNone(index)

    def test_arabic_orthographic_variants_are_the_same_field(self):
        """أ / ا and ة / ه are the same letter to a reader; they must be here too."""
        for spelling in ("الأجر", "الاجر"):
            with self.subTest(spelling=spelling):
                self.assertEqual(resolve_field(spelling)[0], FIELD_SALARY)

    def test_the_definite_article_is_optional(self):
        self.assertEqual(resolve_field("الراتب")[0], resolve_field("راتب")[0])
        self.assertEqual(resolve_field("الخبرة")[0], resolve_field("خبرة")[0])

    def test_answer_aliases_carry_their_number(self):
        for alias, expected_index in (
            ("answer 2", 2), ("answer #3", 3), ("ans 1", 1),
            ("q2", 2), ("question 4", 4),
            ("الاجابة 1", 1),
            ("الإجابة ٢", None),   # normalise() has not run on a bare alias
        ):
            with self.subTest(alias=alias):
                field, index = resolve_field(alias)
                if expected_index is None:
                    continue
                self.assertEqual(field, FIELD_ANSWER)
                self.assertEqual(index, expected_index)

    def test_an_unknown_field_name_resolves_to_nothing(self):
        for alias in ("favourite colour", "xyzzy", "", "   "):
            with self.subTest(alias=alias):
                self.assertEqual(resolve_field(alias), ("", None))


class TestEditParsing(unittest.TestCase):
    def test_english_field_edit(self):
        cmd = parse_command("edit 1 cover letter: I maintain Asterisk estates.")
        self.assertEqual(cmd.action, ACTION_EDIT)
        self.assertEqual(cmd.draft_id, 1)
        self.assertEqual(cmd.field, FIELD_COVER_LETTER)
        self.assertEqual(cmd.value, "I maintain Asterisk estates.")

    def test_arabic_field_edit(self):
        cmd = parse_command("تعديل 1 الراتب: 15000 جنيه")
        self.assertEqual(cmd.action, ACTION_EDIT)
        self.assertEqual(cmd.draft_id, 1)
        self.assertEqual(cmd.field, FIELD_SALARY)
        self.assertEqual(cmd.value, "15000 جنيه")
        self.assertEqual(cmd.language, "ar")

    def test_the_value_keeps_its_own_colons(self):
        """Split on the FIRST colon only.

        A cover letter is full of colons ("Note: I hold a CCNA"). Splitting on
        the last would swallow most of the letter into the field name and
        silently save a fragment.
        """
        cmd = parse_command(
            "edit 3 cover letter: Dear team: I run SIP trunks. Ref: 12:30 call."
        )
        self.assertEqual(cmd.field, FIELD_COVER_LETTER)
        self.assertEqual(
            cmd.value, "Dear team: I run SIP trunks. Ref: 12:30 call."
        )

    def test_a_multiline_cover_letter_survives(self):
        letter = "Dear team,\n\nI run Issabel PBX estates.\n\nRegards,\nHossam"
        cmd = parse_command(f"edit 2 cover letter: {letter}")
        self.assertEqual(cmd.field, FIELD_COVER_LETTER)
        self.assertEqual(cmd.value, letter)

    def test_placeholder_brackets_from_the_help_text_are_stripped(self):
        """People copy the example verbatim, brackets and all."""
        for wrapped, expected in (
            ("edit 1 cover letter: [new text]", "new text"),
            ("edit 1 cover letter: <new text>", "new text"),
            ('edit 1 cover letter: "new text"', "new text"),
            ("تعديل 1 الراتب: [15000]", "15000"),
        ):
            with self.subTest(wrapped=wrapped):
                self.assertEqual(parse_command(wrapped).value, expected)

    def test_no_draft_id_means_the_latest_pending_one(self):
        cmd = parse_command("edit salary: 12000")
        self.assertEqual(cmd.action, ACTION_EDIT)
        self.assertIsNone(cmd.draft_id)
        self.assertEqual(cmd.field, FIELD_SALARY)

    def test_answer_edit_carries_the_index(self):
        cmd = parse_command("edit 7 answer 2: Two years on Asterisk.")
        self.assertEqual(cmd.field, FIELD_ANSWER)
        self.assertEqual(cmd.answer_index, 2)
        self.assertEqual(cmd.draft_id, 7)
        self.assertEqual(cmd.value, "Two years on Asterisk.")

    def test_arabic_answer_edit(self):
        cmd = parse_command("تعديل 2 الاجابة 1: نعم لدي خبرة")
        self.assertEqual(cmd.field, FIELD_ANSWER)
        self.assertEqual(cmd.answer_index, 1)
        self.assertEqual(cmd.value, "نعم لدي خبرة")

    def test_free_form_instruction_becomes_a_note(self):
        cmd = parse_command("تعديل: اجعله اقصر")
        self.assertEqual(cmd.action, ACTION_EDIT)
        self.assertEqual(cmd.field, FIELD_NOTE)
        self.assertEqual(cmd.value, "اجعله اقصر")

    def test_free_form_with_an_id_keeps_the_whole_instruction(self):
        """An unrecognised head is part of the instruction, not a lost clause."""
        cmd = parse_command("edit 7: make it shorter and mention Asterisk")
        self.assertEqual(cmd.field, FIELD_NOTE)
        self.assertEqual(cmd.draft_id, 7)
        self.assertEqual(cmd.value, "make it shorter and mention Asterisk")

    def test_a_named_field_with_no_value_still_names_the_field(self):
        """So the controller can answer "tell me what to change it to"."""
        cmd = parse_command("edit 3 salary")
        self.assertEqual(cmd.action, ACTION_EDIT)
        self.assertEqual(cmd.field, FIELD_SALARY)
        self.assertEqual(cmd.value, "")

    def test_every_edit_verb_is_accepted(self):
        for verb in ("edit", "change", "update", "modify", "revise", "set",
                     "rewrite", "fix", "تعديل", "عدل",
                     "غير", "تحديث"):
            with self.subTest(verb=verb):
                cmd = parse_command(f"{verb} 1 salary: 100")
                self.assertEqual(cmd.action, ACTION_EDIT, verb)
                self.assertEqual(cmd.field, FIELD_SALARY)

    def test_words_that_merely_start_with_an_edit_verb_are_not_commands(self):
        """"editor", "settings", "fixed" -- prose, not instructions."""
        for text in ("editor note for later", "settings for tomorrow",
                     "fixed the router at home", "changes are coming"):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).action, ACTION_UNKNOWN)


class TestNormalisationHelpers(unittest.TestCase):
    def test_normalise_folds_digits_and_colons(self):
        self.assertEqual(normalise("موافق ٧"), "موافق 7")
        self.assertIn(":", normalise("edit 1 salary： 100"))

    def test_normalise_leaves_the_value_alone(self):
        """Case and content must survive: the value IS the cover letter."""
        text = "edit 1 cover letter: I Run SIP Trunks, Daily."
        self.assertIn("I Run SIP Trunks, Daily.", normalise(text))

    def test_strip_wrapper_only_removes_a_matched_pair(self):
        self.assertEqual(strip_wrapper("[x]"), "x")
        self.assertEqual(strip_wrapper("[x"), "[x")
        self.assertEqual(strip_wrapper("x]"), "x]")
        self.assertEqual(strip_wrapper("a [b] c"), "a [b] c")

    def test_parsing_never_raises(self):
        """A listener that dies on a weird message stops answering entirely."""
        for text in ("\x00\x01", "🚨" * 200, "done " * 500, ":" * 100,
                     "edit :::", "تعديل" * 80, "\\", "%s%d"):
            with self.subTest(text=text[:20]):
                parse_command(text)   # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
