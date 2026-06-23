"""
Demo seeder — vstavi fake podatke v bazo za demo prikaz admin panela.
Požene se samo če je baza prazna (0 rezervacij).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Dodaj projekt root v sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.reservation_service import ReservationService


def _ts(days_offset: int = 0, hour: int = 9, minute: int = 0) -> str:
    dt = datetime.now() + timedelta(days=days_offset)
    return dt.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()


def _date(days_offset: int = 0) -> str:
    dt = datetime.now() + timedelta(days=days_offset)
    return dt.strftime("%d.%m.%Y")


DEMO_MARKER = "chat"  # source vrednost ki jo seeder vstavi


def seed_demo(svc: ReservationService) -> None:
    conn = svc._conn()
    ph = svc._placeholder()
    try:
        cur = conn.cursor()
        # Preveri ali so podatki že iz tega seeda (preverimo datum prvega vnosa)
        cur.execute("SELECT COUNT(1) FROM reservations WHERE source = 'chat'")
        row = cur.fetchone()
        count = row[0] if isinstance(row, (tuple, list)) else list(row.values())[0]
        if count > 0:
            # Preveri ali je datum zadnjega vnosa danes (isti deploy)
            cur.execute("SELECT created_at FROM reservations ORDER BY id DESC LIMIT 1")
            last = cur.fetchone()
            if last:
                last_ts = last[0] if isinstance(last, (tuple, list)) else list(last.values())[0]
                last_date = last_ts[:10] if last_ts else ""
                today = datetime.now().strftime("%Y-%m-%d")
                if last_date == today:
                    print(f"[seed] Demo podatki so že vstavljeni za danes — preskačem.")
                    return
            # Izbriši stare demo podatke in vstavi svežih
            print("[seed] Brišem stare demo podatke in vstavljam sveže...")
            cur.execute("DELETE FROM reservations")
            cur.execute("DELETE FROM conversations")
            cur.execute("DELETE FROM inquiries")
            conn.commit()
    finally:
        cur.close()
        conn.close()

    print("[seed] Vstavljam demo podatke...")

    # ── REZERVACIJE (naročila) ────────────────────────────────────────────
    reservations = [
        # Potrjene/zaključene iz preteklosti
        dict(
            date=_date(-14), time="08:30", people=1,
            reservation_type="preiskava", service_type="MR glave in vratu",
            name="Katarina Novak", phone="041 123 456", email="katarina.novak@gmail.com",
            note="Glavoboli in vrtoglavice zadnje 3 tedne. Napotnica osebnega zdravnika.",
            status="confirmed",
            confirmed_at=_ts(-14, 8, 0), confirmed_by="Maja Kokalj",
            admin_notes="Preiskava opravljena. Izvid poslan po emailu.",
            source="chat",
        ),
        dict(
            date=_date(-10), time="10:00", people=1,
            reservation_type="preiskava", service_type="RTG hrbtenice",
            name="Marko Petrovič", phone="031 654 321", email="",
            note="Bolečine v ledvenem delu. Samoplačniško.",
            status="confirmed",
            confirmed_at=_ts(-10, 9, 0), confirmed_by="Andrej Vrečko",
            admin_notes="RTG opravljeno. Izvid izdan na mestu.",
            source="chat",
        ),
        dict(
            date=_date(-7), time="14:00", people=1,
            reservation_type="preiskava", service_type="UZ trebuha",
            name="Ana Kovač", phone="051 789 012", email="ana.kovac@outlook.com",
            note="Bolečine v trebuhu, napihnjenost. Samoplačniško — UZ pregled trebuha.",
            status="confirmed",
            confirmed_at=_ts(-7, 13, 0), confirmed_by="Maja Kokalj",
            source="chat",
        ),
        dict(
            date=_date(-5), time="09:00", people=1,
            reservation_type="preiskava", service_type="MR kolena",
            name="Tomaž Horvat", phone="040 321 987", email="tomaz.h@gmail.com",
            note="Poškodba meniskusa med tekom. Napotnica ortopeda.",
            status="confirmed",
            confirmed_at=_ts(-5, 8, 30), confirmed_by="Andrej Vrečko",
            admin_notes="MR kolena opravljeno. Vidni znaki poškodbe meniskusa — izvid poslan specialistu.",
            source="chat",
        ),
        dict(
            date=_date(-3), time="11:30", people=1,
            reservation_type="preiskava", service_type="Ambulanta za bolezni ščitnice",
            name="Maja Zorko", phone="070 456 123", email="maja.zorko@gmail.com",
            note="Utrujenost, pridobivanje telesne teže, slab spanec. Sum na hipotirozem.",
            status="confirmed",
            confirmed_at=_ts(-3, 10, 0), confirmed_by="Klavdija Oblak",
            source="chat",
        ),

        # Čakajoče — danes in prihodnji teden
        dict(
            date=_date(0), time="13:00", people=1,
            reservation_type="preiskava", service_type="MR hrbtenice (ledveni del)",
            name="Peter Kranjc", phone="041 555 777", email="peter.kranjc@siol.net",
            note="Kronične bolečine v križu. Napotnica nevrologa.",
            status="pending",
            source="chat",
        ),
        dict(
            date=_date(1), time="08:00", people=1,
            reservation_type="preiskava", service_type="RTG ramenskega sklepa",
            name="Sonja Bergant", phone="031 888 444", email="sonja.bergant@gmail.com",
            note="Bolečina v ramenu po padcu. Samoplačniško.",
            status="pending",
            source="chat",
        ),
        dict(
            date=_date(2), time="15:30", people=1,
            reservation_type="preiskava", service_type="UZ ščitnice",
            name="Luka Štefan", phone="064 222 333", email="luka.stefan@gmail.com",
            note="Kontrolni pregled ščitnice po terapiji. Napotnica endokrinologa.",
            status="pending",
            source="chat",
        ),
        dict(
            date=_date(3), time="10:30", people=1,
            reservation_type="preiskava", service_type="MR dojk",
            name="Irena Mlakar", phone="051 000 111", email="irena.mlakar@t-2.net",
            note="Preventivni pregled pri povišanem genetskem tveganju. Napotnica ginekologa.",
            status="pending",
            source="chat",
        ),
        dict(
            date=_date(5), time="09:30", people=1,
            reservation_type="preiskava", service_type="UZ vodeni poseg — izpiranje kalcifikacij",
            name="Drago Lipovec", phone="040 777 888", email="",
            note="Kalcifikirajuča tendinoza ramenskega sklepa. Samoplačniško.",
            status="pending",
            source="chat",
        ),
        dict(
            date=_date(7), time="11:00", people=1,
            reservation_type="preiskava", service_type="MR prostate",
            name="Boris Hočevar", phone="031 444 666", email="boris.hocevar@gmail.com",
            note="Povišan PSA. Napotnica urologa. Z napotnico ZZZS.",
            status="pending",
            source="chat",
        ),
        dict(
            date=_date(10), time="14:30", people=1,
            reservation_type="preiskava", service_type="MR glave in vratu",
            name="Vesna Oblak", phone="070 123 999", email="vesna.oblak@outlook.com",
            note="Migrene z avro. Napotnica nevrologa.",
            status="pending",
            source="chat",
        ),
    ]

    for r in reservations:
        svc.create_reservation(**r)

    # ── POGOVORI (conversations) ──────────────────────────────────────────
    conn = svc._conn()
    ph = svc._placeholder()
    try:
        cur = conn.cursor()
        conversations = [
            ("sess-001", "Pozdravljeni, rad bi se naročil na MR glave.", "Pozdravljeni! Z veseljem vam pomagam z naročanjem na MR glave. Imate napotnico osebnega zdravnika ali bi šlo za samoplačniško preiskavo?", "narocanje_mr", False, _ts(-14, 9, 0)),
            ("sess-001", "Imam napotnico od osebnega zdravnika.", "Odlično! Za naročanje z napotnico pokličite 04 271 30 10 ali pa izpolnite obrazec spodaj — kontaktirali vas bomo za potrditev termina.", "narocanje_mr", False, _ts(-14, 9, 2)),
            ("sess-002", "Ali je potrebna posebna priprava pred MR hrbtenice?", "Za MR hrbtenice posebna priprava načeloma ni potrebna. Veljajo le splošna navodila za MR: pred preiskavo odstranite vse kovinske predmete (nakit, ure, pasove, telefon). Če boste prejeli kontrastno sredstvo, morate biti tešči 6 ur.", "info_mr", False, _ts(-10, 11, 0)),
            ("sess-002", "Koliko časa traja preiskava?", "MR hrbtenice traja 15–30 minut. Če je potrebna aplikacija kontrastnega sredstva, se čas podaljša za dodatnih 15 minut. Skupaj pridite 10 minut prej za registracijo.", "info_mr", False, _ts(-10, 11, 2)),
            ("sess-003", "Koliko stane UZ trebuha?", "UZ preiskave pri nas izvajamo izključno samoplačniško. Za točno ceno UZ trebuha pokličite 04 271 30 10 — cene se razlikujejo glede na obseg preiskave.", "cene", False, _ts(-7, 10, 0)),
            ("sess-004", "Imam kovinski vsadek v kolku — ali lahko naredim MR?", "Odlično vprašanje! Za MR z metalnim vsadkom morate pridobiti certifikat ali mnenje lečečega zdravnika o združljivosti vsadka z MR preiskavo. Brez tega potrdila MR preiskave varnostno ne moremo izvesti. Pokličite nas na 04 271 30 10 in skupaj preverimo.", "info_mr_varnost", False, _ts(-5, 14, 0)),
            ("sess-005", "Kje se nahajate?", "Najdete nas na naslovu **Partizanska cesta 12, 4000 Kranj**. Brezplačno parkirišče je neposredno pred vhodom. Dostopni smo z avtobusom (linija 1 in 3, postaja Partizanska).", "lokacija", False, _ts(-3, 9, 0)),
            ("sess-006", "Kakšen je delovni čas?", "Delamo **vsak dan od 07:00 do 19:00**. Samoplačniške storitve so na voljo tudi v razširjenem urniku: 07:00–08:00 in 18:00–19:00 ter ob sobotah 07:00–09:00.", "delovni_cas", False, _ts(-1, 16, 0)),
            ("sess-007", "Imate čakalne dobe za MR?", "Čakalne dobe za zavarovance z napotnico so odvisne od vrste napotnice in so skladne s pravilniki NIJZ. Za aktualne čakalne dobe pokličite 04 271 30 10. Samoplačniško naročanje je praviloma možno v krajšem roku.", "cakalne_dobe", False, _ts(-1, 17, 0)),
            ("sess-008", "Rad bi se naročil na RTG kolena.", "Seveda! RTG kolena je hitra preiskava, ki traja 5–10 minut. Naročite se po telefonu **04 271 30 10** ali izpolnite obrazec spodaj. Ali imate napotnico ali bi šlo samoplačniško?", "narocanje_rtg", False, _ts(0, 10, 0)),
        ]
        for sess, umsg, bresp, intent, followup, ts in conversations:
            cur.execute(
                f"INSERT INTO conversations (session_id, user_message, bot_response, intent, needs_followup, created_at) VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
                (sess, umsg, bresp, intent, followup, ts),
            )

        # ── POVPRAŠEVANJA (inquiries) ──────────────────────────────────────
        inquiries = [
            ("Zanima me cena MR celotne hrbtenice (vratna + prsna + ledvena). Imam kronične bolečine. Ali so možni termini zjutraj pred 9:00?", None, "Rok Jerman", "rok.jerman@gmail.com", "041 321 654", "Rok Jerman, 041 321 654", "new", _ts(-2, 9, 0)),
            ("Bi rada preverila ali je naročilo na UZ dojk možno brez napotnice in kakšna je cena. Starost 45 let.", None, "Nina Potočnik", "nina.potocnik@siol.net", "051 876 543", "Nina Potočnik, 051 876 543", "new", _ts(-1, 14, 0)),
            ("Ali izvajate MR artrografijo ramenskega sklepa? Napotil me je ortoped. Kdaj bi bil najhitrejši termin?", None, "Gregor Šušteršič", "", "064 111 222", "Gregor Šušteršič, 064 111 222", "new", _ts(0, 8, 0)),
        ]
        for det, deadline, cname, cemail, cphone, craw, status, ts in inquiries:
            cur.execute(
                f"INSERT INTO inquiries (details, deadline, contact_name, contact_email, contact_phone, contact_raw, status, created_at, source) VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (det, deadline, cname, cemail, cphone, craw, status, ts, "chat"),
            )

        conn.commit()
    finally:
        cur.close()
        conn.close()

    print(f"[seed] Vstavljenih {len(reservations)} rezervacij, {len(conversations)} pogovorov, 3 povpraševanja.")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    svc = ReservationService()
    seed_demo(svc)
