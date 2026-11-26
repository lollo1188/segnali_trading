"""
FILTRO ANTI-NEWS — versione 2

Blocca i controlli dei segnali nelle finestre ad alto rischio. Non usa API esterne:
si basa su un calendario che aggiorni a mano (sezione "CALENDARIO" qui sotto).

Cosa è cambiato rispetto alla versione 1:
 - Le finestre sono definite in ORA DI NEW YORK e convertite in ora italiana per
   ogni data: restano corrette anche nelle settimane con ora legale USA/Europa sfasata.
 - Le regole valgono per strategia: le strategie swing (Kumo) non sono toccate dalla
   finestra giornaliera, solo dagli eventi ad alto impatto.
 - Eventi (NFP, CPI, FOMC), festività, mezze sedute e utili sono date esplicite,
   non finestre fisse ogni mercoledì/venerdì/giorno.
 - Rimossa la regola "ogni mercoledì" (petrolio, strategia non più attiva), "ogni
   venerdì" e "lunedì fino alle 10:00" (bloccava London Sweep ogni lunedì).
 - prossimo_sblocco(): dice quando finisce il blocco, così il bot può RIPETERE il
   controllo saltato invece di perdere la candela.

⚠️ Il calendario va aggiornato a mano. Controlla le date su fonti ufficiali:
   BLS (bls.gov/schedule), Federal Reserve (federalreserve.gov), NYSE (nyse.com)
   e i comunicati delle società per gli utili.
"""

from datetime import datetime, date, time, timedelta
from typing import Optional, List, Tuple
import pytz

ROME_TZ = pytz.timezone("Europe/Rome")
ET_TZ = pytz.timezone("America/New_York")

# ── Gruppi di strategie (nomi usati in main.py) ───────────────────────────────
STRATEGIE_SWING = {"kumo_gold_4h", "kumo_nasdaq"}
STRATEGIE_USA = {"orb_sp500", "orb_ant", "orb_cat", "orb_avgo", "kumo_nasdaq"}
STRATEGIE_ORO = {"vwap_gold", "london_sweep", "kumo_gold_4h"}

# ── Regola giornaliera ────────────────────────────────────────────────────────
# Molti dati USA escono alle 08:30 ET (14:30 italiane con orari allineati):
# jobless claims, PPI, vendite al dettaglio, PIL... Finestra stretta ogni giorno
# feriale, solo per le strategie intraday.
FINESTRA_DATI_ET = (time(8, 25), time(8, 50))

# ═════════════════════════════════════════════════════════════════════════════
# CALENDARIO — da aggiornare a mano
# ═════════════════════════════════════════════════════════════════════════════

# Eventi ad alto impatto: (data ET, inizio ET, fine ET, descrizione)
# Valgono per TUTTE le strategie (anche le swing).
# 08:30 ET: NFP e CPI. 14:00 ET: decisione FOMC (conferenza stampa 14:30 ET).
EVENTI_ALTO_IMPATTO = [
    (date(2026, 12, 4),  time(8, 20),  time(9, 45),  "NFP novembre (08:30 ET)"),
    (date(2026, 12, 9),  time(13, 50), time(15, 15), "FOMC dicembre (decisione 14:00 ET)"),
    (date(2026, 12, 10), time(8, 20),  time(9, 45),  "CPI novembre (08:30 ET)"),
    (date(2027, 1, 13),  time(8, 20),  time(9, 45),  "CPI dicembre (08:30 ET)"),
    (date(2027, 2, 11),  time(8, 20),  time(9, 45),  "CPI gennaio (08:30 ET)"),
    (date(2027, 3, 10),  time(8, 20),  time(9, 45),  "CPI febbraio (08:30 ET)"),
    # TODO: aggiungi NFP 2027, altri FOMC 2027, PCE, ecc. dal calendario BLS/Fed.
]

# Giorni in cui Wall Street è chiusa (fonte: NYSE). Blocca le strategie USA.
GIORNI_USA_CHIUSI = {
    date(2026, 11, 26),   # Thanksgiving
    date(2026, 12, 25),   # Natale
    date(2027, 1, 1),     # Capodanno
    # TODO: aggiungi le chiusure 2027 (MLK, Presidents' Day, Good Friday 26/3/2027, ...)
}

# Mezze sedute: giorno -> ora di chiusura ET. Dopo quell'ora il bot non cerca segnali.
MEZZE_SEDUTE_USA = {
    date(2026, 11, 27): time(13, 0),   # giorno dopo Thanksgiving
    date(2026, 12, 24): time(13, 0),   # vigilia di Natale
}

# Giorni in cui il mercato dell'oro è sostanzialmente chiuso. APPROSSIMAZIONE: verifica
# gli orari del tuo CFD su Revolut.
GIORNI_ORO_CHIUSI = {
    date(2026, 12, 25),
    date(2027, 1, 1),
}

# Utili trimestrali: strategia -> giorni da saltare (giorno della REAZIONE del titolo).
# Per risultati dopo la chiusura USA, inserisci il giorno SUCCESSIVO.
UTILI = {
    # Broadcom: risultati mercoledì 9/12/2026 dopo la chiusura -> reazione giovedì 10/12
    "orb_avgo": [date(2026, 12, 10)],
    # TODO: aggiungi la data quando Caterpillar e Anthropic annunciano i risultati.
    "orb_cat": [],
    "orb_ant": [],
    # Ritracciamento giornaliero: si guarda il giorno in cui si ENTRA (apertura del giorno dopo il segnale).
    "pullback_avgo": [date(2026, 12, 10)],
    "pullback_sp500": [],
}

# ═════════════════════════════════════════════════════════════════════════════
# LOGICA
# ═════════════════════════════════════════════════════════════════════════════

Finestra = Tuple[datetime, datetime, str]


def _et(d: date, t: time) -> datetime:
    """Data + ora di New York -> datetime con fuso italiano."""
    return ET_TZ.localize(datetime.combine(d, t)).astimezone(ROME_TZ)


def _finestre_del_giorno(strategia: Optional[str], d: date) -> List[Finestra]:
    """Finestre di blocco relative al giorno d (data di New York)."""
    f: List[Finestra] = []

    # 1) Finestra dati 08:30 ET — solo giorni feriali e solo strategie intraday
    if d.weekday() < 5 and strategia not in STRATEGIE_SWING:
        f.append((_et(d, FINESTRA_DATI_ET[0]), _et(d, FINESTRA_DATI_ET[1]), "dati USA 08:30 ET"))

    # 2) Eventi ad alto impatto — tutte le strategie
    for (dd, t1, t2, desc) in EVENTI_ALTO_IMPATTO:
        if dd == d:
            f.append((_et(d, t1), _et(d, t2), desc))

    # 3) Festività e mezze sedute USA
    if strategia in STRATEGIE_USA:
        if d in GIORNI_USA_CHIUSI:
            f.append((_et(d, time(0, 0)), _et(d, time(23, 59)), "Wall Street chiusa"))
        if d in MEZZE_SEDUTE_USA:
            f.append((_et(d, MEZZE_SEDUTE_USA[d]), _et(d, time(23, 59)), "mezza seduta: mercato USA chiuso"))

    # 4) Oro: giorni di mercato chiuso
    if strategia in STRATEGIE_ORO and d in GIORNI_ORO_CHIUSI:
        f.append((_et(d, time(0, 0)), _et(d, time(23, 59)), "mercato oro chiuso"))

    # 5) Utili del singolo titolo
    if strategia in UTILI and d in UTILI[strategia]:
        f.append((_et(d, time(0, 0)), _et(d, time(23, 59)), "giorno dei risultati trimestrali"))

    return f


def _blocchi_attivi(strategia: Optional[str], ora: Optional[datetime] = None) -> List[Finestra]:
    now = ora or datetime.now(ROME_TZ)
    attivi: List[Finestra] = []
    # La data di New York può differire da quella italiana: controlla i 3 giorni attorno
    for delta in (-1, 0, 1):
        d = now.date() + timedelta(days=delta)
        for (inizio, fine, motivo) in _finestre_del_giorno(strategia, d):
            if inizio <= now < fine:
                attivi.append((inizio, fine, motivo))
    return attivi


def is_news_window(strategia: Optional[str] = None, ora: Optional[datetime] = None) -> bool:
    """True se in questo momento la strategia deve saltare il controllo.
    Senza argomento si comporta come strategia intraday generica (solo finestre
    giornaliere ed eventi ad alto impatto)."""
    return len(_blocchi_attivi(strategia, ora)) > 0


def motivo_blocco(strategia: Optional[str] = None, ora: Optional[datetime] = None) -> Optional[str]:
    """Descrizione del blocco attivo (per i log), o None."""
    attivi = _blocchi_attivi(strategia, ora)
    return ", ".join(sorted({m for (_, _, m) in attivi})) if attivi else None


def prossimo_sblocco(strategia: Optional[str] = None, ora: Optional[datetime] = None) -> Optional[datetime]:
    """Quando finisce il blocco attivo (None se non c'è blocco).
    Se blocchi consecutivi si sovrappongono, restituisce la fine dell'ultimo."""
    now = ora or datetime.now(ROME_TZ)
    attivi = _blocchi_attivi(strategia, now)
    if not attivi:
        return None
    fine = max(f for (_, f, _) in attivi)
    for _ in range(5):                       # estendi se subito dopo ne inizia un altro
        successivi = _blocchi_attivi(strategia, fine)
        if not successivi:
            break
        nuova = max(f for (_, f, _) in successivi)
        if nuova <= fine:
            break
        fine = nuova
    return fine


def is_trading_day() -> bool:
    """True se oggi è un giorno lavorativo (Lun–Ven). Non usata dal bot."""
    return datetime.now(ROME_TZ).weekday() < 5


# ── Funzioni per le strategie giornaliere (ritracciamento) ────────────────────
def prossimo_giorno_usa(d: date) -> date:
    """Primo giorno di borsa USA dopo d (salta weekend e chiusure in GIORNI_USA_CHIUSI)."""
    x = d + timedelta(days=1)
    while x.weekday() >= 5 or x in GIORNI_USA_CHIUSI:
        x += timedelta(days=1)
    return x


def utili_il(strategia: str, d: date) -> bool:
    """True se d è un giorno di risultati trimestrali per quella strategia."""
    return d in UTILI.get(strategia, [])


def eventi_del_giorno(d: date) -> List[str]:
    """Eventi ad alto impatto (NFP, CPI, FOMC...) in calendario per il giorno d (data USA)."""
    return [desc for (dd, _, _, desc) in EVENTI_ALTO_IMPATTO if dd == d]
