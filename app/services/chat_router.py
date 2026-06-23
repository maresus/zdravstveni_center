import re
import json
import os
from pathlib import Path
from datetime import datetime
from typing import Any, Optional
import uuid
import threading
from types import SimpleNamespace

from fastapi import APIRouter, Request

from app.models.chat import ChatRequest, ChatResponse
from app.services.reservation_service import ReservationService
from app.services.email_service import send_guest_confirmation, send_admin_notification
from app.services.sms_service import send_booking_received_sms
from app.services.health_center_extensions import (
    validate_appointment_rules,
    format_appointment_summary,
    get_available_time_slots,
    get_service_info,
    format_all_services_summary,
    get_service_map,
    get_services,
)
from app.rag.rag_engine import rag_engine
from app.rag.chroma_service import answer_tourist_question, is_tourist_query
from app.services.chat_history_service import get_chat_history_service
from app.services.clinic_config import (
    get_clinic_config,
    get_info_response as clinic_get_info_response,
    get_domain_response,
    get_fast_pass_match,
    list_available_clinics,
    resolve_clinic_id,
    set_current_clinic_id,
    reset_current_clinic_id,
)

# Unified Routing System imports
from app.services.session.unified_state import (
    get_unified_state,
    reset_unified_state,
    is_in_flow,
    get_current_step,
    start_flow,
    get_appointment_data,
    is_appointment_complete,
    StateManager,
    FlowType,
    FlowStep,
)
from app.services.routing.unified_router import route as unified_route, IntentType
from app.services.routing.nlp_utils import is_affirmative, is_negative
from app.services.routing.message_normalizer import normalize_user_message
from app.services.routing.appointment_change_requests import (
    detect_appointment_change_request,
    build_appointment_change_reply,
)
from app.services.routing.symptom_general_handler import (
    should_handle_general_symptom_message,
    build_general_symptom_clarify_reply,
)
from app.services.routing.question_intent_guard import (
    looks_like_waiting_time_question,
    looks_like_service_provider_question,
    looks_like_booking_push_message,
    build_waiting_time_reply,
    build_service_provider_reply,
)
from app.services.routing.state_manager import ConversationTracker, SimpleCache
from app.services.routing.interrupt_handler import build_interrupt_response, build_resume_prompt
from app.services.routing.advice import advice_only, advice_only_headache
from app.services.routing.symptom_fallback import (
    append_nudge_if_missing as _append_nudge_if_missing,
    looks_like_medical_statement as _looks_like_medical_statement,
    looks_like_medication_request as _looks_like_medication_request,
    looks_like_previsit_question as _looks_like_previsit_question,
    looks_like_symptom_report as _looks_like_symptom_report,
    looks_like_uncertain_help_request as _looks_like_uncertain_help_request,
    looks_like_urgent_message as _looks_like_urgent_message,
    match_unsupported_symptom as _match_unsupported_symptom,
    previsit_guidance_response as _previsit_guidance_response,
    quick_triage_fallback as _quick_triage_fallback,
    specialist_quick_replies as _specialist_quick_replies,
    symptom_booking_nudge as _symptom_booking_nudge,
)
from app.services.routing.intent_labels import (
    INTENT_BOOK_GENERAL,
    INTENT_CHECK_AVAILABILITY,
    INTENT_GREETING,
    INTENT_HEALTH_SYMPTOMS,
    INTENT_INFO_CONTACT,
    INTENT_INFO_HOURS,
    INTENT_INFO_NAROCANJE,
    INTENT_INFO_PRICES,
    INTENT_INFO_SERVICES,
    INTENT_INFO_TEAM,
    INTENT_QUESTION,
    INTENT_THANKS,
)
from app.services.routing.response_keys import (
    INFO_KEY_CONTACT,
    INFO_KEY_HOURS,
    INFO_KEY_LOCATION,
    INFO_KEY_PARKING,
    INFO_KEY_PRICES,
    INFO_KEY_SERVICES,
    INFO_KEYS_DIRECT,
    INFO_CONTACT_SHORT,
    INFO_SERVICE_DERMATOLOG,
    INFO_SERVICE_ESTETSKI_POSEG,
    INFO_SERVICE_FIZIOTERAPIJA,
    INFO_SERVICE_KOZMETIKA,
    INFO_SERVICE_LASERSKI_POSEG,
    INFO_SERVICE_OKULIST,
    INFO_SERVICE_ORTOPED,
)
from app.services.routing.locale_sl import (
    AVAILABILITY_PHRASES,
    AVAILABILITY_WORDS,
    BOOKING_INFO_PHRASES,
    BOOKING_KEYWORDS,
    BOOKING_KEYWORDS_EXTENDED,
    BOOKING_RELEVANT_KEYS,
    CONTACT_WORDS,
    CONTACT_ROUTE_WORDS,
    CRITICAL_INFO_KEYS,
    GREETING_WORDS,
    HOURS_WORDS,
    PRICE_WORDS,
    QUESTION_MARKERS,
    RELATIVE_DATES,
    SERVICE_INFO_TOKENS,
    SERVICE_KEYWORDS,
    SERVICE_LIST_WORDS,
    SYMPTOM_MARKERS,
    SYMPTOM_PATTERNS,
    SYMPTOM_WORDS,
    TEAM_WORDS,
    THANKS_WORDS,
)
from app.services.knowledge.hybrid_kb import answer_with_hybrid_kb
from app.core.response_formatter import format_response
from app.services.flows.booking_flow import (
    BookingFlowDeps,
    get_resume_prompt as booking_get_resume_prompt,
    handle_appointment_booking as booking_handle_appointment_booking,
)
from app.services.flows.info_flow import pick_info_key
from app.services.flows.interrupt_flow import InterruptFlowDeps, resolve_interrupt_answer
from app.services.flows.booking_interrupt_policy import (
    BookingInterruptDeps,
    handle_booking_interrupt,
    should_prioritize_booking_step_input,
)
from app.services.session_bridge import (
    appointment_states,
    get_appointment_state,
    reset_appointment_state,
)
from app.services.intent_handlers import (
    handle_greeting_goodbye_urgency,
    handle_info_intent,
    handle_price_intent,
    handle_service_info_intent,
    handle_unsupported_symptom_intent,
)
from app.services.chat_orchestrator import process_chat_turn
from app.services.routing.parsers import (
    classify_intent,
    classify_intent_rules,
    detect_service_from_message,
    extract_date_from_message,
    extract_service_type,
    extract_time_from_message,
    is_likely_full_name,
)

router = APIRouter(prefix="/chat", tags=["chat"])
USE_ROUTER_V2 = True
USE_FULL_KB_LLM = False  # False = RAG (fast), True = full KB (slow)
# D4: legacy kill - unified router is always enabled.
USE_UNIFIED_ROUTER = True
SHORT_MODE = os.getenv("SHORT_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}

# ========== ANTI-LOOP & CACHE MECHANISMS ==========

# Initialize trackers
conversation_tracker = ConversationTracker()
response_cache = SimpleCache()

# Chat history service (for persistent storage)
def save_chat_message(
    session_id: str,
    role: str,
    content: str,
    intent: Optional[str] = None,
    service_mentioned: Optional[str] = None,
    booking_step: Optional[str] = None,
    response_cached: bool = False,
    metadata: Optional[dict] = None
):
    """
    Save chat message to persistent storage (non-blocking)

    Args:
        session_id: Session ID
        role: "user" or "assistant"
        content: Message content
        intent: Classified intent
        service_mentioned: Service mentioned
        booking_step: Current booking step
        response_cached: Whether response was cached
        metadata: Additional metadata (e.g., confidence scores)
    """
    try:
        history_service = get_chat_history_service()
        history_service.save_message(
            session_id=session_id,
            role=role,
            content=content,
            intent=intent,
            service_mentioned=service_mentioned,
            booking_step=booking_step,
            response_cached=response_cached,
            metadata=metadata
        )
    except Exception as e:
        # Non-blocking - don't fail request if storage fails
        print(f"[CHAT_HISTORY] Failed to save message: {e}")

# ========== ZDRAVSTVENI CENTER INFO ODGOVORI (YAML) ==========
def _default_help_prompt(clinic_id: str | None = None) -> str:
    return get_domain_response(
        "general",
        "help_prompt",
        default="How can I help?",
        clinic_id=clinic_id,
    )


def _get_info_response(key: str, clinic_id: str | None = None) -> str:
    # Prefer domain-configured info facts (config/clinics/<id>/info.yaml), then legacy clinic config.
    domain_value = get_domain_response("info", key, default=None, clinic_id=clinic_id)
    if isinstance(domain_value, str) and domain_value.strip():
        return domain_value
    return clinic_get_info_response(key, _default_help_prompt(clinic_id=clinic_id), clinic_id=clinic_id)

def get_response(key: str, clinic_id: str | None = None, **kwargs: Any) -> str:
    """Temporary response lookup wrapper for cleanup work."""
    if "." in key:
        domain, rest = key.split(".", 1)
        response = get_domain_response(domain, rest, default=None, clinic_id=clinic_id)
        if isinstance(response, str):
            return response.format_map({**kwargs})
    if key.startswith("info."):
        info_key = key.split(".", 1)[1]
        return get_domain_response("info", info_key, default=_get_info_response(info_key, clinic_id=clinic_id), clinic_id=clinic_id)
    return _get_info_response(INFO_KEY_LOCATION, clinic_id=clinic_id)


def _get_uncertain_marker(clinic_id: str | None = None) -> str:
    return get_domain_response("general", "uncertain_marker", default="I'm not sure", clinic_id=clinic_id)


def _extract_primary_phone(text: str) -> str | None:
    match = re.search(r"(\+?\d[\d\s]{6,}\d)", text or "")
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _extract_primary_email(text: str) -> str | None:
    match = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", text or "")
    return match.group(1).strip() if match else None


def _looks_like_reschedule_request(message: str) -> bool:
    return detect_appointment_change_request(message) == "reschedule"


def _rag_info_answer(question: str, fallback_key: str, clinic_id: str | None = None) -> str:
    """Return KB/RAG-backed answer for info queries, fallback to hardcoded info."""
    try:
        results = rag_engine.search(question, top_k=3)
    except Exception as e:
        print(f"[RAG] Search failed: {e}")
        results = []

    if not results:
        return _get_info_response(fallback_key, clinic_id=clinic_id)

    best = results[0]
    content = (best.content or "").strip()
    if not content:
        return _get_info_response(fallback_key, clinic_id=clinic_id)

    max_len = 700
    if len(content) > max_len:
        snippet = content[:max_len]
        last_dot = snippet.rfind(".")
        if last_dot > 200:
            snippet = snippet[: last_dot + 1]
    else:
        snippet = content

    if best.url:
        label = get_domain_response(
            "general",
            "more_info_label",
            default="More info:",
            clinic_id=clinic_id,
        )
        return f"{snippet}\n\n{label} {best.url}"
    return snippet

def _send_reservation_emails_async(payload: dict) -> None:
    """Send appointment notifications asynchronously (email + SMS)."""
    def _worker() -> None:
        try:
            send_guest_confirmation(payload)
            send_admin_notification(payload)
            if payload.get("phone"):
                send_booking_received_sms(payload)
        except Exception as exc:
            print(f"[NOTIFY] Async send failed: {exc}")
    threading.Thread(target=_worker, daemon=True).start()

# Load full knowledge base
FULL_KB_TEXT = ""
try:
    kb_path = Path(__file__).resolve().parents[2] / "knowledge.jsonl"
    if kb_path.exists():
        chunks = []
        for line in kb_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                text = obj.get("text", "")
                if text:
                    chunks.append(text)
            except Exception:
                pass
        FULL_KB_TEXT = "\n\n".join(chunks)
except Exception as e:
    print(f"[KB] Failed to load knowledge base: {e}")

# Session states
conversation_state: dict[str, dict[str, Any]] = {}  # Session → metadata (confidence, etc.)
conversation_history: list[dict[str, str]] = []
last_interaction: Optional[datetime] = None
chat_session_id: str = str(uuid.uuid4())[:8]
last_user_message_by_session: dict[str, str] = {}


def _short_contact_info() -> str:
    return get_response(INFO_CONTACT_SHORT)


def _service_price_info(service_type: Optional[str], clinic_id: str | None = None) -> str:
    info = get_service_info(service_type or "", clinic_id=clinic_id)
    if not info:
        return _get_info_response(INFO_KEY_PRICES, clinic_id=clinic_id)
    label = service_type or ""
    return get_response(
        "general.service_price",
        clinic_id=clinic_id,
        label=label.capitalize(),
        name=info["name"],
        price_range=info["price_range"],
        duration_minutes=info["duration_minutes"],
    )


def _service_info_response(service: Optional[str], clinic_id: str | None = None) -> Optional[str]:
    if service == "DERMATOLOG":
        return get_response(INFO_SERVICE_DERMATOLOG, clinic_id=clinic_id)
    if service == "ORTOPED":
        return get_response(INFO_SERVICE_ORTOPED, clinic_id=clinic_id)
    if service == "OKULIST":
        return get_response(INFO_SERVICE_OKULIST, clinic_id=clinic_id)
    if service == "ESTETSKI_POSEG":
        return get_response(INFO_SERVICE_ESTETSKI_POSEG, clinic_id=clinic_id)
    if service == "LASERSKI_POSEG":
        return get_response(INFO_SERVICE_LASERSKI_POSEG, clinic_id=clinic_id)
    if service == "FIZIOTERAPIJA":
        return get_response(INFO_SERVICE_FIZIOTERAPIJA, clinic_id=clinic_id)
    if service == "KOZMETIKA":
        return get_response(INFO_SERVICE_KOZMETIKA, clinic_id=clinic_id)
    return None


BOOKING_FLOW_DEPS = BookingFlowDeps(
    get_appointment_state=get_appointment_state,
    reset_appointment_state=reset_appointment_state,
    reset_unified_state=reset_unified_state,
    reset_loop_count=conversation_tracker.reset_loop_count,
    set_step=lambda session_id, state, step: (
        state.__setitem__("step", step),
        StateManager(session_id).set_step(FlowStep(step)) if step in {s.value for s in FlowStep} else StateManager(session_id).set_step(None),
    ),
    set_appointment_field=lambda session_id, state, field, value: (
        state.__setitem__(field, value),
        StateManager(session_id).set_appointment_field(field, value),
    ),
    clear_appointment_data=lambda session_id, state: (
        reset_appointment_state(state),
        StateManager(session_id).clear_appointment_data(),
    ),
    is_negative=is_negative,
    is_affirmative=is_affirmative,
    is_likely_full_name=is_likely_full_name,
    extract_service_type=extract_service_type,
    extract_date_from_message=extract_date_from_message,
    extract_time_from_message=extract_time_from_message,
    validate_appointment_rules=validate_appointment_rules,
    get_available_time_slots=get_available_time_slots,
    get_service_info=get_service_info,
    format_appointment_summary=format_appointment_summary,
    reservation_service_cls=ReservationService,
    send_notifications_async=_send_reservation_emails_async,
    set_context_value=lambda session_id, key, value: StateManager(session_id).set_context_value(key, value),
)

INTERRUPT_FLOW_DEPS = InterruptFlowDeps(
    get_info_response=_get_info_response,
    service_price_info=_service_price_info,
    get_service_info=get_service_info,
)


def _booking_resume_prompt(step: str | None, state: dict[str, Any]) -> str:
    resume = build_resume_prompt(step)
    if resume:
        return resume
    return booking_get_resume_prompt(state, BOOKING_FLOW_DEPS)


BOOKING_INTERRUPT_DEPS = BookingInterruptDeps(
    is_in_flow=is_in_flow,
    get_current_step=get_current_step,
    get_appointment_data=get_appointment_data,
    get_context_value=lambda session_id, key, default=None: StateManager(session_id).get_context_value(key, default),
    set_context_value=lambda session_id, key, value: StateManager(session_id).set_context_value(key, value),
    extract_date_from_message=extract_date_from_message,
    extract_time_from_message=extract_time_from_message,
    extract_service_type=extract_service_type,
    is_likely_full_name=is_likely_full_name,
    build_interrupt_response=build_interrupt_response,
    build_resume_prompt=_booking_resume_prompt,
    interrupt_answer=lambda **kwargs: resolve_interrupt_answer(deps=INTERRUPT_FLOW_DEPS, **kwargs),
    get_info_response=_get_info_response,
    get_service_info=get_service_info,
    looks_like_symptom_report=_looks_like_symptom_report,
    symptom_advice=lambda message, service: (
        advice_only_headache()
        if any(k in message.lower() for k in ["glava", "glavobol", "migrena"])
        else advice_only(service)
    ),
)


def get_resume_prompt(state: dict) -> str:
    """Backward-compatible wrapper around extracted booking flow."""
    return booking_get_resume_prompt(state, BOOKING_FLOW_DEPS)


def handle_appointment_booking(message: str, session_id: str) -> str:
    """Backward-compatible wrapper around extracted booking flow."""
    return booking_handle_appointment_booking(message, session_id, BOOKING_FLOW_DEPS)


# ============================================================
# UNIFIED ROUTING SYSTEM - New architecture
# ============================================================

def handle_unified_routing(
    message: str,
    session_id: str,
    clinic_id: str | None = None,
) -> str | None:
    """
    Handle message using unified routing system.
    Returns response string, or None if should fall back to legacy system.
    """
    if clinic_id:
        set_current_clinic_id(clinic_id)
    appointment_state = get_appointment_state(session_id)
    state_mgr = StateManager(session_id)
    if clinic_id:
        state_mgr.set_context_value("clinic_id", clinic_id)
    state_mgr.set_context_value("clinic_id", clinic_id)
    unified_state = state_mgr.get_state()
    context = state_mgr.ensure_context()

    # Always redirect booking intent to widget form — no step-by-step flow
    _quick_booking_check = unified_route(message, state_mgr.get_state())
    if _quick_booking_check.primary_intent == IntentType.BOOKING_APPOINTMENT:
        _svc = _quick_booking_check.service_type
        if _svc:
            return f"Odlično! Za naročilo na **{_svc}** kliknite gumb 📅 Naroči se spodaj — obrazec je hiter in enostaven."
        return "Z veseljem! Kliknite gumb **📅 Naroči se** spodaj — izpolnite ime, telefon in storitev, pa vas bomo kontaktirali za potrditev termina."

    change_action = detect_appointment_change_request(message)
    if change_action in {"reschedule", "cancel"}:
        contact_text = _get_info_response(INFO_KEY_CONTACT, clinic_id=clinic_id)
        phone = _extract_primary_phone(contact_text) or "04 271 30 10"
        email = _extract_primary_email(contact_text)
        return build_appointment_change_reply(change_action, phone=phone, email=email)

    def _clear_booking_details_preserve_service() -> None:
        """Clear appointment details when changing service, keep only service type."""
        current_service = appointment_state.get("service_type")
        state_mgr.clear_appointment_data()
        if current_service:
            state_mgr.set_appointment_field("service_type", current_service)
        if appointment_state is not None:
            appointment_state["date"] = None
            appointment_state["time"] = None
            appointment_state["name"] = None
            appointment_state["phone"] = None
            appointment_state["email"] = None
            appointment_state["reason"] = None
            appointment_state["step"] = "date"

    # Handle pending service switch confirmation (DA/NE).
    pending_service_switch = context.get("pending_service_switch")
    if pending_service_switch and is_in_flow(session_id):
        pending_service_key = str(pending_service_switch).lower()
        pending_info = get_service_info(pending_service_key)

        if is_affirmative(message):
            state_mgr.confirm_service_switch(pending_service_key, legacy_state=appointment_state)
            state_mgr.clear_context_key("pending_service_switch")

            if pending_info:
                return get_response(
                    "booking.service_switch_confirmed",
                    clinic_id=clinic_id,
                    service_name=pending_info["name"],
                    duration_minutes=pending_info["duration_minutes"],
                    price_range=pending_info["price_range"],
                )
            return get_response("booking.service_switch_confirmed_noinfo", clinic_id=clinic_id)

        if is_negative(message):
            state_mgr.clear_context_key("pending_service_switch")
            step = appointment_state.get("step") or get_current_step(session_id)
            return build_resume_prompt(step) or get_response("booking.resume_prompt", clinic_id=clinic_id)

    # Follow-up path for unsupported symptoms (DA/NE -> specialist choice -> booking).
    awaiting_specialist_prompt = bool(context.get("awaiting_specialist_prompt"))
    awaiting_specialist_choice = bool(context.get("awaiting_specialist_choice"))
    if awaiting_specialist_prompt and not is_in_flow(session_id):
        service_choice = extract_service_type(message, clinic_id=clinic_id)
        if service_choice:
            state_mgr.clear_context_key("awaiting_specialist_prompt")
            state_mgr.clear_context_key("awaiting_specialist_choice")
            state_mgr.transition_to_booking(service_type=service_choice, legacy_state=appointment_state)
            start_flow(session_id, FlowType.APPOINTMENT, FlowStep.DATE)
            return get_response("booking.start_with_date", clinic_id=clinic_id, service_type=service_choice.lower())

        if is_affirmative(message):
            data = _specialist_quick_replies(clinic_id=clinic_id)
            if data:
                state_mgr.set_context_value("ui_override", {"type": "quick_replies", "data": data})
            state_mgr.clear_context_key("awaiting_specialist_prompt")
            state_mgr.set_context_value("awaiting_specialist_choice", True)
            return (
                "Seveda. Ker brez pregleda ne morem zanesljivo določiti specialista, "
                "izberite prosim, pri katerem želite preveriti termin."
            )

        if is_negative(message):
            state_mgr.clear_context_key("awaiting_specialist_prompt")
            services = get_services(clinic_id=clinic_id)
            service_names = [info["name"].lower() for info in services.values() if isinstance(info, dict) and info.get("name")]
            if service_names:
                services_text = ", ".join(service_names[:-1]) + f" ali {service_names[-1]}" if len(service_names) > 1 else service_names[0]
                return f"V redu. Če si premislite, lahko napišete: {services_text}."
            return "V redu. Če si premislite, lahko napišete: ortoped, dermatolog ali okulist."

    if awaiting_specialist_choice and not is_in_flow(session_id):
        service_choice = extract_service_type(message, clinic_id=clinic_id)
        if service_choice:
            state_mgr.clear_context_key("awaiting_specialist_choice")
            state_mgr.transition_to_booking(service_type=service_choice, legacy_state=appointment_state)
            start_flow(session_id, FlowType.APPOINTMENT, FlowStep.DATE)
            return get_response("booking.start_with_date", clinic_id=clinic_id, service_type=service_choice.lower())
        if is_negative(message):
            state_mgr.clear_context_key("awaiting_specialist_choice")
            return "V redu. Če si premislite, sem tukaj."
        # Detect "which specialists?" questions and provide full list
        msg_lower = message.lower()
        is_asking_list = any(kw in msg_lower for kw in ["kateri", "katere", "na voljo", "ponujate", "imate", "seznam"])
        services = get_services(clinic_id=clinic_id)
        service_names = [info["name"] for info in services.values() if isinstance(info, dict) and info.get("name")]
        if is_asking_list and service_names:
            services_text = ", ".join(service_names[:-1]) + f" in {service_names[-1]}" if len(service_names) > 1 else service_names[0] if service_names else ""
            return f"V našem centru nudimo: {services_text}.\n\nKaterega izberete?"
        data = _specialist_quick_replies(clinic_id=clinic_id)
        if data:
            state_mgr.set_context_value("ui_override", {"type": "quick_replies", "data": data})
        # Build dynamic specialist list
        if service_names:
            specialists_text = ", ".join(service_names[:-1]) + f" ali {service_names[-1]}" if len(service_names) > 1 else service_names[0] if service_names else ""
            return f"Izberite specialista: {specialists_text}."
        return "Izberite specialista: dermatolog, ortoped ali okulist."

    # Keep unified state in sync with legacy appointment state.
    was_in_flow = is_in_flow(session_id)
    if appointment_state.get("step"):
        state_mgr.set_flow(FlowType.APPOINTMENT)
        step_val = appointment_state.get("step")
        try:
            state_mgr.set_step(FlowStep(step_val))
        except Exception:
            state_mgr.set_step(None)
    elif appointment_state.get("service_type") or was_in_flow:
        state_mgr.set_flow(FlowType.APPOINTMENT)
        if unified_state.get("step") is None:
            state_mgr.set_step(FlowStep.DATE)
    else:
        state_mgr.set_flow(FlowType.IDLE)
        state_mgr.set_step(None)

    suggested_service = context.get("suggested_service")
    current_step = get_current_step(session_id)

    # Flow-aware priority: if user clearly answers the expected booking step,
    # bypass generic routing/fallback and let booking flow consume it directly.
    if is_in_flow(session_id) and should_prioritize_booking_step_input(
        message=message,
        step=current_step,
        appointment_data=get_appointment_data(session_id),
        deps=BOOKING_INTERRUPT_DEPS,
        decision_intent=None,
        service_hint=suggested_service,
    ):
        return None

    decision = unified_route(message, unified_state)

    # If user provides a date after service info prompt, start booking immediately
    date_str = extract_date_from_message(message)
    if date_str and suggested_service:
        if not is_in_flow(session_id) or current_step in {FlowStep.SERVICE.value, "select_service", None}:
            state_mgr.transition_to_booking(service_type=suggested_service, legacy_state=appointment_state)
            start_flow(session_id, FlowType.APPOINTMENT, FlowStep.DATE)
            state_mgr.clear_context_key("suggested_service")
            return handle_appointment_booking(message, session_id)

    # Service step should accept classified service even if raw text is partial (e.g. "dermatolo").
    if (
        is_in_flow(session_id)
        and current_step in {FlowStep.SERVICE.value, "select_service", None}
        and decision.service_type
    ):
        state_mgr.transition_to_booking(service_type=decision.service_type, legacy_state=appointment_state)
        start_flow(session_id, FlowType.APPOINTMENT, FlowStep.DATE)
        state_mgr.set_context_value("suggested_service", decision.service_type)
        return handle_appointment_booking(message, session_id)

    # One-line booking requests are sometimes classified as SERVICE_INFO due to service descriptions ("pregled").
    # If a concrete service and date are already present, prefer booking flow over info response.
    if (
        not is_in_flow(session_id)
        and decision.service_type
        and extract_date_from_message(message)
        and decision.primary_intent in {IntentType.SERVICE_INFO, IntentType.GENERAL}
    ):
        state_mgr.transition_to_booking(service_type=decision.service_type, legacy_state=appointment_state)
        start_flow(session_id, FlowType.APPOINTMENT, FlowStep.DATE)
        return handle_appointment_booking(message, session_id)

    # Log decision for debugging
    print(f"[UNIFIED] Intent: {decision.primary_intent.value}, Confidence: {decision.confidence:.2f}, Action: {decision.action.value}, Service: {decision.service_type}")

    policy_response = handle_booking_interrupt(
        message=message,
        session_id=session_id,
        decision_intent=decision.primary_intent,
        service_hint=decision.service_type or suggested_service,
        deps=BOOKING_INTERRUPT_DEPS,
    )
    if policy_response:
        return policy_response

    # Do not allow info/price detours while user is providing visit reason.
    if is_in_flow(session_id) and get_current_step(session_id) == FlowStep.REASON.value:
        if decision.primary_intent in {IntentType.INFO, IntentType.PRICE, IntentType.SERVICE_INFO}:
            return None

    # Handle AFFIRMATIVE/NEGATIVE in booking flow
    if decision.primary_intent == IntentType.AFFIRMATIVE and is_in_flow(session_id):
        step = get_current_step(session_id)
        if step == FlowStep.CONFIRM.value:
            # Complete booking
            appointment_data = get_appointment_data(session_id)
            if is_appointment_complete(session_id):
                # Use legacy booking completion
                return None  # Fall back to handle actual booking
        # Continue with next step
        return None  # Fall back to legacy for step handling

    # If user confirms after service info (no flow yet), start booking
    if decision.primary_intent == IntentType.AFFIRMATIVE and suggested_service and not is_in_flow(session_id):
        state_mgr.transition_to_booking(service_type=suggested_service, legacy_state=appointment_state)
        start_flow(session_id, FlowType.APPOINTMENT, FlowStep.DATE)
        state_mgr.clear_context_key("suggested_service")
        return handle_appointment_booking(message, session_id)

    # If user sends a date right after service recommendation, continue booking directly.
    if (
        not is_in_flow(session_id)
        and suggested_service
        and extract_date_from_message(message)
    ):
        state_mgr.transition_to_booking(service_type=suggested_service, legacy_state=appointment_state)
        start_flow(session_id, FlowType.APPOINTMENT, FlowStep.DATE)
        state_mgr.clear_context_key("suggested_service")
        return handle_appointment_booking(message, session_id)

    if decision.primary_intent == IntentType.NEGATIVE and is_in_flow(session_id):
        reset_unified_state(session_id)
        return get_response("general.booking_cancelled", clinic_id=clinic_id)

    basic_intent_reply = handle_greeting_goodbye_urgency(
        intent=decision.primary_intent,
        message=message,
        clinic_id=clinic_id,
        get_response=get_response,
        looks_like_urgent_message=_looks_like_urgent_message,
    )
    if basic_intent_reply:
        return basic_intent_reply

    # Medication/prescription asks should not start booking flow directly.
    if _looks_like_medication_request(message):
        response_text = (
            "Prek klepeta ne izdajamo zdravil ali receptov. "
            "Za zdravila je potreben posvet pri zdravniku.\n\n"
            "Če želite, vas lahko takoj naročim na ustrezen pregled."
        )
        if is_in_flow(session_id):
            return build_interrupt_response(response_text, get_current_step(session_id), True)
        return response_text

    # Vague symptom/medical messages without a concrete specialist should not jump into booking service selection.
    if should_handle_general_symptom_message(
        message=message,
        primary_intent=decision.primary_intent,
        service_type=decision.service_type or suggested_service,
        in_flow=is_in_flow(session_id),
        looks_like_symptom_report=_looks_like_symptom_report,
        looks_like_medical_statement=_looks_like_medical_statement,
    ):
        triage_fallback = _quick_triage_fallback(message, clinic_id=clinic_id)
        return build_general_symptom_clarify_reply(triage_fallback=triage_fallback)

    # Waiting-time questions should not be misrouted into booking service-selection.
    if not is_in_flow(session_id) and looks_like_waiting_time_question(message):
        waiting_service = decision.service_type or suggested_service or extract_service_type(message, clinic_id=clinic_id)
        return build_waiting_time_reply(waiting_service)

    # Service-provider questions (e.g. "kdo opravlja dermatološki pregled") should return info, not booking start.
    if (
        not is_in_flow(session_id)
        and decision.service_type
        and looks_like_service_provider_question(message)
        and not looks_like_booking_push_message(message)
    ):
        info = get_service_info(decision.service_type.lower(), clinic_id=clinic_id)
        if info:
            return build_service_provider_reply(
                service_type=decision.service_type,
                service_name=str(info.get("name") or decision.service_type),
                duration_minutes=info.get("duration_minutes"),
                price_range=info.get("price_range"),
            )

    # Handle BOOKING_APPOINTMENT intent — redirect to form button
    if decision.primary_intent == IntentType.BOOKING_APPOINTMENT:
        service_type = decision.service_type
        if service_type:
            return f"Odlično! Za naročilo na **{service_type}** kliknite gumb 📅 Naroči se spodaj — izpolnite ime, telefon in datum, pa vas bomo kontaktirali za potrditev."
        return "Z veseljem vam pomagamo z naročilom! Kliknite gumb **📅 Naroči se** spodaj — obrazec je hiter in enostaven."

    service_info_reply = handle_service_info_intent(
        intent=decision.primary_intent,
        message=message,
        clinic_id=clinic_id,
        session_id=session_id,
        current_step=get_current_step(session_id),
        decision_service=decision.service_type,
        state_mgr=state_mgr,
        appointment_state=appointment_state,
        is_in_flow=is_in_flow,
        extract_service_type=extract_service_type,
        start_flow=start_flow,
        transition_to_booking=lambda mgr, service_type, legacy_state: mgr.transition_to_booking(
            service_type=service_type, legacy_state=legacy_state
        ),
        flow_type_appointment=FlowType.APPOINTMENT,
        flow_step_date=FlowStep.DATE,
        extract_date_from_message=extract_date_from_message,
        get_response=get_response,
        looks_like_previsit_question=_looks_like_previsit_question,
        previsit_guidance_response=_previsit_guidance_response,
        looks_like_symptom_report=_looks_like_symptom_report,
        symptom_booking_nudge=_symptom_booking_nudge,
        append_nudge_if_missing=_append_nudge_if_missing,
        advice_only=advice_only,
        advice_only_headache=advice_only_headache,
        quick_triage_fallback=_quick_triage_fallback,
        service_info_response=_service_info_response,
        get_info_response=_get_info_response,
        service_price_info=_service_price_info,
        info_key_services=INFO_KEY_SERVICES,
    )
    if service_info_reply is not None:
        if decision.primary_intent == IntentType.SERVICE_INFO and extract_date_from_message(message) and is_in_flow(session_id):
            return handle_appointment_booking(message, session_id)
        # Dodaj disclaimer za simptomska/zdravstvena vprašanja (direktni keyword check, ne izključuje vprašanj)
        _msg_l = message.lower()
        _symptom_cues_router = [
            "boli", "boleč", "bolec", "bola", "srbi", "peče", "pece", "krvavi",
            "otek", "otekl", "rdečin", "rdecin", "madež", "madez", "suha",
            "lušči", "lusci", "vidim slabo", "slabo vid", "po operacij",
            "rehabilitacij", "poškodb", "poskodb", "glivic", "izpusc", "izpušč",
        ]
        if any(c in _msg_l for c in _symptom_cues_router) or _looks_like_medical_statement(message):
            _disclaimer = "\n\n⚠️ *To je splošna usmeritev, ne zdravniški nasvet ali diagnoza. Za natančno oceno se posvetujte z zdravnikom.*"
            if "zdravniški nasvet" not in service_info_reply and "diagnoza" not in service_info_reply:
                service_info_reply = service_info_reply.rstrip() + _disclaimer
        return service_info_reply

    unsupported_reply = handle_unsupported_symptom_intent(
        intent=decision.primary_intent,
        message=message,
        clinic_id=clinic_id,
        session_id=session_id,
        state_mgr=state_mgr,
        is_in_flow=is_in_flow,
        match_unsupported_symptom=_match_unsupported_symptom,
        get_response=get_response,
        get_domain_response=get_domain_response,
    )
    if unsupported_reply is not None:
        return unsupported_reply

    # Symptom statement that did not route to SERVICE_INFO:
    # use quick triage instead of generic fallback.
    if decision.primary_intent == IntentType.GENERAL and not is_in_flow(session_id):
        triage_fallback = _quick_triage_fallback(message, clinic_id=clinic_id)
        if triage_fallback:
            return triage_fallback

    price_reply = handle_price_intent(
        intent=decision.primary_intent,
        clinic_id=clinic_id,
        decision_service=decision.service_type,
        suggested_service=suggested_service,
        appointment_state=appointment_state,
        state_mgr=state_mgr,
        service_price_info=_service_price_info,
        extract_service_type=extract_service_type,
        rag_info_answer=_rag_info_answer,
        message=message,
        info_key_prices=INFO_KEY_PRICES,
    )
    if price_reply is not None:
        return price_reply

    info_reply = handle_info_intent(
        intent=decision.primary_intent,
        message=message,
        clinic_id=clinic_id,
        decision_service=decision.service_type,
        suggested_service=suggested_service,
        appointment_state=appointment_state,
        pick_info_key=pick_info_key,
        get_info_response=_get_info_response,
        rag_info_answer=_rag_info_answer,
        contact_route_words=CONTACT_ROUTE_WORDS,
        critical_info_keys=CRITICAL_INFO_KEYS,
        info_keys_direct=INFO_KEYS_DIRECT,
        info_key_contact=INFO_KEY_CONTACT,
        info_key_location=INFO_KEY_LOCATION,
        info_key_services=INFO_KEY_SERVICES,
    )
    if info_reply is not None:
        return info_reply

    # For other intents, fall back to legacy system
    return None


@router.post("/", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Main chat endpoint (D4): unified router only, delegated orchestrator."""
    global conversation_history, last_interaction, chat_session_id

    state = {
        "appointment_states": appointment_states,
        "conversation_history": conversation_history,
        "last_interaction": last_interaction,
        "chat_session_id": chat_session_id,
        "last_user_message_by_session": last_user_message_by_session,
    }

    deps = SimpleNamespace(
        os=os,
        resolve_clinic_id=resolve_clinic_id,
        list_available_clinics=list_available_clinics,
        set_current_clinic_id=set_current_clinic_id,
        reset_current_clinic_id=reset_current_clinic_id,
        StateManager=StateManager,
        reset_appointment_state=reset_appointment_state,
        reset_unified_state=reset_unified_state,
        extract_service_type=extract_service_type,
        service_price_info=_service_price_info,
        is_in_flow=is_in_flow,
        build_interrupt_response=build_interrupt_response,
        get_current_step=get_current_step,
        format_response=format_response,
        get_response=get_response,
        get_fast_pass_match=get_fast_pass_match,
        INFO_KEY_PRICES=INFO_KEY_PRICES,
        get_appointment_data=get_appointment_data,
        get_appointment_state=get_appointment_state,
        conversation_tracker=conversation_tracker,
        looks_like_uncertain_help_request=_looks_like_uncertain_help_request,
        default_help_prompt=_default_help_prompt,
        handle_unified_routing=handle_unified_routing,
        handle_appointment_booking=handle_appointment_booking,
        response_cache=response_cache,
        is_tourist_query=is_tourist_query,
        answer_tourist_question=answer_tourist_question,
        answer_with_hybrid_kb=answer_with_hybrid_kb,
        get_uncertain_marker=_get_uncertain_marker,
        unified_route=unified_route,
        get_unified_state=get_unified_state,
        save_chat_message=save_chat_message,
        normalize_user_message=normalize_user_message,
    )

    # Handle booking form submission before normal chat processing
    if request.booking_form and (request.booking_form.name or request.booking_form.phone):
        bf = request.booking_form
        try:
            rs = ReservationService()
            rs.create_reservation(
                date=bf.date or "",
                people=1,
                reservation_type="appointment",
                source="widget_form",
                name=bf.name,
                phone=bf.phone,
                email=bf.email,
                note=bf.note,
                service_type=bf.service,
            )
        except Exception:
            pass
        return ChatResponse(
            reply="Hvala za prijavo! Kontaktirali vas bomo za potrditev termina.",
            session_id=request.session_id,
        )

    response = process_chat_turn(request=request, state=state, deps=deps)

    # Propagate potentially reassigned state holders.
    conversation_history = state["conversation_history"]
    last_interaction = state["last_interaction"]
    chat_session_id = state["chat_session_id"]

    return response


# ============================================================
# SMS WEBHOOK ENDPOINT - za Twilio incoming SMS
# ============================================================

from fastapi import Form, Response, UploadFile, File


# ============================================================
# VOICE INPUT ENDPOINT - Whisper transkripcija
# ============================================================

@router.post("/voice")
async def voice_input(
    file: UploadFile = File(...),
    session_id: str = None,
    clinic_id: str = None
):
    """
    Accepts a voice message, transcribes with Whisper, and returns a reply.

    Supported formats: mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg
    Max size: 25 MB
    """
    from app.services.voice_service import get_voice_service

    voice_service = get_voice_service()

    # Check availability
    if not voice_service.is_available():
        return {
            "success": False,
            "error": get_response("general.voice_unavailable", clinic_id=clinic_id),
            "transcription": None,
            "reply": None
        }

    try:
        # Read file content
        content = await file.read()

        # Validate
        validation = voice_service.validate_audio_file(file.filename or "audio.wav", len(content))
        if not validation["valid"]:
            return {
                "success": False,
                "error": validation["error"],
                "transcription": None,
                "reply": None
            }

        # Transcribe
        result = await voice_service.transcribe_from_bytes(
            content,
            file.filename or "audio.wav"
        )

        if not result["success"]:
            return {
                "success": False,
                "error": result.get("error", get_response("general.voice_transcription_error", clinic_id=clinic_id)),
                "transcription": None,
                "reply": None
            }

        transcribed_text = result["text"]

        # Process transcribed text through chat
        from app.models.chat import ChatRequest
        chat_request = ChatRequest(
            message=transcribed_text,
            session_id=session_id,
            clinic_id=clinic_id,
        )

        # Use existing chat logic
        chat_response = await chat(chat_request)

        return {
            "success": True,
            "transcription": transcribed_text,
            "reply": chat_response.reply,
            "session_id": chat_response.session_id,
            "duration_seconds": result.get("duration_seconds")
        }

    except Exception as e:
        print(f"[VOICE] Error: {e}")
        return {
            "success": False,
            "error": get_response("general.voice_processing_error", clinic_id=clinic_id, error=str(e)),
            "transcription": None,
            "reply": None
        }


@router.post("/sms-webhook")
async def sms_webhook(
    request: Request,
    From: str = Form(default=""),
    Body: str = Form(default=""),
    To: str = Form(default=""),
    MessageSid: str = Form(default=""),
):
    """
    Twilio webhook - processes patient SMS responses to reminders.

    Patients can reply:
    - YES / OK → Confirm appointment
    - RESCHEDULE → Request a new time
    - CANCEL / NO → Cancel appointment

    Twilio Console:
    - Webhook URL: https://yourdomain.com/chat/sms-webhook
    - HTTP Method: POST
    """
    try:
        from app.services.reminder_scheduler import handle_sms_response
        from app.services.sms_service import send_sms

        form = await request.form()
        from_value = (From or form.get("From") or form.get("from") or form.get("sender") or form.get("phone") or "").strip()
        body_value = (Body or form.get("Body") or form.get("body") or form.get("message") or form.get("text") or form.get("m") or "").strip()
        to_value = (To or form.get("To") or form.get("to") or "").strip()
        sid_value = (MessageSid or form.get("MessageSid") or form.get("message_id") or form.get("smsId") or form.get("id") or "").strip()

        if not from_value or not body_value:
            print(f"[SMS WEBHOOK] Missing sender/body. form_keys={list(form.keys())}")
            twiml = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
            return Response(content=twiml, media_type="application/xml")

        print(f"[SMS WEBHOOK] Received from {from_value}: {body_value} (SID: {sid_value})")
        reservation_service = ReservationService()
        reservation = reservation_service.find_latest_reservation_by_phone(from_value)
        reservation_id = reservation.get("id") if reservation else None

        if reservation_id:
            reservation_service.add_reservation_message(
                reservation_id=reservation_id,
                direction="inbound",
                channel="sms",
                subject="Prejet SMS",
                body=body_value,
                from_phone=from_value,
                to_phone=to_value,
                message_id=sid_value or None,
                provider_message_sid=sid_value or None,
            )

        sms_text = body_value
        sms_text_l = sms_text.lower()
        quick_actions = {"da", "yes", "ok", "pridem", "potrjujem", "prestav", "prestavi", "odpovej", "odpoved", "cancel", "ne"}

        # 1) Quick actions for reminder flow (DA/PRESTAVI/ODPOVEJ)
        if any(token in sms_text_l for token in quick_actions):
            result = handle_sms_response(from_value, sms_text)
            reply_text = result.get("response_message") or ""
        else:
            # 2) Generic SMS question routed through the same chat brain
            sms_session_id = f"sms:{''.join(ch for ch in str(from_value) if ch.isdigit())}"
            chat_req = ChatRequest(message=sms_text, session_id=sms_session_id, clinic_id=None)
            chat_resp = await chat(chat_req)
            reply_text = (chat_resp.reply or "").strip()

        if not reply_text:
            reply_text = "Hvala za sporočilo. Za pomoč pokličite 04 271 30 10."
        send_result = send_sms(from_value, reply_text)

        if reservation_id and reply_text:
            reservation_service.add_reservation_message(
                reservation_id=reservation_id,
                direction="outbound",
                channel="sms",
                subject="SMS odgovor",
                body=reply_text,
                from_phone=to_value,
                to_phone=from_value,
                message_id=None,
                provider_message_sid=send_result.get("message_sid"),
            )

        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        return Response(content=twiml, media_type="application/xml")

    except Exception as e:
        print(f"[SMS WEBHOOK] Error: {e}")
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        return Response(content=twiml, media_type="application/xml")
