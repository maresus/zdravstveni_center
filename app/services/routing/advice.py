from __future__ import annotations

from typing import Optional

from app.core.llm_client import get_llm_client


def generate_health_advice(symptom_description: str) -> str:
    """
    Generate personalized health advice using LLM.
    Gives general wellness tips (NOT diagnosis) and suggests appropriate specialist.
    """
    try:
        llm_client = get_llm_client()

        system_prompt = """Si prijazen pomočnik diagnostičnega centra Medilab d.o.o. v Kranju. Daj SPLOŠNE nasvete (počitek, obkladki, razgibavanje) — NIKOLI diagnoz, zdravil ali doziranja.

Format: kratek empatičen uvod + 2 konkretni alineji nasveta + disclaimer + priporočilo specialista.
Največ 100 besed.
OBVEZNO vključi: "⚠️ To je splošna usmeritev, ne zdravniški nasvet ali diagnoza. Za natančno oceno obiščite zdravnika."
Zaključi z: "Želite, da vas naročim na pregled?"
Slovenščina, vikaš, jedrnato."""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": symptom_description},
        ]

        response = llm_client.chat.completions.create(
            model="gpt-5-mini",
            messages=messages,
            temperature=0.3,
            max_tokens=170,
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        print(f"[HEALTH_ADVICE] Error: {e}")
        return """Razumem, da imate zdravstvene težave in da je to neprijetno.

Splošni nasveti medtem:
- Počitek in razbremenitev prizadetega dela
- Zadostna hidracija (vsaj 1,5–2 l vode dnevno)
- Nežno razgibavanje, če bolečina dopušča

⚠️ *To je splošna usmeritev, ne zdravniški nasvet ali diagnoza. Za natančno oceno se posvetujte z zdravnikom.*

Če težave trajajo več dni ali se stopnjujejo, priporočam pregled pri specialistu.

Želite, da vas naročim na pregled?"""


def _service_booking_label(service_key: Optional[str]) -> Optional[str]:
    if not service_key:
        return None
    labels = {
        "ORTOPED": "ortopedski pregled",
        "DERMATOLOG": "dermatološki pregled",
        "OKULIST": "okulistični pregled",
        "LASERSKI_POSEG": "laserski poseg",
        "ESTETSKI_POSEG": "estetski poseg",
        "FIZIOTERAPIJA": "fizioterapija",
        "KOZMETIKA": "kozmetični pregled",
    }
    return labels.get(service_key)


def answer_health_query(message: str, preferred_service: Optional[str] = None) -> str:
    """
    Health advice strategy:
    - Always provide safe LLM advice + offer booking.
    """
    try:
        advice = generate_health_advice(message)
        label = _service_booking_label(preferred_service)
        if label:
            return f"Glede na opis bi bil najbolj smiseln **{label}**.\n\n{advice}"
        return advice
    except Exception as e:
        print(f"[HEALTH_ADVICE] Fallback error: {e}")
        return generate_health_advice(message)


def _advice_only_headache() -> str:
    return (
        "Razumem, da je glavobol neprijeten. Poskusite z mirnim okoljem, dovolj tekočine in kratkimi odmori "
        "od zaslonov. Pogosto pomaga tudi reden spanec in lahek obrok.\n\n"
        "Če se glavobol stopnjuje, traja več dni ali se pojavi z dodatnimi znaki (npr. močna omotica, "
        "motnje vida, visoka vročina), priporočam posvet z osebnim zdravnikom ali nujno obravnavo.\n\n"
        "⚠️ *To je splošna usmeritev, ne zdravniški nasvet ali diagnoza. Za natančno oceno se posvetujte z zdravnikom.*\n\n"
        "Če želite, lahko pri nas preverim najhitrejši prosti termin za začetni pregled."
    )


def _advice_only(service: Optional[str]) -> str:
    if service == "ORTOPED":
        return (
            "Pri bolecinah v sklepih ali misicah obicajno pomaga kratek pocitek in razbremenitev. "
            "V prvih 24-48 urah lahko uporabite hladen obkladek (10-15 min veckrat na dan), nato pa "
            "nežno razgibavanje v mejah brez bolecine.\n\n"
            "Izogibajte se tezjim obremenitvam, dokler se stanje ne umiri. Ce bolecina traja vec dni, "
            "se ponavlja ali ovira vsakdanje aktivnosti, je smiseln pregled pri ortopedu."
        )
    if service == "DERMATOLOG":
        return (
            "Pri kožnih težavah je koristno ohraniti kozo cisto in suho ter se izogibati praskanju. "
            "Uporabljajte blage izdelke brez dišav in dražilnih snovi.\n\n"
            "Ce se spremembe sirijo, srbijo, krvavijo ali trajajo vec dni, je priporocljiv dermatoloski "
            "pregled za natančno oceno."
        )
    if service == "OKULIST":
        return (
            "Pri tezavah z vidom pomagajo odmori od zaslonov, dobra osvetlitev pri branju in umetne solze "
            "pri suhosti.\n\n"
            "Ce opazite nenadne spremembe vida, bolecino v oceh ali motnje vida, je smiselno opraviti "
            "okulisticni pregled."
        )
    if service == "ESTETSKI_POSEG":
        return (
            "Pred estetskim posegom je dobro, da izogibate alkohol in intenzivno vadbo 24 ur pred in po "
            "posegu ter se posvetujete o morebitnih zdravilih, ki redčijo kri.\n\n"
            "Za varno izvedbo je pomembna individualna ocena in jasna razlaga pričakovanj ter omejitev."
        )
    if service == "LASERSKI_POSEG":
        return (
            "Pred laserskimi posegi se priporoca izogibati soncenju, po posegu pa je pomembna zascita "
            "koze in upostevanje navodil izvajalca.\n\n"
            "Ce imate specificne tezave (npr. žilice ali bradavice), je smiselna predhodna ocena, da "
            "izberemo primeren poseg."
        )
    if service == "KOZMETIKA":
        return (
            "Za nego koze pomaga redno ciscenje z blagimi izdelki, hidracija in uporaba SPF, ce je koza "
            "izpostavljena soncu.\n\n"
            "Ce so prisotne trdovratne tezave (akne, razdrazena koza), je smiselno izbrati tretma, ki "
            "je prilagojen vasemu tipu koze."
        )
    return (
        "Razumem, da imate tezave. Poskusite s počitkom, dovolj tekocine in izogibanjem obremenitvam, "
        "ki simptome poslabšajo.\n\n"
        "Ce se stanje ne izboljsa v nekaj dneh ali se poslabsa, je priporočljiv posvet pri zdravniku."
    )


DISCLAIMER = "\n\n⚠️ *To je splošna usmeritev, ne zdravniški nasvet ali diagnoza. Za natančno oceno vašega stanja se posvetujte z zdravnikom.*"

def advice_only(service: Optional[str]) -> str:
    base = _advice_only(service)
    cta_by_service = {
        "MR": "Če želite, vas lahko zdaj naročim na MR preiskavo pri dr. Kokalj ali dr. Vrečku.",
        "RTG": "Če želite, vas lahko zdaj naročim na RTG slikanje.",
        "UZ": "Če želite, vas lahko zdaj naročim na ultrazvočno preiskavo.",
        "UZ_POSEG": "Če želite, lahko preverim prost termin za UZ vodeni poseg.",
        "SCITNICA": "Če želite, vas lahko zdaj naročim na pregled ščitnice pri dr. Oblak.",
    }
    normalized = service.upper() if service else None
    cta = cta_by_service.get(normalized, "Če želite, lahko zdaj preverim najhitrejši prosti termin.")
    return f"{base}{DISCLAIMER}\n\n{cta}"


def advice_only_headache() -> str:
    return _advice_only_headache()
