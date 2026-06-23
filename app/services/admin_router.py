import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Header, Request, Depends
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from app.services.email_service import (
    send_custom_message,
    send_reservation_confirmed,
    send_reservation_rejected,
)
from app.services.sms_service import (
    send_booking_received_sms,
    send_cancellation_sms,
    send_confirmation_sms,
    send_sms,
)
from app.services.reservation_service import ReservationService
from app.services.chat_history_service import get_chat_history_service
from app.services.admin_audit import log_admin_action, list_admin_audit
from app.services.clinic_kb_service import get_clinic_info, update_clinic_info
from app.services.clinic_config import (
    get_clinic_config,
    list_available_clinics,
    resolve_clinic_id,
    set_current_clinic_id,
    reset_current_clinic_id,
)
from app.services.session.store import get_session_store

# Compatibility aliases za admin panel
TOTAL_TABLE_CAPACITY = 0  # Ni miz

# Veljavne vrste pregledov/posegov za zdravstveni center (za validacijo)
VALID_TABLE_LOCATIONS = {
    "MR glave in vratu",
    "MR hrbtenice (ledveni del)",
    "MR hrbtenice (vratni del)",
    "MR hrbtenice (prsni del)",
    "MR kolena",
    "MR ramenskega sklepa",
    "MR prostate",
    "MR dojk",
    "MR male medenice",
    "MR zgornjega abdomna",
    "MR enterografija",
    "MR urografija",
    "MR angiografija",
    "MR artrografija",
    "RTG hrbtenice",
    "RTG ramenskega sklepa",
    "RTG kolena",
    "RTG gležnja",
    "RTG roke/zapestja",
    "RTG prsnega koša",
    "UZ trebuha",
    "UZ ščitnice",
    "UZ dojk",
    "UZ sečil",
    "UZ sklepov",
    "UZ vodeni poseg — izpiranje kalcifikacij",
    "UZ vodeni poseg — aplikacija kortikosteroida",
    "UZ vodeni poseg — izpraznitev ciste",
    "Ambulanta za bolezni ščitnice",
}
from app.services.imap_poll_service import load_state, preview_last_messages, resync_last_messages

def _auth_enabled() -> bool:
    return any(
        os.getenv(var)
        for var in ("ADMIN_TOKEN", "ADMIN_READ_TOKEN", "ADMIN_WRITE_TOKEN", "STRICT_ADMIN_AUTH")
    )


def _resolve_clinic_from_request(
    request: Request,
    x_clinic_id: Optional[str],
) -> str:
    strict_clinic = os.getenv("STRICT_CLINIC_ID", "false").strip().lower() in {"1", "true", "yes", "on"}
    try:
        return resolve_clinic_id(x_clinic_id or request.query_params.get("clinic_id"), strict=strict_clinic)
    except ValueError:
        available = list_available_clinics()
        raise HTTPException(status_code=400, detail={"error": "unknown_clinic_id", "available": available})


def admin_context(
    request: Request,
    x_clinic_id: Optional[str] = Header(None, alias="X-Clinic-Id"),
):
    clinic_id = _resolve_clinic_from_request(request, x_clinic_id)
    token = set_current_clinic_id(clinic_id)
    try:
        yield clinic_id
    finally:
        reset_current_clinic_id(token)


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()


def _resolve_role(authorization: Optional[str]) -> Optional[str]:
    token = _extract_bearer(authorization)
    if not token:
        return None

    admin_token = os.getenv("ADMIN_TOKEN")
    write_token = os.getenv("ADMIN_WRITE_TOKEN")
    read_token = os.getenv("ADMIN_READ_TOKEN")

    if admin_token and token == admin_token:
        return "admin"
    if write_token and token == write_token:
        return "editor"
    if read_token and token == read_token:
        return "viewer"
    return None


def require_read(
    authorization: Optional[str] = Header(None),
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    clinic_id: str = Depends(admin_context),
) -> str:
    if not _auth_enabled():
        return "admin"

    provided = _extract_bearer(x_admin_token or authorization)

    # 1) Environment tokens always accepted (ADMIN_TOKEN / READ / WRITE)
    role = _resolve_role(x_admin_token or authorization)
    if role:
        return role

    # 2) Clinic-scoped fallback token (auth.admin_api_key)
    config = get_clinic_config(clinic_id=clinic_id)
    auth_cfg = config.get("auth", {}) if isinstance(config, dict) else {}
    expected = auth_cfg.get("admin_api_key")
    if expected and provided and provided == expected:
        return "admin"

    # 3) Strict mode: require one of the valid tokens above
    strict_admin = os.getenv("STRICT_ADMIN_AUTH", "false").strip().lower() in {"1", "true", "yes", "on"}
    if strict_admin:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Non-strict mode: allow read access even without token.
    return "admin"


def require_write(role: str = Depends(require_read)) -> str:
    if role == "viewer":
        raise HTTPException(status_code=403, detail="Insufficient role")
    return role


def _get_actor(request: Request) -> str:
    return (
        request.headers.get("X-Admin-User")
        or os.getenv("ADMIN_EMAIL")
        or "admin"
    )


def _get_ip(request: Request) -> Optional[str]:
    try:
        return request.client.host if request.client else None
    except Exception:
        return None


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_read)])
service = ReservationService()


@router.get("/health/session-store")
def session_store_health() -> dict[str, Any]:
    store = get_session_store()
    backend = "memory"
    redis_enabled = False
    if store.__class__.__name__ == "RedisSessionStore":
        backend = "redis"
        redis_enabled = bool(getattr(store, "_enabled", False))
    return {
        "backend": backend,
        "redis_enabled": redis_enabled,
        "store_class": store.__class__.__name__,
    }

def _current_room_ids() -> set[str]:
    try:
        return {r.get("id") for r in service._rooms() if r.get("id")}
    except Exception:
        return set()


def _log(event: str, **kwargs) -> None:
    """Preprost log za admin API klice."""
    try:
        ts = datetime.now().isoformat(timespec="seconds")
        extras = " ".join([f"{k}={v}" for k, v in kwargs.items() if v is not None])
        print(f"[ADMIN API] {ts} {event} {extras}")
    except Exception:
        # Logging nesme prekiniti requesta
        pass


def _ensure_subject_tag(reservation_id: Optional[int], subject: str) -> str:
    if not reservation_id:
        return subject or ""
    tag = f"Rezervacija #{reservation_id}"
    if tag.lower() in (subject or "").lower():
        return subject
    if subject:
        return f"{tag} - {subject}"
    return tag


def _normalize_room_id(room: Optional[str]) -> Optional[str]:
    if not room:
        return None
    upper = room.strip().upper()
    for rid in _current_room_ids():
        if rid in upper or upper in rid:
            return rid
    return None


def _parse_ddmmyyyy(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str.strip(), "%d.%m.%Y")
    except Exception:
        return None


def _reservation_days(date_str: str, nights: Optional[int]) -> list[datetime]:
    nights_int = 1
    try:
        nights_int = int(nights or 1)
    except Exception:
        # poskusi izvleči prvo število iz niza (npr. "5 noči")
        import re

        m = re.search(r"\d+", str(nights or ""))
        if m:
            try:
                nights_int = int(m.group(0))
            except Exception:
                nights_int = 1
    if nights_int <= 0:
        nights_int = 1
    start = _parse_ddmmyyyy(date_str)
    if not start:
        return []
    return [start + timedelta(days=i) for i in range(nights_int)]


def _room_conflicts(reservation_id: int, room_id: str, date_str: str, nights: Optional[int]) -> list[str]:
    """Vrne seznam datumov (dd.mm.yyyy) kjer je soba že zasedena."""
    occupied: list[str] = []
    days = _reservation_days(date_str, nights)
    if not days:
        return occupied
    other_reservations = service.read_reservations(limit=1000, reservation_type="room")
    for r in other_reservations:
        if r.get("id") == reservation_id:
            continue
        status = r.get("status")
        if status not in {"confirmed", "processing"}:
            continue
        other_room = _normalize_room_id(r.get("location"))
        if other_room != room_id:
            continue
        other_days = _reservation_days(r.get("date", ""), r.get("nights"))
        overlaps = {d.date() for d in days} & {d.date() for d in other_days}
        if overlaps:
            occupied.extend(sorted({d.strftime("%d.%m.%Y") for d in overlaps}))
    return occupied


class ReservationUpdate(BaseModel):
    status: Optional[str] = None
    date: Optional[str] = None
    people: Optional[int] = None
    nights: Optional[int] = None
    time: Optional[str] = None
    time_window: Optional[str] = None
    location: Optional[str] = None
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    admin_notes: Optional[str] = None
    note: Optional[str] = None
    kids_small: Optional[str] = None
    kids: Optional[str] = None
    event_type: Optional[str] = None
    special_needs: Optional[str] = None
    birth_date: Optional[str] = None


class SendMessageRequest(BaseModel):
    reservation_id: int
    email: str
    subject: str
    body: str
    set_processing: bool = True


class SendSmsRequest(BaseModel):
    reservation_id: int
    phone: str
    body: str
    set_processing: bool = True


class ConfirmReservationRequest(BaseModel):
    room: Optional[str] = None
    location: Optional[str] = None


class AdminCreateReservation(BaseModel):
    date: str
    people: int
    reservation_type: str
    source: str = "admin"
    nights: Optional[int] = None
    rooms: Optional[int] = None
    time: Optional[str] = None
    location: Optional[str] = None
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    note: Optional[str] = None
    admin_notes: Optional[str] = None
    kids: Optional[str] = None
    kids_small: Optional[str] = None
    event_type: Optional[str] = None
    special_needs: Optional[str] = None
    birth_date: Optional[str] = None
    time_window: Optional[str] = None


class KnowledgeFeedbackRequest(BaseModel):
    question: str
    suggestion: str


# HTML page endpoints removed - these are now served from main.py
# The admin router should only contain API endpoints

@router.get("/conversations")
def get_conversations(limit: int = 200, needs_followup_only: bool = False):
    """Vrne zadnje pogovore za admin pregled."""
    _log("conversations", limit=limit, needs_followup_only=needs_followup_only)
    conversations = service.get_conversations(limit=limit, needs_followup_only=needs_followup_only)
    stats = {
        "total": len(conversations),
        "followup": len([c for c in conversations if c.get("needs_followup")]),
    }
    return {"conversations": conversations, "stats": stats}


@router.get("/conversations/session/{session_id}")
def get_conversations_by_session(session_id: str, limit: int = 200):
    """Vrne pogovor za posamezen session_id."""
    _log("conversations_session", session_id=session_id, limit=limit)
    conversations = service.get_conversations_by_session(session_id=session_id, limit=limit)
    return {"session_id": session_id, "conversations": conversations, "total": len(conversations)}


@router.get("/inquiries")
def get_inquiries(limit: int = 200, status: Optional[str] = None):
    _log("inquiries", limit=limit, status=status)
    inquiries = service.get_inquiries(limit=limit, status=status)
    return {"inquiries": inquiries}


@router.get("/usage_stats")
def get_usage_stats():
    """Usage stats from chat_messages table"""
    _log("usage_stats")
    history_service = get_chat_history_service()

    # Get stats for different time periods
    today = datetime.now().strftime("%Y-%m-%d")
    month = datetime.now().strftime("%Y-%m")
    year = datetime.now().strftime("%Y")

    today_sessions = history_service.get_all_sessions(since=f"{today}T00:00:00", limit=1000)
    month_sessions = history_service.get_all_sessions(since=f"{month}-01T00:00:00", limit=1000)
    year_sessions = history_service.get_all_sessions(since=f"{year}-01-01T00:00:00", limit=1000)

    # Get total sessions count
    stats = history_service.get_conversation_stats(days=7)
    total_sessions = stats.get("total_sessions", 0)
    booking_sessions = stats.get("booking_sessions", 0)
    conversion_rate = f"{stats.get('conversion_rate', 0)}%" if total_sessions > 0 else "0%"

    return {
        "total_sessions": total_sessions,
        "today": len(today_sessions),
        "month": len(month_sessions),
        "year": len(year_sessions),
        "conversion_rate": conversion_rate,
        "average_duration": "-"
    }


@router.get("/question_stats")
def get_question_stats(limit: int = 10):
    """Top questions from chat_messages table"""
    _log("question_stats", limit=limit)
    history_service = get_chat_history_service()

    # Get conversation stats which includes top_intents
    stats = history_service.get_conversation_stats(days=30)
    top_intents = stats.get("top_intents", [])

    # Format as questions (use intent as question text)
    questions = []
    for item in top_intents[:limit]:
        intent = item.get("intent", "unknown")
        count = item.get("count", 0)
        questions.append({
            "question": intent,
            "count": count
        })

    return {"questions": questions}


@router.get("/lost_intents")
def get_lost_intents(limit: int = 10):
    """Lost intents from chat_messages (questions without clear intent)"""
    _log("lost_intents", limit=limit)
    history_service = get_chat_history_service()

    # Search for "question" intents (generic questions that didn't match specific intents)
    messages = history_service.search_messages(
        query="",  # Empty query to get all
        role="user",
        intent="question",
        limit=limit
    )

    items = []
    for msg in messages:
        items.append({
            "text": msg.get("content", "")[:100],
            "count": 1  # Individual messages don't have count
        })

    return {"items": items}


@router.get("/funnel_stats")
def get_funnel_stats(days: int = 30):
    """Conversion funnel from chat_messages"""
    _log("funnel_stats", days=days)
    history_service = get_chat_history_service()

    stats = history_service.get_conversation_stats(days=days)

    total_sessions = stats.get("total_sessions", 0)
    booking_sessions = stats.get("booking_sessions", 0)

    # Estimate started (sessions with booking intent)
    # For now, use booking_sessions as "started"
    # completed = booking_sessions (since we only track confirmed bookings)

    return {
        "started": total_sessions,
        "completed": booking_sessions,
        "confirmed": booking_sessions
    }


@router.get("/missed_questions")
def get_missed_questions(limit: int = 5):
    """Missed questions (same as lost_intents for now)"""
    _log("missed_questions", limit=limit)
    return get_lost_intents(limit=limit)


@router.post("/knowledge_feedback")
def create_knowledge_feedback(
    payload: KnowledgeFeedbackRequest,
    request: Request,
    role: str = Depends(require_write),
):
    _log("knowledge_feedback", question=payload.question[:60] if payload.question else "")
    feedback_id = service.create_knowledge_feedback(payload.question.strip(), payload.suggestion.strip())
    if not feedback_id:
        raise HTTPException(status_code=400, detail="Neveljaven predlog.")
    log_admin_action(
        action="knowledge_feedback",
        actor=_get_actor(request),
        role=role,
        ip=_get_ip(request),
        details={"question": payload.question[:120], "suggestion": payload.suggestion[:120]},
    )
    return {"ok": True, "id": feedback_id}


@router.get("/reservations")
def get_reservations(
    limit: int = 100,
    status: Optional[str] = None,
    type: Optional[str] = None,
    source: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """Vrne seznam rezervacij s filtri ter osnovno statistiko."""
    _log("reservations", limit=limit, status=status, type=type, source=source, date_from=date_from, date_to=date_to)
    reservations = service.read_reservations(limit=limit, status=status, reservation_type=type, source=source)

    def _parse_date(date_str: str) -> Optional[datetime]:
        if not date_str:
            return None
        date_str = date_str.replace(" ", "")
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
            try:
                return datetime.strptime(date_str, fmt)
            except (ValueError, TypeError):
                continue
        return None

    if date_from or date_to:
        start = _parse_date(date_from) if date_from else None
        end = _parse_date(date_to) if date_to else None
        filtered = []
        for r in reservations:
            days = _reservation_days(r.get("date", ""), r.get("nights"))
            if not days:
                # če ni datuma, ga obdržimo (ne izločimo)
                filtered.append(r)
                continue
            overlaps = False
            for d in days:
                if start and d < start:
                    continue
                if end and d > end:
                    continue
                overlaps = True
                break
            if overlaps:
                filtered.append(r)
        reservations = filtered

    all_res = service.read_reservations(limit=1000)
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    stats = {
        "pending": len([r for r in all_res if r.get("status") == "pending"]),
        "processing": len([r for r in all_res if r.get("status") == "processing"]),
        "confirmed": len([r for r in all_res if r.get("status") == "confirmed"]),
        "today": len([r for r in all_res if str(r.get("created_at", "")).startswith(today_prefix)]),
    }

    return {"reservations": reservations, "stats": stats}


@router.put("/reservations/{reservation_id}")
def update_reservation(
    reservation_id: int,
    data: ReservationUpdate,
    request: Request,
    role: str = Depends(require_write),
):
    """Posodobi rezervacijo."""
    existing = service.get_reservation(reservation_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Rezervacija ni najdena")
    res_type = existing.get("reservation_type")
    location = data.location
    valid_rooms = {"", None, "ALJAZ", "JULIJA", "ANA"}
    if res_type == "room" and location is not None and location not in valid_rooms:
        raise HTTPException(status_code=400, detail="Neveljavna soba")
    if res_type == "table" and location is not None and location not in VALID_TABLE_LOCATIONS:
        raise HTTPException(status_code=400, detail="Neveljavna vrsta pregleda/posega")
    ok = service.update_reservation(
        reservation_id,
        status=data.status,
        date=data.date,
        people=data.people,
        nights=data.nights,
        location=data.location,
        admin_notes=data.admin_notes,
        kids=data.kids,
        kids_small=data.kids_small,
        time=data.time,
        time_window=data.time_window,
        name=data.name,
        phone=data.phone,
        email=data.email,
        note=data.note,
        event_type=data.event_type,
        special_needs=data.special_needs,
        birth_date=data.birth_date,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Rezervacija ni najdena")
    log_admin_action(
        action="reservation_update",
        reservation_id=reservation_id,
        actor=_get_actor(request),
        role=role,
        ip=_get_ip(request),
        details={"status": data.status, "date": data.date, "time": data.time},
    )
    return {"ok": True}


@router.patch("/reservations/{reservation_id}")
def patch_reservation(
    reservation_id: int,
    data: ReservationUpdate,
    request: Request,
    role: str = Depends(require_write),
):
    """Partial update rezervacije (status, admin_notes, kids)."""
    fields = {
        "status": data.status,
        "admin_notes": data.admin_notes,
        "kids": data.kids,
        "kids_small": data.kids_small,
        "time": data.time,
        "time_window": data.time_window,
        "location": data.location,
        "name": data.name,
        "phone": data.phone,
        "email": data.email,
        "note": data.note,
        "event_type": data.event_type,
        "special_needs": data.special_needs,
        "birth_date": data.birth_date,
    }
    if data.status == "confirmed":
        fields["confirmed_at"] = datetime.now().isoformat()
    ok = service.update_reservation(reservation_id, **fields)
    if not ok:
        raise HTTPException(status_code=404, detail="Rezervacija ni najdena")
    log_admin_action(
        action="reservation_patch",
        reservation_id=reservation_id,
        actor=_get_actor(request),
        role=role,
        ip=_get_ip(request),
        details={k: v for k, v in fields.items() if v is not None},
    )
    return {"ok": True}


@router.delete("/reservations/{reservation_id}")
def delete_reservation(
    reservation_id: int,
    request: Request,
    role: str = Depends(require_write),
):
    """Izbriši rezervacijo."""
    _log("delete_reservation", reservation_id=reservation_id)
    res = service.get_reservation(reservation_id)
    if not res:
        raise HTTPException(status_code=404, detail="Rezervacija ni najdena")
    ok = service.delete_reservation(reservation_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Napaka pri brisanju")
    log_admin_action(
        action="reservation_delete",
        reservation_id=reservation_id,
        actor=_get_actor(request),
        role=role,
        ip=_get_ip(request),
        details={"name": res.get("name"), "date": res.get("date"), "time": res.get("time")},
    )
    return {"ok": True, "deleted_id": reservation_id}


@router.delete("/reservations/all")
def delete_all_reservations():
    """Izbriše VSE rezervacije - za reset baze pred predajo stranki."""
    count = service.delete_all_reservations()
    return {"success": True, "deleted": count, "message": f"Izbrisanih {count} rezervacij"}


@router.post("/reservations/{reservation_id}/confirm")
def confirm_reservation(
    reservation_id: int,
    request: Request,
    data: Optional[ConfirmReservationRequest] = None,
    role: str = Depends(require_write),
):
    """Potrdi rezervacijo, preveri zasedenost sobe in pošlje email gostu."""
    res = service.get_reservation(reservation_id)
    if not res:
        raise HTTPException(status_code=404, detail="Rezervacija ni najdena")
    requested_room = _normalize_room_id((data.room if data else None) or res.get("location"))
    requested_location = (data.location if data else None) or res.get("location")
    if res.get("reservation_type") == "room":
        if not requested_room:
            raise HTTPException(status_code=400, detail="Soba mora biti izbrana.")
        conflicts = _room_conflicts(reservation_id, requested_room, res.get("date", ""), res.get("nights"))
        if conflicts:
            return {"success": False, "warning": f"Soba {requested_room} je zasedena: {', '.join(conflicts)}"}
    else:
        requested_room = None

    if res.get("reservation_type") == "table" and not res.get("time"):
        auto_time = service.pick_time_slot(
            res.get("date", ""),
            requested_location,
            res.get("time_window"),
        )
        if auto_time:
            res["time"] = auto_time

    service.update_reservation(
        reservation_id,
        status="confirmed",
        confirmed_at=datetime.now().isoformat(),
        confirmed_by=os.getenv("ADMIN_EMAIL", "info@kovacnik.com"),
        location=requested_room or requested_location,
        time=res.get("time"),
    )
    res = service.get_reservation(reservation_id) or res
    send_reservation_confirmed(res)
    sms_sent = False
    if res.get("phone"):
        sms_result = send_confirmation_sms(res)
        sms_sent = bool(sms_result.get("success"))
        if sms_sent:
            service.add_reservation_message(
                reservation_id=reservation_id,
                direction="outbound",
                channel="sms",
                subject="Potrditev termina (SMS)",
                body=f"Termin potrjen: {res.get('date', '')} ob {res.get('time', '')}",
                from_phone=os.getenv("TWILIO_PHONE_NUMBER", ""),
                to_phone=res.get("phone") or "",
                message_id=None,
                provider_message_sid=sms_result.get("message_sid"),
            )
    subject = _ensure_subject_tag(reservation_id, "Potrditev rezervacije")
    service.add_reservation_message(
        reservation_id=reservation_id,
        direction="outbound",
        subject=subject,
        body="Rezervacija potrjena.",
        from_email=os.getenv("ADMIN_EMAIL", "info@kovacnik.com"),
        to_email=res.get("email") or "",
        message_id=None,
    )
    if request:
        log_admin_action(
            action="reservation_confirm",
            reservation_id=reservation_id,
            actor=_get_actor(request),
            role=role,
            ip=_get_ip(request),
            details={"location": requested_room or requested_location, "time": res.get("time")},
        )
    return {"success": True, "email_sent": True, "sms_sent": sms_sent, "room": requested_room or requested_location}


@router.post("/reservations/{reservation_id}/reject")
def reject_reservation(
    reservation_id: int,
    request: Request,
    role: str = Depends(require_write),
):
    """Zavrne rezervacijo in pošlje email gostu."""
    res = service.get_reservation(reservation_id)
    if not res:
        raise HTTPException(status_code=404, detail="Rezervacija ni najdena")
    service.update_reservation(reservation_id, status="rejected")
    res = service.get_reservation(reservation_id) or res
    send_reservation_rejected(res)
    sms_sent = False
    if res.get("phone"):
        sms_result = send_cancellation_sms(res)
        sms_sent = bool(sms_result.get("success"))
        if sms_sent:
            service.add_reservation_message(
                reservation_id=reservation_id,
                direction="outbound",
                channel="sms",
                subject="Odpoved termina (SMS)",
                body=f"Termin odpovedan: {res.get('date', '')} ob {res.get('time', '')}",
                from_phone=os.getenv("TWILIO_PHONE_NUMBER", ""),
                to_phone=res.get("phone") or "",
                message_id=None,
                provider_message_sid=sms_result.get("message_sid"),
            )
    subject = _ensure_subject_tag(reservation_id, "Zavrnjena rezervacija")
    service.add_reservation_message(
        reservation_id=reservation_id,
        direction="outbound",
        subject=subject,
        body="Rezervacija zavrnjena.",
        from_email=os.getenv("ADMIN_EMAIL", "info@kovacnik.com"),
        to_email=res.get("email") or "",
        message_id=None,
    )
    log_admin_action(
        action="reservation_reject",
        reservation_id=reservation_id,
        actor=_get_actor(request),
        role=role,
        ip=_get_ip(request),
        details={"name": res.get("name"), "date": res.get("date")},
    )
    return {"success": True, "email_sent": True, "sms_sent": sms_sent}


@router.post("/send-message")
def send_message(
    data: SendMessageRequest,
    request: Request,
    role: str = Depends(require_write),
):
    """Pošlje sporočilo gostu in opcijsko status nastavi na 'processing'."""
    if not data.email:
        raise HTTPException(status_code=400, detail="Email manjka")
    subject = _ensure_subject_tag(data.reservation_id, data.subject or "")
    send_custom_message(data.email, subject, data.body)
    if data.reservation_id:
        service.add_reservation_message(
            reservation_id=data.reservation_id,
            direction="outbound",
            channel="email",
            subject=subject,
            body=data.body,
            from_email=os.getenv("ADMIN_EMAIL", "info@kovacnik.com"),
            to_email=data.email,
            message_id=None,
        )
    if data.set_processing:
        service.update_reservation(
            data.reservation_id,
            status="processing",
            guest_message=data.body,
        )
    log_admin_action(
        action="send_message",
        reservation_id=data.reservation_id,
        actor=_get_actor(request),
        role=role,
        ip=_get_ip(request),
        details={"email": data.email, "subject": subject},
    )
    return {"ok": True}


@router.post("/send-sms")
def send_sms_message(
    data: SendSmsRequest,
    request: Request,
    role: str = Depends(require_write),
):
    """Pošlje SMS pacientu in ga shrani v zgodovino rezervacije."""
    if not data.phone:
        raise HTTPException(status_code=400, detail="Telefon manjka")
    body = (data.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="SMS sporočilo je prazno")

    result = send_sms(data.phone, body)
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("error") or "Napaka pri pošiljanju SMS")

    if data.reservation_id:
        service.add_reservation_message(
            reservation_id=data.reservation_id,
            direction="outbound",
            channel="sms",
            subject="SMS odgovor",
            body=body,
            from_phone=os.getenv("TWILIO_PHONE_NUMBER", ""),
            to_phone=data.phone,
            message_id=None,
            provider_message_sid=result.get("message_sid"),
        )

    if data.set_processing:
        service.update_reservation(
            data.reservation_id,
            status="processing",
            guest_message=body,
        )

    log_admin_action(
        action="send_sms",
        reservation_id=data.reservation_id,
        actor=_get_actor(request),
        role=role,
        ip=_get_ip(request),
        details={"phone": data.phone, "mock": result.get("mock", False)},
    )
    return {"ok": True, "mock": result.get("mock", False), "message_sid": result.get("message_sid")}


@router.get("/reservations/{reservation_id}/messages")
def get_reservation_messages(reservation_id: int):
    """Vrne sporočila za izbrano rezervacijo."""
    messages = service.list_reservation_messages(reservation_id)
    return {"messages": messages}


@router.get("/imap_status")
def get_imap_status():
    """Vrne stanje IMAP pollinga."""
    return load_state()


@router.post("/imap_resync")
def imap_resync(
    request: Request,
    limit: int = 50,
    role: str = Depends(require_write),
):
    """Ročno prebere zadnjih N sporočil iz IMAP."""
    log_admin_action(
        action="imap_resync",
        actor=_get_actor(request),
        role=role,
        ip=_get_ip(request),
        details={"limit": limit},
    )
    return resync_last_messages(limit=limit)


@router.get("/imap_preview")
def imap_preview(limit: int = 10):
    """Vrne osnovne podatke zadnjih N sporočil (subject/from/date)."""
    return preview_last_messages(limit=limit)


@router.get("/stats")
def get_stats():
    """Agregirani podatki za dashboard."""
    _log("stats")
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    week_ago = datetime.now() - timedelta(days=7)
    month_ago = datetime.now().replace(day=1)
    res_list = service.read_reservations(limit=1000)

    def parse_created(r) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(str(r.get("created_at", "")))
        except Exception:
            return None

    counts = {
        "danes": 0,
        "ta_teden": 0,
        "ta_mesec": 0,
        "po_statusu": {"pending": 0, "processing": 0, "confirmed": 0, "rejected": 0},
        "po_tipu": {"room": 0, "table": 0},
    }
    for r in res_list:
        created = parse_created(r)
        if created:
            if str(r.get("created_at", "")).startswith(today_prefix):
                counts["danes"] += 1
            if created >= week_ago:
                counts["ta_teden"] += 1
            if created >= month_ago:
                counts["ta_mesec"] += 1
        status = r.get("status")
        if status in counts["po_statusu"]:
            counts["po_statusu"][status] += 1
        rtype = r.get("reservation_type")
        if rtype in counts["po_tipu"]:
            counts["po_tipu"][rtype] += 1
    return counts


@router.get("/audit")
def get_admin_audit(limit: int = 200, offset: int = 0):
    """Vrne admin audit trail."""
    _log("audit", limit=limit, offset=offset)
    return {"items": list_admin_audit(limit=limit, offset=offset)}


@router.get("/kb/clinic_info")
def get_kb_clinic_info():
    """Vrne urejene KB vsebine za clinic info."""
    _log("kb_clinic_info")
    return {"items": get_clinic_info()}


@router.put("/kb/clinic_info")
def update_kb_clinic_info(
    payload: dict[str, str],
    request: Request,
    role: str = Depends(require_write),
):
    _log("kb_clinic_info_update")
    items = update_clinic_info(payload)
    log_admin_action(
        action="kb_clinic_info_update",
        actor=_get_actor(request),
        role=role,
        ip=_get_ip(request),
        details={"keys": list(payload.keys())},
    )
    return {"ok": True, "items": items}


@router.get("/export")
def export_reservations(
    status: Optional[str] = None,
    type: Optional[str] = None,
    source: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """Izvoz rezervacij v CSV (uporabi iste filtre kot /reservations)."""
    data = get_reservations(limit=1000, status=status, type=type, source=source, date_from=date_from, date_to=date_to)
    reservations = data.get("reservations", [])
    headers = [
        "id",
        "date",
        "time",
        "nights",
        "rooms",
        "people",
        "kids",
        "kids_small",
        "reservation_type",
        "name",
        "email",
        "phone",
        "location",
        "note",
        "status",
        "source",
        "created_at",
    ]
    lines = [",".join(headers)]
    for r in reservations:
        row = []
        for h in headers:
            val = r.get(h, "")
            if val is None:
                val = ""
            cell = str(val).replace('"', '""')
            if any(c in cell for c in [",", "\n", '"']):
                cell = f'"{cell}"'
            row.append(cell)
        lines.append(",".join(row))
    csv_content = "\n".join(lines)
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=reservations.csv"},
    )


@router.get("/calendar/rooms")
def calendar_rooms(month: int, year: int):
    """Vrne zasedenost sob po dnevih z ločenimi pending/confirmed."""
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Neveljaven mesec")
    days: dict[str, dict[str, Any]] = {}
    reservations = service.read_reservations(limit=1000, reservation_type="room")
    for r in reservations:
        status = r.get("status")
        if status not in {"pending", "processing", "confirmed"}:
            continue
        room_id = _normalize_room_id(r.get("location"))
        if not room_id:
            continue
        for day in _reservation_days(r.get("date", ""), r.get("nights")):
            if day.month != month or day.year != year:
                continue
            key = day.strftime("%Y-%m-%d")
            bucket = "confirmed" if status == "confirmed" else "pending"
            entry = days.setdefault(key, {"confirmed": [], "pending": [], "reservations": []})
            if room_id not in entry[bucket]:
                entry[bucket].append(room_id)
            entry["reservations"].append(
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "people": r.get("people"),
                    "kids": r.get("kids"),
                    "location": room_id,
                    "email": r.get("email"),
                    "phone": r.get("phone"),
                    "status": status,
                    "date": r.get("date"),
                    "nights": r.get("nights"),
                }
            )
    return {"days": days}


@router.get("/calendar/tables")
def calendar_tables(month: int, year: int, location: Optional[str] = None):
    """Zasedenost miz po dnevih in urah."""
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Neveljaven mesec")
    calendar: dict[str, dict[str, Any]] = {}
    reservations = service.read_reservations(limit=1000, reservation_type="table")
    for r in reservations:
        status = r.get("status")
        if status in {"rejected", "cancelled"}:
            continue
        if location and r.get("location") and r.get("location") != location:
            continue
        day = _parse_ddmmyyyy(r.get("date", ""))
        if not day or day.month != month or day.year != year:
            continue
        iso = day.strftime("%Y-%m-%d")
        people = 0
        try:
            people = int(r.get("people") or 0)
        except Exception:
            people = 0
        entry = calendar.setdefault(
            iso, {"total_people": 0, "capacity": TOTAL_TABLE_CAPACITY, "reservations": []}
        )
        entry["total_people"] += people
        entry["reservations"].append(
            {
                "time": r.get("time"),
                "time_window": r.get("time_window"),
                "people": people,
                "name": r.get("name"),
                "status": status,
                "location": r.get("location"),
                "email": r.get("email"),
                "phone": r.get("phone"),
                "date": r.get("date"),
                "id": r.get("id"),
                "birth_date": r.get("birth_date"),
                "reservation_type": "table",
            }
        )
    return calendar


@router.post("/reservations")
def create_admin_reservation(
    data: AdminCreateReservation,
    request: Request,
    role: str = Depends(require_write),
):
    """Ročno dodajanje rezervacije (admin)."""
    warning: Optional[str] = None
    valid_rooms = {"", None, "ALJAZ", "JULIJA", "ANA"}
    location = _normalize_room_id(data.location) if data.reservation_type == "room" else data.location

    if data.reservation_type == "room":
        if location not in valid_rooms:
            raise HTTPException(status_code=400, detail="Neveljavna soba")
    if data.reservation_type == "table":
        if location and location not in VALID_TABLE_LOCATIONS:
            raise HTTPException(status_code=400, detail="Neveljavna vrsta pregleda/posega")

    if data.reservation_type == "room" and location:
        conflicts = _room_conflicts(0, location, data.date, data.nights)
        if conflicts:
            warning = f"Soba {location} je zasedena: {', '.join(conflicts)}"
    if data.reservation_type == "table" and data.time:
        ok, suggested_location, suggestions = service.check_table_availability(data.date, data.time, data.people)
        if not ok:
            warning = "Kapaciteta je polna za izbrano uro."
            if suggestions:
                warning += f" Predlogi: {', '.join(suggestions)}"
        if suggested_location and not data.location:
            location = suggested_location

    try:
        new_id = service.create_reservation(
            date=data.date,
            nights=data.nights,
            rooms=data.rooms,
            people=data.people,
            reservation_type=data.reservation_type,
            time=data.time,
            location=location,
            name=data.name,
            phone=data.phone,
            email=data.email,
            note=data.note,
            status="confirmed",
            admin_notes=data.admin_notes,
            kids=data.kids,
            kids_small=data.kids_small,
            source="admin",
            event_type=data.event_type,
            special_needs=data.special_needs,
            birth_date=data.birth_date,
            time_window=data.time_window,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Napaka pri shranjevanju: {exc}")

    sms_sent = False
    if data.phone:
        created = service.get_reservation(new_id)
        if created:
            sms_result = send_booking_received_sms(created)
            sms_sent = bool(sms_result.get("success"))
            if sms_sent:
                service.add_reservation_message(
                    reservation_id=new_id,
                    direction="outbound",
                    channel="sms",
                    subject="Naročilo prejeto (SMS)",
                    body=f"Naročilo prejeto: {created.get('date', '')} ob {created.get('time', '')}",
                    from_phone=os.getenv("TWILIO_PHONE_NUMBER", ""),
                    to_phone=created.get("phone") or "",
                    message_id=None,
                    provider_message_sid=sms_result.get("message_sid"),
                )
    log_admin_action(
        action="reservation_create",
        reservation_id=new_id,
        actor=_get_actor(request),
        role=role,
        ip=_get_ip(request),
        details={"name": data.name, "date": data.date, "time": data.time, "location": location},
    )
    return {"success": True, "id": new_id, "warning": warning, "sms_sent": sms_sent}


# ==================== CHAT HISTORY ENDPOINTS ====================

@router.get("/chat_sessions")
def get_chat_sessions(days: int = 7, limit: int = 100):
    """
    Get list of recent chat sessions

    Args:
        days: Number of days back to look
        limit: Maximum number of sessions to return
    """
    _log("GET /chat_sessions", days=days, limit=limit)
    try:
        history_service = get_chat_history_service()
        since = (datetime.now() - timedelta(days=days)).isoformat()
        sessions = history_service.get_all_sessions(since=since, limit=limit)

        return {
            "sessions": sessions,
            "total": len(sessions),
            "days": days
        }
    except Exception as e:
        print(f"[ADMIN] Error fetching chat sessions: {e}")
        return {"sessions": [], "total": 0, "error": str(e)}


@router.get("/chat_history/{session_id}")
def get_session_history(session_id: str, limit: Optional[int] = None):
    """
    Get chat history for a specific session

    Args:
        session_id: Session ID to retrieve
        limit: Optional limit on number of messages
    """
    _log("GET /chat_history", session_id=session_id, limit=limit)
    try:
        history_service = get_chat_history_service()
        messages = history_service.get_session_history(session_id=session_id, limit=limit)

        return {
            "session_id": session_id,
            "messages": messages,
            "total": len(messages)
        }
    except Exception as e:
        print(f"[ADMIN] Error fetching session history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chat_stats")
def get_chat_stats(days: int = 7):
    """
    Get conversation statistics

    Args:
        days: Number of days to analyze
    """
    _log("GET /chat_stats", days=days)
    try:
        history_service = get_chat_history_service()
        stats = history_service.get_conversation_stats(days=days)

        return stats
    except Exception as e:
        print(f"[ADMIN] Error fetching chat stats: {e}")
        return {
            "error": str(e),
            "total_sessions": 0,
            "total_messages": 0
        }


@router.get("/search_messages")
def search_chat_messages(
    query: str,
    role: Optional[str] = None,
    intent: Optional[str] = None,
    limit: int = 50
):
    """
    Search chat messages by content

    Args:
        query: Text to search for
        role: Filter by role (user/assistant)
        intent: Filter by intent
        limit: Maximum results
    """
    _log("GET /search_messages", query=query, role=role, intent=intent)
    try:
        history_service = get_chat_history_service()
        results = history_service.search_messages(
            query=query,
            role=role,
            intent=intent,
            limit=limit
        )

        return {
            "results": results,
            "total": len(results),
            "query": query
        }
    except Exception as e:
        print(f"[ADMIN] Error searching messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# ANALYTICS ENDPOINTS
# ============================================================

from app.services.analytics_service import get_analytics_service
from app.services.chat_quality_audit import run_daily_chat_quality_audit


@router.get("/analytics/dashboard")
async def get_analytics_dashboard(days: int = 7):
    """
    Vrne kompletno statistiko za admin dashboard.

    Args:
        days: Število dni za analizo (default 7)
    """
    _log("GET /analytics/dashboard", days=days)
    try:
        analytics = get_analytics_service()
        return analytics.get_dashboard_stats(days)
    except Exception as e:
        print(f"[ADMIN] Error getting analytics dashboard: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/analytics/trending")
async def get_trending_topics(days: int = 7):
    """
    Vrne trending topics in simptome.
    """
    _log("GET /analytics/trending", days=days)
    try:
        analytics = get_analytics_service()
        return analytics.get_trending_topics(days)
    except Exception as e:
        print(f"[ADMIN] Error getting trending topics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/analytics/funnel")
async def get_booking_funnel(days: int = 30):
    """
    Vrne booking funnel analizo - kje uporabniki odpadejo.
    """
    _log("GET /analytics/funnel", days=days)
    try:
        analytics = get_analytics_service()
        return analytics.get_booking_funnel(days)
    except Exception as e:
        print(f"[ADMIN] Error getting booking funnel: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/analytics/sentiment")
async def get_sentiment_stats(days: int = 7):
    """
    Vrne sentiment analizo pogovorov.
    """
    _log("GET /analytics/sentiment", days=days)
    try:
        analytics = get_analytics_service()
        return analytics.get_sentiment_stats(days)
    except Exception as e:
        print(f"[ADMIN] Error getting sentiment stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/analytics/peak-hours")
async def get_peak_hours(days: int = 30):
    """
    Vrne analizo peak hours - kdaj je največ aktivnosti.
    """
    _log("GET /analytics/peak-hours", days=days)
    try:
        analytics = get_analytics_service()
        return analytics.get_peak_hours(days)
    except Exception as e:
        print(f"[ADMIN] Error getting peak hours: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/analytics/reservations")
async def get_reservation_analytics(days: int = 30):
    """
    Vrne statistiko rezervacij.
    """
    _log("GET /analytics/reservations", days=days)
    try:
        analytics = get_analytics_service()
        return analytics.get_reservation_stats(days)
    except Exception as e:
        print(f"[ADMIN] Error getting reservation analytics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/analytics/reminder-stats")
async def get_reminder_statistics():
    """
    Vrne statistiko reminder sistema (no-show rate, confirmation rate, etc.)
    """
    _log("GET /analytics/reminder-stats")
    try:
        from app.services.reminder_scheduler import get_reminder_stats
        return get_reminder_stats()
    except Exception as e:
        print(f"[ADMIN] Error getting reminder stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/quality/daily-audit")
def get_daily_quality_audit(
    days: int = 1,
    max_sessions: int = 2000,
    max_examples: int = 20,
    include_test_sessions: bool = False,
):
    """
    Daily chat quality audit over saved chat_messages.
    Intended to be called once per day from cron/automation.
    """
    _log("GET /quality/daily-audit", days=days, max_sessions=max_sessions, max_examples=max_examples)
    try:
        exclude_prefixes = () if include_test_sessions else (
            "test_center::e2e",
            "test_center::golden",
            "test_center::test",
            "e2e",
            "golden",
            "test-",
        )
        return run_daily_chat_quality_audit(
            days=days,
            max_sessions=max_sessions,
            max_examples=max_examples,
            exclude_session_prefixes=exclude_prefixes,
        )
    except Exception as e:
        print(f"[ADMIN] Error running daily quality audit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# SMART SCHEDULER ENDPOINTS
# ============================================================

from app.services.smart_scheduler import get_smart_scheduler


@router.get("/scheduler/suggestions")
async def get_scheduling_suggestions(
    service_type: str,
    preferred_date: Optional[str] = None,
    phone: Optional[str] = None,
    max_suggestions: int = 5
):
    """
    Vrne pametne predloge terminov.

    Args:
        service_type: Tip storitve (dermatolog, ortoped, ...)
        preferred_date: Želeni datum (DD.MM.YYYY)
        phone: Telefon za lookup uporabnikovih preferenc
        max_suggestions: Maksimalno število predlogov
    """
    _log("GET /scheduler/suggestions", service_type=service_type, phone=phone)
    try:
        scheduler = get_smart_scheduler()
        return scheduler.get_smart_suggestions(
            service_type=service_type,
            preferred_date=preferred_date,
            phone=phone,
            max_suggestions=max_suggestions
        )
    except Exception as e:
        print(f"[ADMIN] Error getting scheduling suggestions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scheduler/user-preferences/{phone}")
async def get_user_scheduling_preferences(phone: str):
    """
    Vrne preference uporabnika na podlagi preteklih rezervacij.

    Args:
        phone: Telefonska številka
    """
    _log("GET /scheduler/user-preferences", phone=phone)
    try:
        scheduler = get_smart_scheduler()
        return scheduler.get_user_preferences(phone)
    except Exception as e:
        print(f"[ADMIN] Error getting user preferences: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scheduler/occupancy/{date}")
async def get_slot_occupancy(date: str):
    """
    Vrne zasedenost terminov za določen dan.

    Args:
        date: Datum v formatu DD.MM.YYYY
    """
    _log("GET /scheduler/occupancy", date=date)
    try:
        scheduler = get_smart_scheduler()
        return scheduler.get_slot_occupancy(date)
    except Exception as e:
        print(f"[ADMIN] Error getting slot occupancy: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scheduler/weekly-load")
async def get_weekly_load(start_date: Optional[str] = None):
    """
    Vrne load za cel teden.

    Args:
        start_date: Začetni datum (DD.MM.YYYY), default danes
    """
    _log("GET /scheduler/weekly-load", start_date=start_date)
    try:
        scheduler = get_smart_scheduler()
        return scheduler.get_weekly_load(start_date)
    except Exception as e:
        print(f"[ADMIN] Error getting weekly load: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# HANDOFF ENDPOINTS
# ============================================================

from app.services.handoff_service import get_handoff_service


@router.post("/handoff/create/{session_id}")
async def create_handoff(session_id: str, use_llm: bool = True, role: str = Depends(require_write)):
    """
    Ustvari handoff paket za prenos pogovora na recepcijo.

    Args:
        session_id: ID seje
        use_llm: Uporabi LLM za povzetek (default True)
    """
    _log("POST /handoff/create", session_id=session_id)
    try:
        handoff = get_handoff_service()
        return await handoff.create_handoff(session_id, use_llm_summary=use_llm)
    except Exception as e:
        print(f"[ADMIN] Error creating handoff: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/handoff/pending")
async def get_pending_handoffs():
    """
    Vrne vse pending handoff-e, sortirane po prioriteti.
    """
    _log("GET /handoff/pending")
    try:
        handoff = get_handoff_service()
        return {
            "handoffs": handoff.get_pending_handoffs(),
            "stats": handoff.get_handoff_stats()
        }
    except Exception as e:
        print(f"[ADMIN] Error getting pending handoffs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/handoff/{session_id}")
async def get_handoff_details(session_id: str):
    """
    Vrne podrobnosti specifičnega handoff-a.

    Args:
        session_id: ID seje
    """
    _log("GET /handoff", session_id=session_id)
    try:
        handoff_service = get_handoff_service()

        # Check if already in pending
        if session_id in handoff_service.pending_handoffs:
            return handoff_service.pending_handoffs[session_id]

        # Otherwise create new one
        return await handoff_service.create_handoff(session_id, use_llm_summary=False)
    except Exception as e:
        print(f"[ADMIN] Error getting handoff details: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class HandoffResolution(BaseModel):
    resolution_note: Optional[str] = None


@router.post("/handoff/resolve/{session_id}")
async def resolve_handoff(session_id: str, resolution: HandoffResolution = None, role: str = Depends(require_write)):
    """
    Označi handoff kot rešen.

    Args:
        session_id: ID seje
        resolution_note: Opomba o rešitvi
    """
    _log("POST /handoff/resolve", session_id=session_id)
    try:
        handoff = get_handoff_service()
        note = resolution.resolution_note if resolution else None
        success = handoff.resolve_handoff(session_id, note)
        return {"success": success, "session_id": session_id}
    except Exception as e:
        print(f"[ADMIN] Error resolving handoff: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/handoff/stats")
async def get_handoff_stats():
    """
    Vrne statistiko handoff sistema.
    """
    _log("GET /handoff/stats")
    try:
        handoff = get_handoff_service()
        return handoff.get_handoff_stats()
    except Exception as e:
        print(f"[ADMIN] Error getting handoff stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# PROACTIVE ASSISTANT ENDPOINTS
# ============================================================

from app.services.proactive_assistant import get_proactive_assistant


@router.get("/proactive/alerts/{phone}")
async def get_patient_alerts(phone: str, include_campaigns: bool = True):
    """
    Vrne proaktivne alerte za pacienta.

    Args:
        phone: Telefonska številka
        include_campaigns: Vključi zdravstvene kampanje
    """
    _log("GET /proactive/alerts", phone=phone)
    try:
        assistant = get_proactive_assistant()
        return assistant.get_patient_alerts(phone, include_campaigns)
    except Exception as e:
        print(f"[ADMIN] Error getting patient alerts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/proactive/campaigns")
async def get_active_campaigns():
    """
    Vrne trenutno aktivne zdravstvene kampanje.
    """
    _log("GET /proactive/campaigns")
    try:
        assistant = get_proactive_assistant()
        return {
            "campaigns": assistant.get_active_campaigns(),
            "month": datetime.now().month
        }
    except Exception as e:
        print(f"[ADMIN] Error getting campaigns: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/proactive/patient-patterns/{phone}")
async def get_patient_patterns(phone: str):
    """
    Vrne vzorce obnašanja pacienta.

    Args:
        phone: Telefonska številka
    """
    _log("GET /proactive/patient-patterns", phone=phone)
    try:
        assistant = get_proactive_assistant()
        history = assistant.get_patient_history(phone)
        patterns = assistant.analyze_patterns(history)
        return {
            "phone": phone,
            "patterns": patterns,
            "history_count": len(history)
        }
    except Exception as e:
        print(f"[ADMIN] Error getting patient patterns: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# KNOWLEDGE GRAPH ENDPOINTS
# ============================================================

from app.services.knowledge_graph import get_knowledge_graph


@router.post("/knowledge-graph/query-symptoms")
async def query_symptoms(text: str, role: str = Depends(require_write)):
    """
    Analizira simptome in vrne priporočila.

    Args:
        text: Opis simptomov
    """
    _log("POST /knowledge-graph/query-symptoms")
    try:
        kg = get_knowledge_graph()
        return kg.query_symptoms(text)
    except Exception as e:
        print(f"[ADMIN] Error querying symptoms: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/knowledge-graph/preparations/{service_id}")
async def get_service_preparations(service_id: str):
    """
    Vrne potrebne priprave za storitev.

    Args:
        service_id: ID storitve (dermatoloski_pregled, ortopedski_pregled, ...)
    """
    _log("GET /knowledge-graph/preparations", service_id=service_id)
    try:
        kg = get_knowledge_graph()
        return {
            "service_id": service_id,
            "preparations": kg.get_preparations_for_service(service_id)
        }
    except Exception as e:
        print(f"[ADMIN] Error getting preparations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/knowledge-graph/related/{node_id}")
async def get_related_info(node_id: str):
    """
    Vrne povezane informacije za vozlišče v grafu.

    Args:
        node_id: ID vozlišča
    """
    _log("GET /knowledge-graph/related", node_id=node_id)
    try:
        kg = get_knowledge_graph()
        return kg.get_related_info(node_id)
    except Exception as e:
        print(f"[ADMIN] Error getting related info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/knowledge-graph/stats")
async def get_knowledge_graph_stats():
    """
    Vrne statistiko knowledge grafa.
    """
    _log("GET /knowledge-graph/stats")
    try:
        kg = get_knowledge_graph()
        node_types = {}
        for node in kg.nodes.values():
            t = node.node_type.value
            node_types[t] = node_types.get(t, 0) + 1

        return {
            "total_nodes": len(kg.nodes),
            "total_edges": len(kg.edges),
            "nodes_by_type": node_types
        }
    except Exception as e:
        print(f"[ADMIN] Error getting knowledge graph stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# TRIAGE SERVICE ENDPOINTS
# ============================================================

from app.services.triage_service import get_triage_service


@router.post("/triage/quick")
async def quick_triage(symptoms: str, role: str = Depends(require_write)):
    """
    Izvede hitro triažo na podlagi simptomov.

    Args:
        symptoms: Opis simptomov

    Returns:
        Priporočilo in analiza (brez multi-step procesa)
    """
    _log("POST /triage/quick")
    try:
        triage = get_triage_service()
        return triage.quick_triage(symptoms)
    except Exception as e:
        print(f"[ADMIN] Error in quick triage: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/triage/start/{session_id}")
async def start_triage_session(session_id: str, role: str = Depends(require_write)):
    """
    Začne novo triage sejo.

    Args:
        session_id: ID seje
    """
    _log("POST /triage/start", session_id=session_id)
    try:
        triage = get_triage_service()
        return triage.start_triage_session(session_id)
    except Exception as e:
        print(f"[ADMIN] Error starting triage: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class TriageResponse(BaseModel):
    response: str


@router.post("/triage/respond/{session_id}")
async def process_triage_response(session_id: str, data: TriageResponse, role: str = Depends(require_write)):
    """
    Procesira odgovor v triage seji.

    Args:
        session_id: ID seje
        response: Uporabnikov odgovor
    """
    _log("POST /triage/respond", session_id=session_id)
    try:
        triage = get_triage_service()
        return triage.process_response(session_id, data.response)
    except Exception as e:
        print(f"[ADMIN] Error processing triage response: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/triage/session/{session_id}")
async def get_triage_session(session_id: str):
    """
    Vrne podatke o triage seji.

    Args:
        session_id: ID seje
    """
    _log("GET /triage/session", session_id=session_id)
    try:
        triage = get_triage_service()
        session = triage.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return {
            "session_id": session.session_id,
            "state": session.state.value,
            "symptoms": session.symptoms,
            "duration_days": session.duration_days,
            "intensity": session.intensity.value if session.intensity else None,
            "started_at": session.started_at
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ADMIN] Error getting triage session: {e}")
        raise HTTPException(status_code=500, detail=str(e))
