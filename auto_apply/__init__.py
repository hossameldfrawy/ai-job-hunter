"""
Phase 2: auto-apply and interview copilot.

    candidate.py       CV -> structured profile (cached, Gemini-extracted)
    profile_builder.py assisted account creation + profile completion
    engine.py          draft -> human approval -> submit -> evidence
    review.py          the review card, per channel, and the in-line edit engine
    commands.py        the Arabic/English reply grammar ("done 7", "تعديل ...")
    control.py         what a reply DOES, plus the Saved-Messages listener
    email_listener.py  inbox monitor for interview invitations

Everything here is LOCAL-ONLY by design. It needs a real browser and it writes
to a vault that is never synced to the public repo, so it does not run on the
GitHub Actions schedule -- that stays a pure discovery pipeline.
"""

from __future__ import annotations

__all__ = [
    "candidate", "profile_builder", "engine", "review", "commands", "control",
    "email_listener",
]
