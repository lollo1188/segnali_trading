"""
STATO PERSISTENTE — memoria del bot che sopravvive ai riavvii

Salva in un file JSON:
 - ultimi segnali inviati (per la pausa anti-doppioni tra segnali uguali)
 - posizioni TEORICHE del ritracciamento giornaliero (per i promemoria MANTIENI/ESCI/STOP)
 - data dell'ultima candela giornaliera già elaborata
 - quando sono stati inviati gli avvisi periodici (es. calendario notizie in scadenza)

Dove salva:
 - variabile STATO_PATH, se impostata;
 - altrimenti /data/stato.json se esiste la cartella /data (volume Railway);
 - altrimenti ./stato.json (ATTENZIONE: su Railway senza volume il file si perde
   a ogni nuovo deploy; sopravvive solo ai riavvii dello stesso deploy).

Su Railway: crea un Volume sul servizio con mount path /data.
"""

import json
import logging
import os
import threading
from datetime import date, datetime
from typing import Any, Dict

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def percorso() -> str:
    if os.environ.get("STATO_PATH"):
        return os.environ["STATO_PATH"]
    if os.path.isdir("/data"):
        return "/data/stato.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "stato.json")


def su_volume() -> bool:
    """True se il file è su un percorso esplicito o sul volume /data."""
    return bool(os.environ.get("STATO_PATH")) or percorso().startswith("/data/")


# ── Conversione date <-> testo ────────────────────────────────────────────────
def _a_json(o: Any) -> Any:
    if isinstance(o, datetime):
        return {"__dt__": o.isoformat()}
    if isinstance(o, date):
        return {"__d__": o.isoformat()}
    if isinstance(o, dict):
        return {k: _a_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_a_json(v) for v in o]
    return o


def _da_json(o: Any) -> Any:
    if isinstance(o, dict):
        if set(o) == {"__dt__"}:
            return datetime.fromisoformat(o["__dt__"])
        if set(o) == {"__d__"}:
            return date.fromisoformat(o["__d__"])
        return {k: _da_json(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_da_json(v) for v in o]
    return o


VUOTO = {"last_signals": {}, "posizioni_pb": {}, "ultimo_pb": {}, "avvisi": {}}


def carica() -> Dict[str, dict]:
    """Legge lo stato salvato. Se il file manca o è illeggibile riparte da zero."""
    p = percorso()
    try:
        with open(p, encoding="utf-8") as f:
            dati = _da_json(json.load(f))
        stato = {k: dict(dati.get(k) or {}) for k in VUOTO}
        logger.info(f"Stato caricato da {p}: {len(stato['last_signals'])} segnali recenti, "
                    f"{len(stato['posizioni_pb'])} posizioni teoriche aperte")
        return stato
    except FileNotFoundError:
        logger.info(f"Nessuno stato salvato in {p}: si parte da zero")
    except Exception as e:
        logger.error(f"Stato in {p} illeggibile ({type(e).__name__}: {e}): si parte da zero")
    return {k: {} for k in VUOTO}


def cartella() -> str:
    """Cartella del file di stato (lì vive anche il registro dei segnali)."""
    return os.path.dirname(percorso()) or "."


def scrivi_json(p: str, dati: Any) -> bool:
    """Scrive un JSON in modo atomico (file temporaneo + rename), con date e orari."""
    with _lock:
        try:
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_a_json(dati), f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
            return True
        except Exception as e:
            logger.error(f"Salvataggio di {p} fallito: {type(e).__name__}: {e}")
            return False


def leggi_json(p: str, default: Any) -> Any:
    try:
        with open(p, encoding="utf-8") as f:
            return _da_json(json.load(f))
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.error(f"{p} illeggibile ({type(e).__name__}: {e}): si riparte da zero")
        return default


def salva(last_signals: dict, posizioni_pb: dict, ultimo_pb: dict, avvisi: dict = None) -> bool:
    """Scrive lo stato in modo atomico (file temporaneo + rename)."""
    p = percorso()
    dati = _a_json({"last_signals": last_signals, "posizioni_pb": posizioni_pb,
                    "ultimo_pb": ultimo_pb, "avvisi": avvisi or {}})
    with _lock:
        try:
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dati, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
            return True
        except Exception as e:
            logger.error(f"Salvataggio stato in {p} fallito: {type(e).__name__}: {e}")
            return False
