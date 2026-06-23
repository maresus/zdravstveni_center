from __future__ import annotations

import re
from typing import Literal

ChangeAction = Literal["reschedule", "cancel"]


_RESCHEDULE_PATTERNS = (
    "drug termin",
    "nov termin",
    "drug datum",
)

_CANCEL_PATTERNS = (
    "odpovej",
    "odpoved",
    "preklic",
    "preklici",
    "preklicem",
    "cancel",
    "ne pridem",
    "odjav",
)

_APPOINTMENT_HINTS = (
    "termin",
    "pregled",
    "narocilo",
    "naročilo",
    "rezervacij",
    "dogovorjen",
    "dogovorjen termin",
)

_RESCHEDULE_VERB_RE = re.compile(r"\b(prestav\w*|premakn\w*|spremeni\w*|spremenil\w*|spremenim\w*)\b")
_CANCEL_VERB_RE = re.compile(r"\b(odpovej\w*|odpoved\w*|preklic\w*|cancel|odjav\w*)\b")


def detect_appointment_change_request(message: str) -> ChangeAction | None:
    lowered = (message or "").lower().strip()
    if not lowered:
        return None

    has_hint = any(h in lowered for h in _APPOINTMENT_HINTS)
    has_reschedule_phrase = any(p in lowered for p in _RESCHEDULE_PATTERNS)
    has_reschedule_verb = bool(_RESCHEDULE_VERB_RE.search(lowered))
    has_cancel = any(p in lowered for p in _CANCEL_PATTERNS) or bool(_CANCEL_VERB_RE.search(lowered))

    # High-confidence reschedule: change verb + appointment context.
    if has_reschedule_verb and has_hint:
        return "reschedule"
    # Phrase-based reschedule requires stronger evidence of existing appointment.
    if has_reschedule_phrase and any(h in lowered for h in ("termin", "dogovorjen", "rezervacij", "naročilo", "naročilo", "pregled")):
        if any(x in lowered for x in ("prestav", "premakn", "spremen")) or any(x in lowered for x in ("imam", "že", "ze", "dogovorjen", "prestavil", "prestavim")):
            return "reschedule"
    if has_cancel and has_hint:
        return "cancel"

    # A few high-confidence short forms used in real chats / SMS-ish inputs.
    if re.fullmatch(r"\s*(prestav|prestavi)\s*", lowered):
        return "reschedule"
    if re.fullmatch(r"\s*(odpovej|odpoved|cancel)\s*", lowered):
        return "cancel"
    return None


def build_appointment_change_reply(action: ChangeAction, *, phone: str | None, email: str | None) -> str:
    phone_txt = phone or "04 271 30 10"
    email_part = f" ali pišite na {email}" if email else ""
    if action == "reschedule":
        return (
            "Prestavitev termina trenutno ni možna preko klepeta. "
            f"Za prestavitev prosim pokličite na {phone_txt}{email_part}."
        )
    return (
        "Odpoved termina trenutno ni možna preko klepeta. "
        f"Za odpoved prosim pokličite na {phone_txt}{email_part}."
    )
