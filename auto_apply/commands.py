"""
The conversational command grammar: one line of Arabic or English in, one
structured `Command` out.

WHY THIS IS A SEPARATE, PURE MODULE
-----------------------------------
The listener that feeds it reads the user's Telegram Saved Messages -- a real
private notebook, not a bot inbox. Two things follow from that, and both are
correctness properties rather than polish:

  1. MOST MESSAGES ARE NOT COMMANDS. A shopping list, a pasted link, a note to
     self. Anything this parser does not RECOGNISE must come back as
     `unknown`, and the listener stays silent. A parser that guesses would turn
     a private notebook into a bot that argues with its owner.

  2. A FALSE POSITIVE SUBMITS A JOB APPLICATION IN SOMEONE'S NAME. So the
     approve/decline/status/help forms are matched with `fullmatch` against a
     closed vocabulary: "done", "done 7", "موافق 7" and nothing looser. The
     word "done" inside a sentence is a sentence, not an approval.

Keeping the grammar pure -- no database, no network, no Telethon -- is what
lets every one of those cases be tested exhaustively without a browser or an
MTProto session.

WHAT IT UNDERSTANDS
-------------------
    done | done 7 | ok 7 | approve #7 | موافق | اعتمد 7 | تم
    decline 7 | discard | رفض 7 | الغاء
    edit 7 cover letter: <text>      تعديل 7 خطاب التغطية: <نص>
    edit salary: 15000 AED           تعديل الراتب: 15000
    edit 7 answer 2: <text>          تعديل 7 الاجابة 2: <نص>
    edit: <free-form instruction>    تعديل: <تفاصيل>
    status | drafts | الحالة
    help | مساعدة
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from auto_apply.review import (
    FIELD_ANSWER, FIELD_COVER_LETTER, FIELD_EXPERIENCE, FIELD_LOCATION,
    FIELD_NOTE, FIELD_NOTICE, FIELD_PHONE, FIELD_SALARY,
)

#: actions
ACTION_APPROVE = "approve"
ACTION_EDIT = "edit"
ACTION_DECLINE = "decline"
ACTION_STATUS = "status"
ACTION_HELP = "help"
ACTION_UNKNOWN = "unknown"


@dataclass(slots=True)
class Command:
    """One parsed instruction. `action == "unknown"` means: not for us."""

    action: str = ACTION_UNKNOWN
    draft_id: int | None = None
    field: str = ""
    value: str = ""
    answer_index: int | None = None
    raw: str = ""
    language: str = "en"

    @property
    def recognised(self) -> bool:
        return self.action != ACTION_UNKNOWN


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
#: Arabic-Indic (٠-٩) and Extended Arabic-Indic (۰-۹) digits. A phone keyboard
#: set to Arabic produces these, so "موافق ٧" must resolve to draft 7 -- and it
#: is exactly the message an Arabic-speaking user is most likely to send.
_DIGIT_MAP = {
    **{chr(0x0660 + i): str(i) for i in range(10)},
    **{chr(0x06F0 + i): str(i) for i in range(10)},
}

#: Colon variants that appear in Arabic and CJK keyboards. The colon is the
#: separator between a field name and its value, so missing one of these turns
#: a perfectly good edit into an unrecognised message.
_COLONS = {"：": ":", "∶": ":", "۔": ":"}

_ARABIC_FOLD = str.maketrans({
    "ٱ": "ا",   # alef wasla    -> alef
    "ة": "ه",   # teh marbuta   -> heh
    "ى": "ي",   # alef maksura  -> yeh
    "ـ": "",         # tatweel is decoration
})

_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Fold the surface variation a human keyboard produces.

    Digits, colons, Arabic orthography and whitespace only. Case and content
    are left alone -- the VALUE half of an edit is copied verbatim, and folding
    it would silently rewrite the cover letter the user just typed.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", str(text))
    out = "".join(_DIGIT_MAP.get(ch, _COLONS.get(ch, ch)) for ch in out)
    return out.strip()


def _fold(token: str) -> str:
    """Aggressive fold, for matching a keyword against the alias tables."""
    text = unicodedata.normalize("NFKD", token or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.translate(_ARABIC_FOLD).casefold()
    text = re.sub(r"[^\w؀-ۿ ]+", " ", text)
    return _WS.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
#: Closed vocabularies. Anything not in one of these is not a command.
APPROVE_WORDS = (
    "done", "ok", "okay", "go", "send", "submit", "approve", "approved",
    "confirm", "yes", "apply",
    "موافق",              # موافق
    "موافقة",        # موافقة
    "اعتمد",              # اعتمد
    "تم",                                # تم
    "تمام",                    # تمام
    "وافق",                    # وافق
    "نعم",                          # نعم
    "ارسل",                    # ارسل
    "ارسال",              # ارسال
    "قدم",                          # قدم
    "اوك",                          # اوك
)

DECLINE_WORDS = (
    "decline", "discard", "reject", "cancel", "drop", "no", "skip",
    "رفض",                          # رفض
    "ارفض",                    # ارفض
    "الغاء",              # الغاء
    "تجاهل",              # تجاهل
    "لا",                                # لا
    "حذف",                          # حذف
)

STATUS_WORDS = (
    "status", "drafts", "draft", "list", "pending", "ls", "queue",
    "الحالة",        # الحالة
    "حالة",                    # حالة
    "المسودات",   # المسودات
    "القائمة",  # القائمة
    "عرض",                          # عرض
)

HELP_WORDS = (
    "help", "commands", "?", "usage",
    "مساعدة",        # مساعدة
    "المساعدة",   # المساعدة
    "الاوامر",  # الاوامر
    "اوامر",              # اوامر
)

EDIT_WORDS = (
    "edit", "change", "update", "modify", "revise", "set", "rewrite", "fix",
    "تعديل",              # تعديل
    "عدل",                          # عدل
    "غير",                          # غير
    "تغيير",              # تغيير
    "تحديث",              # تحديث
    "صحح",                          # صحح
)

#: field alias -> canonical field key. Keys are stored folded.
_FIELD_ALIASES: dict[str, str] = {}


def _register(field: str, *aliases: str) -> None:
    for alias in aliases:
        _FIELD_ALIASES[_fold(alias)] = field


_register(
    FIELD_COVER_LETTER,
    "cover letter", "cover_letter", "coverletter", "cover", "letter",
    "motivation", "motivation letter", "message", "body", "text",
    "خطاب",                                  # خطاب
    "الخطاب",                      # الخطاب
    "خطاب التغطية",  # خطاب التغطية
    "الخطاب التعريفي",
    "رسالة",                            # رسالة
    "الرسالة",                # الرسالة
    "النص",                                  # النص
)
_register(
    FIELD_SALARY,
    "salary", "pay", "wage", "expected salary", "salary expectation",
    "expected pay", "compensation", "package",
    "راتب",                                  # راتب
    "الراتب",                      # الراتب
    "الاجر",                            # الاجر
    "المرتب",                      # المرتب
    "الراتب المتوقع",
)
_register(
    FIELD_EXPERIENCE,
    "experience", "years", "years of experience", "years experience", "exp",
    "خبرة",                                  # خبرة
    "الخبرة",                      # الخبرة
    "سنوات الخبرة",  # سنوات الخبرة
)
_register(
    FIELD_NOTICE,
    "notice", "notice period", "availability", "start date", "joining",
    "فترة الاشعار",  # فترة الاشعار
    "الاشعار",                # الاشعار
    "موعد البدء",   # موعد البدء
    "التوفر",                      # التوفر
)
_register(
    FIELD_PHONE,
    "phone", "mobile", "number", "phone number", "whatsapp",
    "الهاتف",                      # الهاتف
    "الجوال",                      # الجوال
    "الموبايل",          # الموبايل
    "رقم الهاتف",   # رقم الهاتف
)
_register(
    FIELD_LOCATION,
    "location", "city", "address",
    "الموقع",                      # الموقع
    "المدينة",                # المدينة
    "العنوان",                # العنوان
)

#: "answer 2", "q2", "الاجابة 2" -- an answer alias always carries its number.
_ANSWER_RE = re.compile(
    r"^(?:answer|ans|question|q|"
    r"الاجابه|اجابه|"
    r"جواب|السوال|"
    r"سوال)"
    r"\s*#?\s*(?P<n>\d+)$"
)

_ARABIC_CHARS = re.compile(r"[؀-ۿ]")


def _alternation(words: tuple[str, ...]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


# A bare verb, optionally followed by a draft id. `fullmatch` only: the word
# "done" inside a sentence is a sentence, not an approval.
_APPROVE_RE = re.compile(
    rf"(?:{_alternation(APPROVE_WORDS)})\s*(?:#\s*)?(\d+)?", re.I)
_DECLINE_RE = re.compile(
    rf"(?:{_alternation(DECLINE_WORDS)})\s*(?:#\s*)?(\d+)?", re.I)
_STATUS_RE = re.compile(rf"(?:{_alternation(STATUS_WORDS)})", re.I)
_HELP_RE = re.compile(rf"(?:{_alternation(HELP_WORDS)})", re.I)

# The edit verb, an optional draft id, then everything else.
#
# The verb must END where it says it does. `\b` is unreliable next to Arabic
# script, so the boundary is written out as "whitespace, or a character that can
# only begin the rest of the command, or end of message". Without it "editor"
# and "settings for tomorrow" would parse as edit commands.
_EDIT_RE = re.compile(
    rf"^(?:{_alternation(EDIT_WORDS)})(?:\s+|(?=[#\d:：])|$)\s*"
    rf"(?:#\s*)?(?P<id>\d+)?\s*(?P<rest>.*)$",
    re.I | re.S,
)

#: Bracketing the docs use for a placeholder -- "edit 1 cover letter: [new
#: text]". Users copy the example verbatim, brackets and all.
_WRAPPERS = (("[", "]"), ("<", ">"), ("«", "»"), ('"', '"'),
             ("'", "'"), ("“", "”"), ("`", "`"))


def strip_wrapper(value: str) -> str:
    """Remove one matched pair of placeholder brackets or quotes."""
    value = value.strip()
    for opener, closer in _WRAPPERS:
        if len(value) >= 2 and value.startswith(opener) and value.endswith(closer):
            return value[1:-1].strip()
    return value


def _language(text: str) -> str:
    return "ar" if _ARABIC_CHARS.search(text or "") else "en"


def resolve_field(token: str) -> tuple[str, int | None]:
    """Map a written field name onto a canonical key. ("", None) if unknown."""
    folded = _fold(token)
    if not folded:
        return "", None

    match = _ANSWER_RE.match(folded)
    if match:
        return FIELD_ANSWER, int(match.group("n"))

    if folded in _FIELD_ALIASES:
        return _FIELD_ALIASES[folded], None
    # Arabic writes the definite article as a prefix, and users drop it freely:
    # "الراتب" and "راتب" are the same request.
    if folded.startswith("ال") and folded[2:] in _FIELD_ALIASES:
        return _FIELD_ALIASES[folded[2:]], None
    return "", None


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------
def parse_command(text: str) -> Command:
    """Parse one message. Never raises; unrecognised input is `unknown`."""
    raw = str(text or "")
    line = normalise(raw)
    if not line:
        return Command(raw=raw)

    language = _language(raw)

    # The verb forms are matched against the WHOLE message, collapsed to one
    # line -- never against its first line alone. "done\nremember to call the
    # recruiter" is a note to self, and reading only the first line of it
    # submits a job application. An edit is the one exception, because its
    # value is legitimately multi-line: a pasted cover letter.
    single = _WS.sub(" ", line).strip().strip(".!،,")
    if _HELP_RE.fullmatch(single):
        return Command(ACTION_HELP, raw=raw, language=language)
    if _STATUS_RE.fullmatch(single):
        return Command(ACTION_STATUS, raw=raw, language=language)

    match = _APPROVE_RE.fullmatch(single)
    if match:
        return Command(ACTION_APPROVE, _int_or_none(match.group(1)),
                       raw=raw, language=language)

    match = _DECLINE_RE.fullmatch(single)
    if match:
        return Command(ACTION_DECLINE, _int_or_none(match.group(1)),
                       raw=raw, language=language)

    match = _EDIT_RE.match(line)
    if match:
        return _parse_edit(match, raw, language)

    return Command(raw=raw, language=language)


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_edit(match: re.Match[str], raw: str, language: str) -> Command:
    draft_id = _int_or_none(match.group("id"))
    rest = (match.group("rest") or "").strip()

    if not rest:
        # "edit" or "edit 7" with nothing after it: a request for the syntax,
        # not a malformed edit. Answering with help is more useful than an
        # error, and it cannot damage a draft.
        return Command(ACTION_HELP, draft_id, raw=raw, language=language)

    # Split on the FIRST colon only: a cover letter is full of colons, and
    # splitting on the last would swallow the letter into the field name.
    if ":" in rest:
        head, value = rest.split(":", 1)
        head = head.strip()
        field, answer_index = resolve_field(head)
        value = strip_wrapper(value)
        if field:
            return Command(ACTION_EDIT, draft_id, field, value, answer_index,
                           raw=raw, language=language)
        # An unrecognised head, e.g. "edit 7: make it shorter and mention
        # Asterisk". The whole thing is the instruction, head included --
        # dropping it would silently lose the first clause of what was asked.
        # When there is no head at all ("تعديل: اجعله اقصر") the colon is just
        # punctuation and only the value survives.
        instruction = strip_wrapper(rest if head else value)
        return Command(ACTION_EDIT, draft_id, FIELD_NOTE, instruction,
                       raw=raw, language=language)

    # No colon. Either a bare field name (ask for the value) or free-form.
    field, answer_index = resolve_field(rest)
    if field:
        return Command(ACTION_EDIT, draft_id, field, "", answer_index,
                       raw=raw, language=language)
    return Command(ACTION_EDIT, draft_id, FIELD_NOTE, strip_wrapper(rest),
                   raw=raw, language=language)


# ---------------------------------------------------------------------------
# Help text -- the reply to `help` / `مساعدة`, and the listener's banner
# ---------------------------------------------------------------------------
HELP_TEXT = """\
\U0001F916 *AI JOB HUNTER — REVIEW COMMANDS*

✅ APPROVE & SUBMIT
   done            — submit the newest pending draft
   done 7          — submit draft #7
   موافق 7  /  اعتمد 7

✏️ EDIT, THEN RE-CONFIRM
   edit 7 cover letter: <new text>
   edit 7 salary: 15000 AED
   edit 7 experience: 3 years
   edit 7 answer 2: <new answer>
   edit 7: make it shorter and mention Asterisk
   تعديل 7 الراتب: 15000
   تعديل 7 خطاب التغطية: <النص الجديد>
   تعديل: اجعله اقصر

❌ DISCARD
   decline 7  /  رفض 7

\U0001F4CB OTHER
   status  /  الحالة   — what is waiting for you
   help    /  مساعدة   — this message

An edit always returns the draft to *review_pending*, so nothing is
submitted until you reply *done* to the version you actually read."""
