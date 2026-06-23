from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException

from app.models.chat import ChatRequest, ChatResponse
from app.services.fallback_pipeline import (
    handle_fast_pass,
    handle_loop_guard,
    handle_price_followup,
    resolve_response_fallback,
)
from app.services.routing.ood_policy import check_ood as ood_check

_PURE_GREETINGS = {
    "zdravo", "zdravjo", "zdwavo", "živjo", "zivjo", "pozdravljeni",
    "dober dan", "dobro jutro", "hello", "hej", "halo", "bok", "ej",
    "hi", "hey",
}


def _is_pure_greeting(text: str) -> bool:
    t = text.lower().strip().rstrip("!.,?")
    return t in _PURE_GREETINGS


def process_chat_turn(*, request: ChatRequest, state: dict[str, Any], deps: Any) -> ChatResponse:
    raw_message = request.message.strip()
    normalize_user_message = getattr(deps, "normalize_user_message", None)
    message = normalize_user_message(raw_message) if callable(normalize_user_message) else raw_message
    raw_session_id = request.session_id or state["chat_session_id"]
    strict_clinic = deps.os.getenv("STRICT_CLINIC_ID", "false").strip().lower() in {"1", "true", "yes", "on"}
    try:
        clinic_id = deps.resolve_clinic_id(request.clinic_id, strict=strict_clinic)
    except ValueError:
        available = deps.list_available_clinics()
        raise HTTPException(status_code=400, detail={"error": "unknown_clinic_id", "available": available})

    token = deps.set_current_clinic_id(clinic_id)
    try:
        session_id = f"{clinic_id}::{raw_session_id}"
        state_mgr = deps.StateManager(session_id)

        if not raw_message:
            payload = deps.format_response(
                deps.get_response("general.empty_message", clinic_id=clinic_id),
                state_manager=state_mgr,
                metadata={"contract_version": "v0.1", "router": "unified_only"},
            )
            return ChatResponse(reply=payload["text"], session_id=raw_session_id, metadata=payload["metadata"])

        # Short-circuit pure greetings before fast_pass / RAG to avoid doctor-list response
        if _is_pure_greeting(raw_message):
            greeting_text = deps.get_response("general.greeting", clinic_id=clinic_id)
            payload = deps.format_response(
                greeting_text,
                state_manager=state_mgr,
                metadata={"contract_version": "v0.1", "router": "greeting_guard"},
            )
            return ChatResponse(reply=payload["text"], session_id=raw_session_id, metadata=payload["metadata"])

        now = datetime.now()
        if state["last_interaction"] and (now - state["last_interaction"]).total_seconds() > 3600:
            state["conversation_history"] = []
            if session_id in state["appointment_states"]:
                deps.reset_appointment_state(state["appointment_states"][session_id])
            deps.reset_unified_state(session_id)
        state["last_interaction"] = now

        price_followup_response = handle_price_followup(
            message=message,
            raw_session_id=raw_session_id,
            session_id=session_id,
            clinic_id=clinic_id,
            state_mgr=state_mgr,
            deps=deps,
        )
        if price_followup_response is not None:
            return price_followup_response

        fast_pass_response = handle_fast_pass(
            message=message,
            raw_session_id=raw_session_id,
            session_id=session_id,
            clinic_id=clinic_id,
            state_mgr=state_mgr,
            deps=deps,
        )
        if fast_pass_response is not None:
            return fast_pass_response

        loop_guard_response = handle_loop_guard(
            message=message,
            raw_session_id=raw_session_id,
            session_id=session_id,
            clinic_id=clinic_id,
            state_mgr=state_mgr,
            deps=deps,
        )
        if loop_guard_response is not None:
            return loop_guard_response

        # ── OOD Policy Guard ────────────────────────────────────────────────
        # Check for out-of-domain messages early in the pipeline
        unified_state = deps.get_unified_state(session_id)
        ood_result = ood_check(
            message,
            rag_similarity=None,
            session_data={
                "flow_type": unified_state.get("flow"),
                "step": unified_state.get("step"),
            },
        )
        if ood_result.is_ood and ood_result.response:
            payload = deps.format_response(
                ood_result.response,
                state_manager=state_mgr,
                metadata={"contract_version": "v0.1", "router": "ood_guard", "ood_level": ood_result.level.value},
            )
            return ChatResponse(reply=payload["text"], session_id=raw_session_id, metadata=payload["metadata"])

        deps.conversation_tracker.add_message(session_id, message)

        response_text = deps.handle_unified_routing(message, session_id, clinic_id=clinic_id)

        if response_text is None and deps.is_in_flow(session_id):
            response_text = deps.handle_appointment_booking(message, session_id)

        if response_text is None:
            response_text = resolve_response_fallback(
                message=message,
                session_id=session_id,
                clinic_id=clinic_id,
                conversation_history=state["conversation_history"],
                deps=deps,
            )

        state["conversation_history"].append({"role": "user", "content": raw_message})
        state["conversation_history"].append({"role": "assistant", "content": response_text})
        state["last_user_message_by_session"][session_id] = raw_message
        if len(state["conversation_history"]) > 20:
            state["conversation_history"] = state["conversation_history"][-20:]

        decision = deps.unified_route(message, deps.get_unified_state(session_id))
        metadata = {
            "contract_version": "v0.1",
            "router": "unified_only",
            "intent": decision.primary_intent.value,
            "confidence": round(float(decision.confidence), 3),
            "action": decision.action.value,
        }
        ui_override = state_mgr.get_context_value("ui_override")
        if ui_override:
            metadata["ui"] = ui_override

        try:
            flow_state = deps.get_unified_state(session_id)
            appointment_state = deps.get_appointment_state(session_id)
            current_step = appointment_state.get("step") if appointment_state.get("step") is not None else None
            metadata["flow"] = flow_state.get("flow")
            metadata["booking_step"] = current_step
        except Exception as e:
            print(f"[UI_CONTRACT] Failed to build UI payload: {e}")

        try:
            ap_state = deps.get_appointment_state(session_id)
            current_step = ap_state.get("step") if ap_state.get("step") is not None else None
            deps.save_chat_message(
                session_id=session_id,
                role="user",
                content=raw_message,
                intent=decision.primary_intent.value,
                booking_step=current_step,
                response_cached=False,
                metadata={"normalized_message": message} if message != raw_message else None,
            )
            deps.save_chat_message(
                session_id=session_id,
                role="assistant",
                content=response_text,
                booking_step=current_step,
                metadata=metadata,
            )
        except Exception as e:
            print(f"[CHAT_HISTORY] Failed to save conversation: {e}")

        payload = deps.format_response(response_text, state_manager=state_mgr, metadata=metadata)
        if ui_override:
            state_mgr.clear_context_key("ui_override")
        return ChatResponse(reply=payload["text"], session_id=raw_session_id, metadata=payload["metadata"])
    finally:
        deps.reset_current_clinic_id(token)
