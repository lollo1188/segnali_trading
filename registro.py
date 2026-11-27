"""
REGISTRO DEI SEGNALI — misura dal vivo come vanno le strategie

Ogni segnale inviato su Telegram viene salvato in registro.json, nella stessa cartella
dello stato (su Railway il volume /data). Il bot poi controlla da solo, sulle candele
successive di Yahoo, come sarebbe finito, con le stesse regole del backtest:
 - stop o target toccati (se nella stessa candela si toccano entrambi conta lo stop);
 - altrimenti chiusura forzata: intraday all'ora di chiusura del messaggio
   (21:45 italiane per l'oro, 15:45 di New York per le strategie USA),
   swing (Kumo) dopo 14 giorni di calendario.
Il risultato è in R (multipli del rischio iniziale), al netto dello spread stimato.

Il resoconto settimanale riporta, per strategia: segnali, vinti/persi, PF in R,
R totale, slittamento medio al momento dell'invio e ritardo medio rispetto alla
chiusura della candela.

⚠️ Esito TEORICO: il bot non sa se hai aperto il CFD né a che prezzo.
⚠️ Non segue TP1 e break-even dell'ORB, né TP1 e trailing del London Sweep (come il backtest).
⚠️ Ritracciamento giornaliero (9, 10) escluso: ha già i suoi promemoria serali.
"""

import logging
import os
import threading
from datetime import datetime, time, timedelta
from typing import Callable, List, Optional

import pandas as pd
import pytz

import stato

logger = logging.getLogger(__name__)

ROME_TZ = pytz.timezone("Europe/Rome")
ET_TZ = pytz.timezone("America/New_York")

_lock = threading.RLock()

# Spread andata+ritorno in % del valore (come backtest.py: schermata/KID Revolut)
COSTI = {"vwap_gold": 0.0001, "london_sweep": 0.0001, "kumo_gold_4h": 0.0001,
         "orb_sp500": 0.0002, "kumo_nasdaq": 0.0002,
         "orb_ant": 0.0004, "orb_cat": 0.0004, "orb_avgo": 0.0004}
INTERVALLO = {"vwap_gold": "15m", "london_sweep": "15m", "orb_sp500": "15m",
              "orb_ant": "15m", "orb_cat": "15m", "orb_avgo": "15m",
              "kumo_gold_4h": "1h", "kumo_nasdaq": "1h"}
NOMI = {"vwap_gold": "VWAP Reversal Oro", "london_sweep": "London Sweep Oro",
        "orb_sp500": "ORB S&amp;P500", "orb_ant": "ORB ANT", "orb_cat": "ORB CAT",
        "orb_avgo": "ORB AVGO", "kumo_gold_4h": "Kumo Oro 4H", "kumo_nasdaq": "Kumo Nasdaq 1H"}
STRATEGIE_USA_INTRADAY = {"orb_sp500", "orb_ant", "orb_cat", "orb_avgo"}
GIORNI_SWING = 14


def percorso() -> str:
    return os.path.join(stato.cartella(), "registro.json")


_registro: Optional[List[dict]] = None


def _tutti() -> List[dict]:
    global _registro
    if _registro is None:
        _registro = list(stato.leggi_json(percorso(), []))
    return _registro


def _salva() -> None:
    stato.scrivi_json(percorso(), _tutti())


def _scadenza(name: str, fine: datetime) -> datetime:
    """Quando la posizione teorica viene chiusa se non tocca stop né target."""
    if name in STRATEGIE_USA_INTRADAY:
        d = fine.astimezone(ET_TZ).date()
        return ET_TZ.localize(datetime.combine(d, time(15, 45))).astimezone(ROME_TZ)
    if name in ("vwap_gold", "london_sweep"):
        d = fine.astimezone(ROME_TZ).date()
        return ROME_TZ.localize(datetime.combine(d, time(21, 45)))
    return fine + timedelta(days=GIORNI_SWING)


def registra(name: str, ticker: str, signal: dict, inviato: Optional[datetime] = None,
             ombra: bool = False) -> None:
    """Salva un segnale appena inviato (ombra=True: modalità solo registro, non inviato)."""
    try:
        fine = signal.get("candela_fine")
        if fine is None:
            return
        inviato = inviato or datetime.now(ROME_TZ)
        prezzo_invio = signal.get("prezzo_attuale")
        rec = {
            "strategia": name, "ticker": ticker, "direzione": signal["direzione"],
            "entry": float(signal["entry"]), "sl": float(signal["sl"]), "tp": float(signal["tp"]),
            "candela_fine": fine, "inviato": inviato,
            "prezzo_invio": float(prezzo_invio) if prezzo_invio not in (None, "") else None,
            "scadenza": _scadenza(name, fine),
            "esito": None, "uscita": None, "r": None, "chiuso": None,
        }
        if ombra:
            rec["ombra"] = True
        with _lock:
            _tutti().append(rec)
            _salva()
    except Exception as e:
        logger.error(f"[registro] {name}: segnale non registrato ({type(e).__name__}: {e})")


def aperti(ora: Optional[datetime] = None) -> List[dict]:
    """Posizioni teoriche ancora aperte: non ancora chiuse dalla valutazione e non scadute.
    (Una posizione che ha toccato lo stop negli ultimi 30 minuti può risultare ancora aperta
    finché la valutazione periodica non la chiude.)"""
    ora = ora or datetime.now(ROME_TZ)
    with _lock:
        return [dict(r) for r in _tutti() if r.get("esito") is None and r["scadenza"] > ora]


def _valuta_uno(rec: dict, df: pd.DataFrame, ora: datetime) -> bool:
    """Aggiorna rec se la posizione teorica si è chiusa. True se chiusa ora."""
    d = 1 if rec["direzione"] == "LONG" else -1
    entry, sl, tp = rec["entry"], rec["sl"], rec["tp"]
    rischio = abs(entry - sl)
    if rischio <= 0:
        rec.update(esito="NON VALIDO", chiuso=ora)
        return True
    barre = df[(df.index >= rec["candela_fine"]) & (df.index < rec["scadenza"])]
    # solo candele già chiuse
    passo = pd.Timedelta(INTERVALLO.get(rec["strategia"], "15m").replace("m", "min"))
    barre = barre[barre.index + passo <= ora]
    uscita, esito = None, None
    for ts, b in barre.iterrows():
        if d == 1:
            if b["Low"] <= sl: uscita, esito = sl, "STOP"; break
            if b["High"] >= tp: uscita, esito = tp, "TARGET"; break
        else:
            if b["High"] >= sl: uscita, esito = sl, "STOP"; break
            if b["Low"] <= tp: uscita, esito = tp, "TARGET"; break
    if esito is None:
        if ora < rec["scadenza"] + passo:
            return False                                 # ancora aperta
        if len(barre) == 0:
            rec.update(esito="NESSUN DATO", chiuso=ora)
            return True
        uscita, esito = float(barre["Close"].iloc[-1]), "CHIUSURA"
    costo_r = COSTI.get(rec["strategia"], 0.0002) * entry / rischio
    rec.update(esito=esito, uscita=float(uscita), chiuso=ora,
               r=round((uscita - entry) * d / rischio - costo_r, 3))
    return True


def valuta(scarica: Callable[[str, str, str], Optional[pd.DataFrame]],
           ora: Optional[datetime] = None) -> List[dict]:
    """Chiude le posizioni teoriche concluse. `scarica(ticker, period, interval)` deve
    restituire candele con indice in ora italiana (es. strategies.get_data)."""
    ora = ora or datetime.now(ROME_TZ)
    chiusi, cache = [], {}
    with _lock:
        aperti = [r for r in _tutti() if r.get("esito") is None and r["candela_fine"] < ora]
        for rec in aperti:
            iv = INTERVALLO.get(rec["strategia"], "15m")
            chiave = (rec["ticker"], iv)
            if chiave not in cache:
                cache[chiave] = scarica(rec["ticker"], "60d" if iv == "15m" else "3mo", iv)
            df = cache[chiave]
            if df is None:
                continue
            try:
                if _valuta_uno(rec, df, ora):
                    chiusi.append(rec)
            except Exception as e:
                logger.error(f"[registro] valutazione fallita: {type(e).__name__}: {e}")
        if chiusi:
            _salva()
    return chiusi


def _num(x: float, dec: int = 2) -> str:
    return f"{x:+.{dec}f}".replace(".", ",")


def _statistiche(recs: List[dict]) -> dict:
    chiusi = [r for r in recs if r.get("r") is not None]
    vinti = [r["r"] for r in chiusi if r["r"] > 0]
    persi = [r["r"] for r in chiusi if r["r"] <= 0]
    pf = (sum(vinti) / abs(sum(persi))) if persi and sum(persi) != 0 else (float("inf") if vinti else None)
    slip = []
    for r in recs:
        if r.get("prezzo_invio") is not None and abs(r["entry"] - r["sl"]) > 0:
            d = 1 if r["direzione"] == "LONG" else -1
            slip.append((r["prezzo_invio"] - r["entry"]) * d / abs(r["entry"] - r["sl"]) * 100)
    ritardi = [(r["inviato"] - r["candela_fine"]).total_seconds() / 60 for r in recs]
    ombra = bool(recs) and all(r.get("ombra") for r in recs)
    return {"n": len(recs), "chiusi": len(chiusi), "aperti": sum(1 for r in recs if r.get("esito") is None),
            "vinti": len(vinti), "persi": len(persi), "r_tot": sum(vinti) + sum(persi), "pf": pf,
            "slip": sum(slip) / len(slip) if slip else None,
            "ritardo": sum(ritardi) / len(ritardi) if ritardi else None, "ombra": ombra}


def _riga(nome: str, s: dict) -> str:
    if s["n"] == 0:
        return ""
    if s.get("ombra"):
        nome += " 👻 <i>solo registro</i>"
    pf = "—" if s["pf"] is None else ("∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}".replace(".", ","))
    riga = (f"<b>{nome}</b>: {s['n']} segnali | ✅ {s['vinti']} ❌ {s['persi']}"
            + (f" ⏳ {s['aperti']}" if s["aperti"] else "")
            + f" | {_num(s['r_tot'], 1)}R | PF {pf}")
    extra = []
    if s["slip"] is not None:
        extra.append(f"slittamento medio {s['slip']:.0f}% del rischio")
    if s["ritardo"] is not None:
        extra.append(f"inviato in media {s['ritardo']:.0f} min dopo la candela")
    return riga + ("\n   " + " · ".join(extra) if extra else "")


def riepilogo(giorni: int = 7, ora: Optional[datetime] = None) -> str:
    """Testo HTML del resoconto: ultimi `giorni` giorni e totale da inizio registro."""
    ora = ora or datetime.now(ROME_TZ)
    with _lock:
        tutti = list(_tutti())
    dal = ora - timedelta(days=giorni)
    settimana = [r for r in tutti if r["inviato"] >= dal]
    righe = [f"📒 <b>Resoconto segnali — ultimi {giorni} giorni</b>", "━━━━━━━━━━━━━━━━━━━━"]
    corpo = [_riga(NOMI.get(k, k), _statistiche([r for r in settimana if r["strategia"] == k]))
             for k in NOMI]
    corpo = [c for c in corpo if c]
    righe += corpo or ["Nessun segnale questa settimana."]
    if tutti:
        inizio = min(r["inviato"] for r in tutti)
        righe += ["", f"📊 <b>Totale dal {inizio:%d/%m/%Y}</b>"]
        tot = [_riga(NOMI.get(k, k), _statistiche([r for r in tutti if r["strategia"] == k]))
               for k in NOMI]
        righe += [c for c in tot if c]
    righe += ["━━━━━━━━━━━━━━━━━━━━",
              "ℹ️ Esiti TEORICI con le regole del backtest (stop, target o chiusura forzata), "
              "in R al netto dello spread. Con meno di ~30 segnali per strategia non trarre conclusioni."]
    return "\n".join(righe)
