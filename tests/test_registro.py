from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

import registro

ROME, ET = registro.ROME_TZ, registro.ET_TZ
FINE = ROME.localize(datetime(2027, 3, 16, 15, 0))          # settimana sfasata


@pytest.fixture(autouse=True)
def registro_vuoto(tmp_path, monkeypatch):
    monkeypatch.setenv("STATO_PATH", str(tmp_path / "stato.json"))
    monkeypatch.setattr(registro, "_registro", None)


def _sig(direzione, entry, sl, tp, prezzo=None):
    return {"direzione": direzione, "entry": str(entry), "sl": str(sl), "tp": str(tp),
            "candela_fine": FINE, "prezzo_attuale": prezzo}


def _finto(ticker, period, interval):
    passo = 15 if interval == "15m" else 60
    idx = pd.date_range(FINE - timedelta(hours=2), periods=200, freq=f"{passo}min")
    if ticker == "^GSPC":
        c = np.minimum(100 + 0.2 * np.arange(len(idx)), 103.0)   # tocca il target
    elif ticker == "CAT":
        c = np.full(len(idx), 99.6)                              # piatto: chiusura forzata
    else:
        c = np.full(len(idx), 100.5)                             # swing ancora aperta
    return pd.DataFrame({"Open": c, "High": c + 0.05, "Low": c - 0.05, "Close": c, "Volume": 1}, index=idx)


def test_esiti_e_salvataggio():
    inviato = FINE + timedelta(minutes=2)
    registro.registra("orb_sp500", "^GSPC", _sig("LONG", 100, 99, 102.5, "100.20"), inviato)
    registro.registra("orb_cat", "CAT", _sig("SHORT", 100, 101, 97.5, "99.90"), inviato)
    registro.registra("kumo_nasdaq", "^NDX", _sig("LONG", 100, 98, 104), inviato)
    assert registro._tutti()[0]["scadenza"] == ROME.localize(datetime(2027, 3, 16, 20, 45))

    ora = ET.localize(datetime(2027, 3, 16, 16, 30)).astimezone(ROME)
    registro.valuta(_finto, ora)
    esiti = {r["strategia"]: (r["esito"], r["r"]) for r in registro._tutti()}
    assert esiti["orb_sp500"] == ("TARGET", pytest.approx(2.48, abs=0.01))
    assert esiti["orb_cat"] == ("CHIUSURA", pytest.approx(0.36, abs=0.01))
    assert esiti["kumo_nasdaq"] == (None, None)
    assert [r["strategia"] for r in registro.aperti(ora)] == ["kumo_nasdaq"]

    registro._registro = None                                     # ricarica dal file
    assert registro._tutti()[0]["esito"] == "TARGET"

    testo = registro.riepilogo(7, ora=ora + timedelta(days=1))
    assert "ORB S&amp;P500" in testo and "+2,5R" in testo and "⏳ 1" in testo


def test_stop_prima_del_target_nella_stessa_candela():
    registro.registra("orb_sp500", "^GSPC", _sig("LONG", 100, 99, 101), FINE)

    def candela_enorme(*a):
        idx = pd.date_range(FINE, periods=4, freq="15min")
        return pd.DataFrame({"Open": 100, "High": 102, "Low": 98, "Close": 100, "Volume": 1}, index=idx)
    registro.valuta(candela_enorme, FINE + timedelta(hours=1))
    assert registro._tutti()[0]["esito"] == "STOP"
