"""
Email Service za zdravstveni center

STANJE: PRIPRAVLJEN, NEAKTIVEN
- Koda je pripravljena za pošiljanje emailov
- NI povezana z rezervacijskim flowom
- Ko boš pripravljen, aktiviraj v chat_router.py

AKTIVACIJA:
1. Nastavi SMTP credentials v .env
2. V chat_router.py odkomentiraj klic send_reservation_email()

SMTP NASTAVITVE (.env):
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=mr@medilab.si
SMTP_PASSWORD=your_app_password
SMTP_FROM_EMAIL=mr@medilab.si
SMTP_FROM_NAME=Medilab d.o.o.
ADMIN_EMAIL=mr@medilab.si
"""

import os
import smtplib
import resend
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import uuid

# ============================================================
# KONFIGURACIJA - bere iz .env ali environment variables
# ============================================================

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "mr@medilab.si")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Medilab d.o.o.")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "mr@medilab.si")
SMTP_SSL = os.getenv("SMTP_SSL", "").strip().lower() in {"1", "true", "yes"}
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")
SUBJECT_PREFIX = os.getenv("SUBJECT_PREFIX", "").strip()

# Brand barve (enake kot WordPress)
BRAND_COLOR = "#7b5e3b"
BORDER_COLOR = "#e8e0d8"
BG_COLOR = "#f7f3ee"
TEXT_COLOR = "#1b1f1a"
MUTED_COLOR = "#6a6a6a"

# ============================================================
# HTML TEMPLATES
# ============================================================

def _email_wrapper(content: str) -> str:
    """Zavije vsebino v branded HTML template."""
    return f"""
<!DOCTYPE html>
<html lang="sl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Medilab d.o.o.</title>
</head>
<body style="margin:0; padding:0; background:#faf9f7; font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
    <div style="max-width:620px; margin:0 auto; padding:24px 16px;">
        <!-- Header -->
        <div style="background:{BRAND_COLOR}; color:#fff; padding:18px 24px; border-radius:14px 14px 0 0; font-size:18px; font-weight:700; letter-spacing:.3px;">
            Medilab d.o.o.
        </div>
        
        <!-- Content -->
        <div style="background:#fff; border:1px solid {BORDER_COLOR}; border-top:none; padding:24px; color:{TEXT_COLOR}; font-size:15px; line-height:1.6;">
            {content}
        </div>
        
        <!-- Footer -->
        <div style="background:{BG_COLOR}; border:1px solid {BORDER_COLOR}; border-top:none; border-radius:0 0 14px 14px; padding:16px 24px; color:{MUTED_COLOR}; font-size:12px;">
            Medilab d.o.o.
        </div>
    </div>
</body>
</html>
"""


def _kv_table(rows: Dict[str, str]) -> str:
    """Generira HTML tabelo s key-value pari."""
    html = f"""
    <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%; border:1px solid {BORDER_COLOR}; border-radius:10px; overflow:hidden; font-size:14px; margin:16px 0;">
    """
    items = list(rows.items())
    for i, (key, value) in enumerate(items):
        is_last = i == len(items) - 1
        border_bottom = "0" if is_last else f"1px solid {BORDER_COLOR}"
        html += f"""
        <tr>
            <td style="background:#f9f7f5; padding:10px 12px; width:40%; border-bottom:{border_bottom}; color:#444;">
                <strong>{key}</strong>
            </td>
            <td style="padding:10px 12px; border-bottom:{border_bottom}; color:#111;">
                {value if value else '—'}
            </td>
        </tr>
        """
    html += "</table>"
    return html


# ============================================================
# EMAIL TEMPLATES
# ============================================================

def _guest_room_confirmation_html(data: Dict[str, Any]) -> str:
    """Email gostu - potrditev rezervacije SOBE."""
    content = f"""
    <p>Pozdravljeni <strong>{data.get('name', 'gost')}</strong>,</p>
    
    <p>Hvala za vaše povpraševanje. Posredujemo vam povzetek:</p>
    
    {_kv_table({
        'Datum prihoda': data.get('date', ''),
        'Število nočitev': str(data.get('nights', '')),
        'Število oseb': str(data.get('people', '')),
        'Otroci': f"{data.get('kids', '')} otrok ({data.get('kids_ages', '')})" if data.get('kids') else "",
        'Število sob': str(data.get('rooms', '')),
        'Soba': data.get('location', ''),
        'Kontakt': data.get('phone', ''),
        'Email': data.get('email', ''),
        'Opombe': data.get('note', ''),
    })}
    
    <p style="margin-top:18px;">
        <strong>Cena:</strong> 50€/nočitev na odraslo osebo<br>
        <strong>Zajtrk:</strong> vključen (8:00-9:00)<br>
        <strong>Večerja:</strong> 25€/oseba (po želji)<br>
        <strong>Otroci do 4 let:</strong> brezplačno<br>
        <strong>Otroci 4-12 let:</strong> 50% popust
    </p>
    
    <p style="margin-top:18px;">
        Prijava od 14:00, odjava do 10:00.<br>
        Večerja ob 18:00 (ponedeljki in torki brez večerij).
    </p>
    
    <p><strong>POMEMBNO:</strong> To je povpraševanje, ne potrjena rezervacija.<br>
    Potrditev boste prejeli po pregledu.</p>
    
    <p style="margin-top:18px; color:{MUTED_COLOR};">
        Rezervacijo bomo potrdili po preverjanju razpoložljivosti.<br>
        Za spremembe ali preklic nas kontaktirajte na 04 271 30 10 ali mr@medilab.si
    </p>
    """
    return _email_wrapper(content)


def _guest_table_confirmation_html(data: Dict[str, Any]) -> str:
    """Email gostu - potrditev naročila pregleda v zdravstvenem centru."""
    # Pridobi service type iz podatkov
    service_type = data.get('service_type', '').upper()
    service_names = {
        'DERMATOLOG': 'Dermatološki pregled',
        'ORTOPED': 'Ortopedski pregled',
        'OKULIST': 'Okulistični pregled',
        'LASERSKI_POSEG': 'Laserski poseg',
        'ESTETSKI_POSEG': 'Estetski poseg',
        'KOZMETIKA': 'Kozmetični salon',
    }
    service_display = service_names.get(service_type, data.get('location', 'Pregled'))

    content = f"""
    <p>Pozdravljeni <strong>{data.get('name', 'pacient')}</strong>,</p>

    <p>Hvala za vaše naročilo. Posredujemo vam povzetek:</p>

    {_kv_table({
        'Vrsta pregleda': service_display,
        'Datum': data.get('date', ''),
        'Ura': data.get('time', ''),
        'Trajanje': f"{data.get('duration_minutes', 30)} minut",
        'Kontakt': data.get('phone', ''),
        'Email': data.get('email', ''),
        'Razlog obiska': data.get('reason', ''),
    })}

    <p style="margin-top:18px;">
        <strong>Navodila pred obiskom:</strong>
    </p>
    <ul style="margin:10px 0; padding-left:20px; color:#333;">
        <li>Prosimo, pridite 10 minut pred naročeno uro</li>
        <li>S seboj prinesite zdravstveno izkaznico (če jo imate)</li>
        <li>Če imate že narejene izvide ali slike (RTG, MRI, CT), jih prinesite</li>
    </ul>

    <p style="margin-top:18px;">
        <strong>POMEMBNO:</strong> To je naročilo, ki ga bomo potrdili po preverjanju razpoložljivosti.
    </p>

    <p style="margin-top:18px; color:{MUTED_COLOR};">
        V primeru preprečitve nas prosimo obvestite vsaj 24 ur vnaprej.<br>
        Kontakt: <strong>04 271 30 10</strong> ali <strong>mr@medilab.si</strong>
    </p>
    """
    return _email_wrapper(content)


def _admin_new_reservation_html(data: Dict[str, Any], confirm_url: str = "", reject_url: str = "") -> str:
    """Email adminu - novo naročilo čaka na obdelavo."""
    res_type = "Soba" if data.get('reservation_type') == 'room' else "Pregled"
    
    rows = {
        'ID': f"#{data.get('id', '?')}",
        'Tip': res_type,
        'Ime': data.get('name', ''),
        'Email': data.get('email', ''),
        'Telefon': data.get('phone', ''),
        'Datum': data.get('date', ''),
    }
    
    if data.get('reservation_type') == 'room':
        rows['Nočitve'] = str(data.get('nights', ''))
        rows['Sobe'] = str(data.get('rooms', ''))
    else:
        rows['Ura'] = data.get('time', '')
        rows['Vrsta pregleda/posega'] = data.get('location', '')
    
    rows['Osebe'] = str(data.get('people', ''))
    rows['Vir'] = data.get('source', 'chatbot')
    
    if data.get('note'):
        rows['Opomba'] = data.get('note', '')
    
    # Gumbi za akcije (če so URL-ji podani)
    action_buttons = ""
    if confirm_url and reject_url:
        action_buttons = f"""
        <div style="margin-top:20px;">
            <a href="{confirm_url}" style="display:inline-block; background:{BRAND_COLOR}; color:#fff; padding:12px 20px; border-radius:10px; text-decoration:none; font-weight:700; margin-right:10px;">
                ✅ Potrdi
            </a>
            <a href="{reject_url}" style="display:inline-block; background:#b42318; color:#fff; padding:12px 20px; border-radius:10px; text-decoration:none; font-weight:700;">
                ❌ Zavrni
            </a>
        </div>
        """
    
    content = f"""
    <p><strong>Nova rezervacija čaka na obdelavo:</strong></p>
    
    {_kv_table(rows)}
    
    {action_buttons}
    
    <p style="margin-top:20px; color:{MUTED_COLOR}; font-size:13px;">
        Rezervacija ustvarjena: {datetime.now().strftime('%d.%m.%Y %H:%M')}<br>
        Odpri admin panel: <a href="http://localhost:8000/admin" style="color:{BRAND_COLOR};">Admin rezervacije</a>
    </p>
    """
    return _email_wrapper(content)


def _guest_confirmed_html(data: Dict[str, Any]) -> str:
    """Email pacientu - termin POTRJEN."""
    service_type = data.get('location', 'Pregled')

    # Ikone po tipu pregleda
    icon_map = {
        'Ortopedski pregled': '🦴',
        'Dermatološki pregled': '🩺',
        'Okulistični pregled': '👁️',
        'Laserski poseg': '✨',
        'Estetski poseg': '💉',
        'Kozmetični salon': '💆',
        'Fizioterapija': '🏃',
    }
    icon = icon_map.get(service_type, '🏥')

    content = f"""
    <p>Pozdravljeni <strong>{data.get('name', 'pacient')}</strong>,</p>

    <p>Vaš termin je <strong style="color:#22c55e;">POTRJEN</strong> ✅</p>

    {_kv_table({
        f'{icon} Vrsta pregleda': service_type,
        '📅 Datum': data.get('date', ''),
        '🕐 Ura': data.get('time', ''),
        '📍 Lokacija': 'Medilab d.o.o., Partizanska 12, Kranj',
    })}

    <div style="background:#f0f9ff;border-left:4px solid #0891b2;padding:12px 16px;margin:20px 0;border-radius:4px;">
        <p style="margin:0;font-weight:600;color:#0891b2;margin-bottom:8px;">ℹ️ Navodila pred pregledom:</p>
        <ul style="margin:0;padding-left:20px;color:#134e4a;">
            <li>Prosimo pridite <strong>10 minut pred terminom</strong></li>
            <li>S seboj prinesite <strong>zdravstveno kartico</strong></li>
            <li>Če imate prejšnje izvide, jih prinesite s seboj</li>
        </ul>
    </div>

    <p style="margin-top:18px;">
        <strong>Za morebitne spremembe ali preklic:</strong><br>
        📞 04 271 30 10<br>
        ✉️ mr@medilab.si
    </p>

    <p style="color:{MUTED_COLOR};font-size:12px;margin-top:24px;">
        Priložen je tudi .ics koledar - dodajte termin v svoj koledar s klikom na priponko.
    </p>
    """
    return _email_wrapper(content)


def _guest_rejected_html(data: Dict[str, Any]) -> str:
    """Email pacientu - termin ZAVRNJEN."""
    service_type = data.get('location', 'Pregled')

    content = f"""
    <p>Pozdravljeni <strong>{data.get('name', 'pacient')}</strong>,</p>

    <p>Zahvaljujemo se za vaše zaupanje in zanimanje za termin pri nas.</p>

    <p>Žal smo prisiljeni obvestiti, da vašega termina za <strong>{service_type}</strong> dne <strong>{data.get('date', '')}</strong> ob <strong>{data.get('time', '')}</strong> ne moremo potrditi.</p>

    <div style="background:#fef2f2;border-left:4px solid #ef4444;padding:12px 16px;margin:20px 0;border-radius:4px;">
        <p style="margin:0;color:#991b1b;">
            Razlog: Termin je že zaseden ali zdravnik ni na voljo v izbranem času.
        </p>
    </div>

    <p>Priporočamo vam, da nas kontaktirate za iskanje alternativnega termina:</p>

    <p style="margin-left:16px;">
        📞 <strong>04 271 30 10</strong><br>
        ✉️ <strong>mr@medilab.si</strong>
    </p>

    <p style="margin-top:24px;">
        Lepo vas pozdravljamo,<br>
        <strong>Medilab d.o.o.</strong>
    </p>
    """
    return _email_wrapper(content)


def _guest_reminder_html(data: Dict[str, Any]) -> str:
    """Email pacientu - OPOMNIK za termin čez 24 ur."""
    service_type = data.get('location', 'Pregled')

    # Ikone po tipu pregleda
    icon_map = {
        'Ortopedski pregled': '🦴',
        'Dermatološki pregled': '🩺',
        'Okulistični pregled': '👁️',
        'Laserski poseg': '✨',
        'Estetski poseg': '💉',
        'Kozmetični salon': '💆',
        'Fizioterapija': '🏃',
    }
    icon = icon_map.get(service_type, '🏥')

    content = f"""
    <p>Pozdravljeni <strong>{data.get('name', 'pacient')}</strong>,</p>

    <div style="background:#fef3c7;border-left:4px solid #f59e0b;padding:14px 18px;margin:16px 0;border-radius:4px;">
        <p style="margin:0;font-weight:700;color:#92400e;font-size:16px;">
            ⏰ Opomnik: Vaš termin je <strong>jutri</strong>!
        </p>
    </div>

    {_kv_table({
        f'{icon} Vrsta pregleda': service_type,
        '📅 Datum': data.get('date', ''),
        '🕐 Ura': data.get('time', ''),
        '📍 Lokacija': 'Medilab d.o.o., Partizanska 12, Kranj',
    })}

    <div style="background:#f0f9ff;border-left:4px solid #0891b2;padding:12px 16px;margin:20px 0;border-radius:4px;">
        <p style="margin:0;font-weight:600;color:#0891b2;margin-bottom:8px;">ℹ️ Priprava na pregled:</p>
        <ul style="margin:0;padding-left:20px;color:#134e4a;">
            <li>Pridite <strong>10 minut pred terminom</strong></li>
            <li>Prinesite <strong>zdravstveno kartico</strong></li>
            <li>Če imate prejšnje izvide, jih prinesite s seboj</li>
        </ul>
    </div>

    <p style="margin-top:18px;">
        <strong>V primeru preprečitve:</strong><br>
        Prosimo, obvestite nas <strong>čim prej</strong>, da lahko termin dodelimo drugemu pacientu.<br>
        📞 04 271 30 10<br>
        ✉️ mr@medilab.si
    </p>

    <p style="margin-top:24px;">
        Veselimo se vašega obiska!<br>
        <strong>Medilab d.o.o.</strong>
    </p>
    """
    return _email_wrapper(content)


# ============================================================
# SEND FUNCTIONS
# ============================================================

def _send_email(to: str, subject: str, html_body: str, ical_attachment: Optional[str] = None) -> bool:
    """
    Pošlje email preko SMTP z opcijsko iCal priponko.
    Vrne True če uspešno, False če napaka.

    POMEMBNO: Ta funkcija trenutno NI AKTIVNA.
    Za aktivacijo nastavi SMTP credentials v .env
    """
    if SUBJECT_PREFIX:
        prefix = SUBJECT_PREFIX
        if not subject:
            subject = prefix
        elif not subject.startswith(prefix):
            subject = f"{prefix} {subject}"

    if RESEND_API_KEY:
        try:
            resend.api_key = RESEND_API_KEY
            payload = {
                "from": RESEND_FROM_EMAIL,
                "to": to,
                "subject": subject,
                "html": html_body,
            }
            # iCal priponka preko Resend ni podprta, samo SMTP
            if ical_attachment:
                print(f"[EMAIL] Warning: iCal attachment ne deluje z Resend")
            resend.Emails.send(payload)
            print(f"[EMAIL] Resend poslano: {subject} -> {to}")
            return True
        except Exception as e:
            print(f"[EMAIL] Resend napaka: {e}")
            return False

    if not SMTP_USER or not SMTP_PASSWORD:
        print(f"[EMAIL] SMTP ni konfiguriran. Email NI poslan: {subject}")
        return False

    try:
        msg = MIMEMultipart('mixed')
        msg['Subject'] = subject
        msg['From'] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
        msg['To'] = to
        msg['Reply-To'] = SMTP_FROM_EMAIL

        # Plain text verzija
        text_body = f"Sporočilo od zdravstvenega centra. Za ogled odprite v brskalniku."
        msg.attach(MIMEText(text_body, 'plain', 'utf-8'))

        # HTML verzija
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        # iCal priponka
        if ical_attachment:
            ical_part = MIMEBase('text', 'calendar', method='REQUEST', name='appointment.ics')
            ical_part.set_payload(ical_attachment.encode('utf-8'))
            ical_part.add_header('Content-Disposition', 'attachment', filename='appointment.ics')
            ical_part.add_header('Content-Class', 'urn:content-classes:calendarmessage')
            msg.attach(ical_part)

        # Pošlji
        # Port 465 = SSL, Port 587 = TLS (ali prisilno preko SMTP_SSL)
        if SMTP_PORT == 465 or SMTP_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)

        print(f"[EMAIL] Poslano: {subject} -> {to} (iCal: {bool(ical_attachment)})")
        return True

    except Exception as e:
        print(f"[EMAIL] Napaka pri pošiljanju: {e}")
        return False


# ============================================================
# PUBLIC API
# ============================================================

def send_guest_confirmation(data: Dict[str, Any]) -> bool:
    """
    Pošlje potrditveni email gostu.
    
    Args:
        data: Podatki rezervacije (name, email, date, people, ...)
    
    Returns:
        True če uspešno poslano
    """
    email = data.get('email')
    if not email:
        return False
    
    rid = data.get('id')
    tag = f"Naročilo #{rid}" if rid else "Naročilo"
    if data.get('reservation_type') == 'room':
        html = _guest_room_confirmation_html(data)
        subject = f"{tag} - Povpraševanje za sobo"
    else:
        html = _guest_table_confirmation_html(data)
        subject = f"{tag} - Naročilo na pregled"

    return _send_email(email, subject, html)


def send_admin_notification(data: Dict[str, Any], confirm_url: str = "", reject_url: str = "") -> bool:
    """
    Pošlje obvestilo adminu o novi rezervaciji.
    
    Args:
        data: Podatki rezervacije
        confirm_url: URL za potrditev (opcijsko)
        reject_url: URL za zavrnitev (opcijsko)
    
    Returns:
        True če uspešno poslano
    """
    res_type = "sobe" if data.get('reservation_type') == 'room' else "pregleda"
    rid = data.get('id')
    tag = f"Naročilo #{rid}" if rid else "Naročilo"
    subject = f"{tag} - Novo naročilo {res_type} – {data.get('name', 'Neznano')}"
    
    html = _admin_new_reservation_html(data, confirm_url, reject_url)
    return _send_email(ADMIN_EMAIL, subject, html)


def _generate_ical(data: Dict[str, Any]) -> str:
    """Generira iCal priponko za Google Calendar, Outlook, Apple Calendar."""
    service_type = data.get('location', 'Pregled')
    date_str = data.get('date', '')  # Format: DD.MM.LLLL
    time_str = data.get('time', '')  # Format: HH:MM

    # Pretvori datum in čas v datetime
    try:
        parts = date_str.split('.')
        if len(parts) == 3:
            day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
            hour, minute = map(int, time_str.split(':'))
            dt_start = datetime(year, month, day, hour, minute)
            dt_end = dt_start + timedelta(minutes=30)  # 30 min trajanje
        else:
            return ""
    except:
        return ""

    # Format za iCal
    dtstart = dt_start.strftime('%Y%m%dT%H%M%S')
    dtend = dt_end.strftime('%Y%m%dT%H%M%S')
    dtstamp = datetime.now().strftime('%Y%m%dT%H%M%SZ')
    uid = f"{data.get('id', uuid.uuid4())}@medilab.si"

    ical = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Medilab//SI
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{dtstamp}
DTSTART:{dtstart}
DTEND:{dtend}
SUMMARY:{service_type}
DESCRIPTION:Termin pri Medilab d.o.o.\\n\\nPacient: {data.get('name', '')}\\nVrsta: {service_type}
LOCATION:Medilab d.o.o., Partizanska 12, Kranj
STATUS:CONFIRMED
SEQUENCE:0
BEGIN:VALARM
TRIGGER:-PT1440M
ACTION:DISPLAY
DESCRIPTION:Reminder: {service_type} jutri ob {time_str}
END:VALARM
END:VEVENT
END:VCALENDAR"""
    return ical


def send_reservation_confirmed(data: Dict[str, Any]) -> bool:
    """Pošlje gostu obvestilo da je rezervacija POTRJENA + iCal priponko."""
    email = data.get('email')
    if not email:
        return False

    html = _guest_confirmed_html(data)
    rid = data.get('id')
    tag = f"Termin #{rid}" if rid else "Termin"
    subject = f"{tag} - Termin potrjen ✅"
    ical_content = _generate_ical(data)
    return _send_email(email, subject, html, ical_attachment=ical_content)


def send_reservation_rejected(data: Dict[str, Any]) -> bool:
    """Pošlje gostu obvestilo da je rezervacija ZAVRNJENA."""
    email = data.get('email')
    if not email:
        return False

    html = _guest_rejected_html(data)
    rid = data.get('id')
    tag = f"Rezervacija #{rid}" if rid else "Rezervacija"
    subject = f"{tag} - Rezervacija zavrnjena"
    return _send_email(email, subject, html)


def send_appointment_reminder(data: Dict[str, Any]) -> bool:
    """Pošlje gostu OPOMNIK za termin, ki je čez 24 ur."""
    email = data.get('email')
    if not email:
        return False

    html = _guest_reminder_html(data)
    service_type = data.get('location', 'Pregled')
    subject = f"⏰ Opomnik: {service_type} jutri ob {data.get('time', '')}"
    return _send_email(email, subject, html)


def send_custom_message(to_email: str, subject: str, body: str) -> bool:
    """Pošlje poljubno sporočilo gostu."""
    if not to_email:
        return False
    html = _email_wrapper(f"<div style='white-space:pre-wrap;line-height:1.7'>{body}</div>")
    return _send_email(to_email, subject, html)


# ============================================================
# TEST FUNCTION
# ============================================================

def test_email_templates():
    """
    Testna funkcija - generira HTML emaile brez pošiljanja.
    Uporabi za pregled dizajna.
    
    Uporaba:
        from app.services.email_service import test_email_templates
        test_email_templates()
    """
    test_data = {
        'id': 123,
        'name': 'Janez Novak',
        'email': 'janez@example.com',
        'phone': '041 123 456',
        'date': '15.07.2025',
        'nights': 3,
        'rooms': 1,
        'people': 4,
        'location': 'Soba ALJAŽ',
        'reservation_type': 'room',
        'source': 'chatbot',
        'note': 'Potrebujemo otroško posteljico',
    }
    
    print("=" * 60)
    print("TEST: Guest Room Confirmation")
    print("=" * 60)
    html = _guest_room_confirmation_html(test_data)
    
    # Shrani v datoteko za pregled
    with open('/tmp/test_email_guest_room.html', 'w') as f:
        f.write(html)
    print("Shranjeno v: /tmp/test_email_guest_room.html")
    
    print("\n" + "=" * 60)
    print("TEST: Admin Notification")
    print("=" * 60)
    html = _admin_new_reservation_html(test_data, "http://localhost/confirm", "http://localhost/reject")
    
    with open('/tmp/test_email_admin.html', 'w') as f:
        f.write(html)
    print("Shranjeno v: /tmp/test_email_admin.html")
    
    # Test za mizo
    test_data['reservation_type'] = 'table'
    test_data['time'] = '13:00'
    test_data['location'] = 'Ortopedski pregled'
    del test_data['nights']
    del test_data['rooms']
    
    print("\n" + "=" * 60)
    print("TEST: Guest Table Confirmation")
    print("=" * 60)
    html = _guest_table_confirmation_html(test_data)
    
    with open('/tmp/test_email_guest_table.html', 'w') as f:
        f.write(html)
    print("Shranjeno v: /tmp/test_email_guest_table.html")
    
    print("\n✅ Vse email template generirane. Odpri HTML datoteke v brskalniku za pregled.")


if __name__ == "__main__":
    test_email_templates()
