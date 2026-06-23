"""
SMS Service za Zdravstveni Center

Podpira:
- Twilio API za produkcijsko pošiljanje SMS
- Mock mode za testiranje brez pravega API-ja

Uporaba:
    from app.services.sms_service import send_sms, send_appointment_reminder

    # Pošlji generičen SMS
    send_sms("+38640123456", "Vaš termin je jutri ob 10:00")

    # Pošlji opomin za termin
    send_appointment_reminder(reservation_id, hours_before=2)
"""
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from enum import Enum
import requests

# Twilio integration (optional)
try:
    from twilio.rest import Client as TwilioClient
    HAS_TWILIO = True
except ImportError:
    HAS_TWILIO = False
    print("[SMS] Twilio not installed. Run: pip install twilio")

# Configuration from environment
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "twilio").strip().lower()  # twilio | smsapi
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")  # +1234567890
SMSAPI_BASE_URL = os.getenv("SMSAPI_BASE_URL", "https://api.smsapi.com").strip().rstrip("/")
SMSAPI_OAUTH_TOKEN = os.getenv("SMSAPI_OAUTH_TOKEN", os.getenv("SMSAPI_ACCESS_TOKEN", "")).strip()
SMSAPI_SENDER = os.getenv("SMSAPI_SENDER", "").strip()
SMS_MOCK_MODE = os.getenv("SMS_MOCK_MODE", "true").lower() in ("true", "1", "yes")

# Clinic info
CLINIC_NAME = os.getenv("CLINIC_NAME", "Zdravstveni center d.o.o.")
CLINIC_PHONE = os.getenv("CLINIC_PHONE", "04 271 30 10")


class ReminderType(Enum):
    """Tipi opomnikov"""
    PRE_VISIT_3_DAYS = "pre_visit_3_days"
    PRE_VISIT_2_HOURS = "pre_visit_2_hours"
    POST_VISIT_1_DAY = "post_visit_1_day"
    CONFIRMATION = "confirmation"
    CANCELLATION = "cancellation"
    BOOKING_RECEIVED = "booking_received"


# SMS Templates
SMS_TEMPLATES = {
    ReminderType.PRE_VISIT_3_DAYS: """
{clinic_name}: Spomin - {service_name} cez 3 dni ({date} ob {time}).

Prinesite:
- Zdravstveno kartico
- Osebni dokument

Vpr? Pokličite {phone}
""".strip(),

    ReminderType.PRE_VISIT_2_HOURS: """
{clinic_name}: Danes ob {time} imate {service_name}.

Prišli boste?
Odg. DA, PRESTAVI ali ODPOVEJ

Lokacija: Zdraviliška 12, Ljubljana
""".strip(),

    ReminderType.POST_VISIT_1_DAY: """
{clinic_name}: Kako ste po včerajšnjem pregledu?

Če imate vprašanja, nas kontaktirajte:
Tel: {phone}
Email: info@zc-kranj.si
""".strip(),

    ReminderType.CONFIRMATION: """
{clinic_name}: Termin POTRJEN!

{service_name}
{date} ob {time}

Naslov: Zdraviliška 12, Ljubljana
Parkirišče: Brezplačno pred objektom

Pridite 10 min prej.
""".strip(),

    ReminderType.CANCELLATION: """
{clinic_name}: Termin ODPOVEDAN.

{service_name}
{date} ob {time}

Za nov termin: {phone} ali chat
""".strip(),

    ReminderType.BOOKING_RECEIVED: """
{clinic_name}: Naročilo prejeto.

{service_name}
{date} ob {time}

Potrditev termina prejmete kmalu.
Info: {phone}
""".strip(),
}


def _format_phone(phone: str) -> str:
    """
    Normaliziraj telefonsko številko v mednarodni format.

    Examples:
        040123456 → +38640123456
        +38640123456 → +38640123456
        00386 40 123 456 → +38640123456
    """
    # Remove spaces, dashes, parentheses
    phone = "".join(c for c in phone if c.isdigit() or c == "+")

    # Handle Slovenian numbers
    if phone.startswith("0") and not phone.startswith("00"):
        # Local format (040...) → International
        phone = "+386" + phone[1:]
    elif phone.startswith("00386"):
        phone = "+" + phone[2:]
    elif not phone.startswith("+"):
        # Assume Slovenian if no prefix
        phone = "+386" + phone

    return phone


def _get_twilio_client() -> Optional["TwilioClient"]:
    """Vrne Twilio client če je konfiguriran."""
    if not HAS_TWILIO:
        return None

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        print("[SMS] Twilio credentials not configured")
        return None

    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def _send_via_twilio(formatted_phone: str, message: str, result: Dict[str, Any]) -> Dict[str, Any]:
    client = _get_twilio_client()
    if not client:
        result["error"] = "Twilio client not available"
        return result

    if not TWILIO_PHONE_NUMBER:
        result["error"] = "TWILIO_PHONE_NUMBER not configured"
        return result

    try:
        twilio_message = client.messages.create(
            body=message,
            from_=TWILIO_PHONE_NUMBER,
            to=formatted_phone
        )
        result["success"] = True
        result["message_sid"] = twilio_message.sid
        print(f"[SMS/TWILIO] Sent to {formatted_phone}: {twilio_message.sid}")
    except Exception as e:
        result["error"] = str(e)
        print(f"[SMS/TWILIO] Error sending to {formatted_phone}: {e}")
    return result


def _send_via_smsapi(formatted_phone: str, message: str, result: Dict[str, Any]) -> Dict[str, Any]:
    if not SMSAPI_OAUTH_TOKEN:
        result["error"] = "SMSAPI_OAUTH_TOKEN not configured"
        return result

    endpoint = f"{SMSAPI_BASE_URL}/sms.do"
    to_digits = "".join(ch for ch in formatted_phone if ch.isdigit())
    payload = {
        "to": to_digits,
        "message": message,
        "format": "json",
    }
    if SMSAPI_SENDER:
        payload["from"] = SMSAPI_SENDER

    try:
        response = requests.post(
            endpoint,
            data=payload,
            headers={"Authorization": f"Bearer {SMSAPI_OAUTH_TOKEN}"},
            timeout=15,
        )
        if response.status_code != 200:
            result["error"] = f"SMSAPI HTTP {response.status_code}: {response.text[:300]}"
            print(f"[SMS/SMSAPI] {result['error']}")
            return result

        message_sid = None
        try:
            data = response.json()
            if isinstance(data, list) and data:
                first = data[0] or {}
                message_sid = first.get("id") or first.get("message_id")
                if first.get("error"):
                    result["error"] = f"SMSAPI error: {first.get('error')}"
                    return result
            elif isinstance(data, dict):
                message_sid = data.get("id") or data.get("message_id")
                if data.get("error"):
                    result["error"] = f"SMSAPI error: {data.get('error')}"
                    return result
        except Exception:
            raw = (response.text or "").strip()
            if raw.lower().startswith("error"):
                result["error"] = f"SMSAPI error: {raw[:300]}"
                return result
            message_sid = raw[:120] if raw else None

        result["success"] = True
        result["message_sid"] = message_sid or f"SMSAPI_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        print(f"[SMS/SMSAPI] Sent to {formatted_phone}: {result['message_sid']}")
    except Exception as e:
        result["error"] = str(e)
        print(f"[SMS/SMSAPI] Error sending to {formatted_phone}: {e}")
    return result


def send_sms(
    to_phone: str,
    message: str,
    mock_override: Optional[bool] = None
) -> Dict[str, Any]:
    """
    Pošlje SMS sporočilo.

    Args:
        to_phone: Telefonska številka prejemnika
        message: Besedilo sporočila (max 160 znakov za 1 SMS)
        mock_override: Če True, ne pošlji dejansko (za testiranje)

    Returns:
        {
            "success": bool,
            "message_sid": str or None,
            "error": str or None,
            "mock": bool
        }
    """
    use_mock = mock_override if mock_override is not None else SMS_MOCK_MODE
    formatted_phone = _format_phone(to_phone)

    # Truncate message if too long
    if len(message) > 320:
        message = message[:317] + "..."

    result = {
        "success": False,
        "message_sid": None,
        "error": None,
        "mock": use_mock,
        "provider": SMS_PROVIDER,
        "to": formatted_phone,
        "message_length": len(message),
    }

    if use_mock:
        # Mock mode - just log
        print(f"[SMS MOCK] To: {formatted_phone}")
        print(f"[SMS MOCK] Message ({len(message)} chars):")
        print(f"[SMS MOCK] {message}")
        print(f"[SMS MOCK] ---")
        result["success"] = True
        result["message_sid"] = "MOCK_" + datetime.now().strftime("%Y%m%d%H%M%S")
        return result

    # Production mode
    if SMS_PROVIDER == "smsapi":
        return _send_via_smsapi(formatted_phone, message, result)
    return _send_via_twilio(formatted_phone, message, result)


def send_appointment_reminder(
    reservation: Dict[str, Any],
    reminder_type: ReminderType,
    mock_override: Optional[bool] = None
) -> Dict[str, Any]:
    """
    Pošlje opomin za termin.

    Args:
        reservation: Rezervacija iz database (mora vsebovati phone, date, time, service_type)
        reminder_type: Tip opomnika (pre-visit, post-visit, etc.)
        mock_override: Če True, ne pošlji dejansko

    Returns:
        Rezultat send_sms()
    """
    phone = reservation.get("phone")
    if not phone:
        return {"success": False, "error": "No phone number in reservation"}

    # Get service name
    service_type = reservation.get("service_type", "pregled")
    service_name = reservation.get("location") or service_type  # location holds service name

    # Format template
    template = SMS_TEMPLATES.get(reminder_type, SMS_TEMPLATES[ReminderType.CONFIRMATION])

    message = template.format(
        clinic_name=CLINIC_NAME,
        service_name=service_name,
        date=reservation.get("date", ""),
        time=reservation.get("time", ""),
        phone=CLINIC_PHONE,
        patient_name=reservation.get("name", "")
    )

    return send_sms(phone, message, mock_override)


def send_quick_action_reminder(
    reservation: Dict[str, Any],
    hours_before: int = 2,
    mock_override: Optional[bool] = None
) -> Dict[str, Any]:
    """
    Pošlje opomin z quick actions (DA/PRESTAVI/ODPOVEJ).

    Posebej za 2h pred terminom ko želimo potrditev prihoda.
    """
    phone = reservation.get("phone")
    if not phone:
        return {"success": False, "error": "No phone number"}

    service_name = reservation.get("location") or reservation.get("service_type", "pregled")
    time_str = reservation.get("time", "")

    message = f"""{CLINIC_NAME}: Danes ob {time_str} imate {service_name}.

Prišli boste?
Odg: DA, PRESTAVI ali ODPOVEJ

Naslov: Zdraviliška 12, Ljubljana"""

    return send_sms(phone, message, mock_override)


def process_sms_response(
    from_phone: str,
    message_body: str,
    reservation_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Procesira odgovor na SMS (DA/PRESTAVI/ODPOVEJ).

    Args:
        from_phone: Telefonska številka pošiljatelja
        message_body: Besedilo odgovora
        reservation_id: ID rezervacije (če znan)

    Returns:
        {
            "action": "confirm" | "reschedule" | "cancel" | "unknown",
            "message": str  # Odgovor za uporabnika
        }
    """
    body_lower = message_body.strip().lower()

    # Detect action
    if body_lower in ("da", "yes", "pridem", "potrjujem", "ok", "prihajam"):
        return {
            "action": "confirm",
            "message": f"{CLINIC_NAME}: Hvala za potrditev! Vidimo se ob dogovorjenem terminu."
        }

    elif body_lower in ("prestavi", "prestaviti", "premik", "drug termin"):
        return {
            "action": "reschedule",
            "message": f"{CLINIC_NAME}: Razumemo. Za nov termin pokličite {CLINIC_PHONE} ali uporabite chat."
        }

    elif body_lower in ("odpovej", "odpovedati", "ne", "cancel", "ne pridem"):
        return {
            "action": "cancel",
            "message": f"{CLINIC_NAME}: Termin odpovedan. Za nov termin smo na voljo."
        }

    else:
        return {
            "action": "unknown",
            "message": f"{CLINIC_NAME}: Odgovorite z DA, PRESTAVI ali ODPOVEJ. Za pomoč: {CLINIC_PHONE}"
        }


# Convenience functions for specific reminder types
def send_3_day_reminder(reservation: Dict[str, Any]) -> Dict[str, Any]:
    """Pošlje opomin 3 dni pred terminom."""
    return send_appointment_reminder(reservation, ReminderType.PRE_VISIT_3_DAYS)


def send_2_hour_reminder(reservation: Dict[str, Any]) -> Dict[str, Any]:
    """Pošlje opomin 2 uri pred terminom z quick actions."""
    return send_quick_action_reminder(reservation, hours_before=2)


def send_post_visit_followup(reservation: Dict[str, Any]) -> Dict[str, Any]:
    """Pošlje follow-up 1 dan po obisku."""
    return send_appointment_reminder(reservation, ReminderType.POST_VISIT_1_DAY)


def send_confirmation_sms(reservation: Dict[str, Any]) -> Dict[str, Any]:
    """Pošlje SMS potrditev ob ustvarjanju rezervacije."""
    return send_appointment_reminder(reservation, ReminderType.CONFIRMATION)


def send_cancellation_sms(reservation: Dict[str, Any]) -> Dict[str, Any]:
    """Pošlje SMS ob preklicu rezervacije."""
    return send_appointment_reminder(reservation, ReminderType.CANCELLATION)


def send_booking_received_sms(reservation: Dict[str, Any]) -> Dict[str, Any]:
    """Pošlje SMS, da je naročilo prejeto (pred končno potrditvijo)."""
    return send_appointment_reminder(reservation, ReminderType.BOOKING_RECEIVED)
