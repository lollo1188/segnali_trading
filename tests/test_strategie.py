"""ORB in una settimana con ora legale USA/Europa sfasata (16/3/2027)."""
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

import strategies as st

ET, ROME = st.ET_TZ, st.ROME_TZ
OGGI = date(2027, 3, 16)


def _barre(giorno, n_rialzo, base=100.0):
    idx, righe, p = [], [], base
    t = ET.localize(datetime.combine(giorno, time(9, 30)))
    for k in range(26):
        o = p
        if k == 0:
            h, lo, c = base + 1, base, base + 0.5
        elif k <= 1:
            h, lo, c = o + 0.3, o - 0.3, o + 0.05
        elif k <= 1 + n_rialzo:
            c = o + 0.6; h, lo = c + 0.1, o - 0.1
        else:
            c = o + 0.02; h, lo = c + 0.2, o - 0.2
        righe.append((o, h, lo, c, 1000)); idx.append(t + timedelta(minutes=15 * k)); p = c
    return pd.DataFrame(righe, columns=["Open", "High", "Low", "Close", "Volume"],
                        index=pd.DatetimeIndex(idx).tz_convert(ROME))


@pytest.fixture
def dati_breakout(monkeypatch):
    df = pd.concat([_barre(date(2027, 3, 12), 0), _barre(date(2027, 3, 15), 0), _barre(OGGI, 6)])
    adesso = ET.localize(datetime.combine(OGGI, time(11, 2)))
    df = df[df.index <= adesso]
    monkeypatch.setattr(st, "get_data", lambda *a, **k: df.copy())
    monkeypatch.setattr(st, "adesso_et", lambda: adesso)
    return adesso


def test_opening_range_alle_0930_new_york():
    assert st.ora_it(time(9, 30), OGGI) == "14:30"          # non 15:30


def test_orb_sp500_settimana_sfasata(dati_breakout):
    sig = st.check_orb_sp500()
    assert sig and sig["direzione"] == "LONG"
    assert sig["chiudi_entro"] == "20:45"
    assert sig["candela_fine"] == ROME.localize(datetime(2027, 3, 16, 16, 0))


def test_orb_azioni_settimana_sfasata(dati_breakout):
    # regressione: un nome duplicato (fine_candela) faceva andare in errore questa funzione
    sig = st._check_orb_azione("CAT", "ORB CAT", "Caterpillar", True, 0.5, "test")
    assert sig and sig["direzione"] == "LONG" and "candela_fine" in sig


def test_orb_azioni_troppo_presto(dati_breakout, monkeypatch):
    monkeypatch.setattr(st, "adesso_et", lambda: ET.localize(datetime.combine(OGGI, time(9, 50))))
    assert st._check_orb_azione("CAT", "x", "x", True, 0.5, "") is None


def test_finestre_new_york():
    assert st.in_orario_et(time(9, 45), time(15, 45), ROME.localize(datetime(2027, 3, 16, 14, 45)))
    assert not st.in_orario_et(time(9, 45), time(15, 45), ROME.localize(datetime(2027, 3, 16, 21, 0)))


def test_tutti_i_segnali_hanno_la_chiusura_della_candela():
    import inspect
    codice = inspect.getsource(st)
    assert codice.count('"candela": candle_label') == codice.count('"candela_fine": chiusura_candela') == 11


def test_kumo_valuta_l_ultima_candela_chiusa():
    """Regressione: il chikou spostato nel futuro faceva valutare una candela di 27 periodi fa."""
    import inspect
    for f in (st.check_kumo_gold_4h, st.check_kumo_nasdaq_1h):
        src = inspect.getsource(f)
        assert 'df["chikou"]' not in src and 'shift(26)' in src


def test_ritracciamento_ingresso_in_giorno_festivo(monkeypatch):
    """Ingresso previsto in una festività non in calendario: la posizione non va persa."""
    import numpy as np
    giorni = pd.bdate_range(end="2026-07-06", periods=301)
    giorni = giorni[giorni != pd.Timestamp("2026-07-03")]
    c = 5000 + np.cumsum(np.random.default_rng(3).normal(1, 5, len(giorni)))
    idx = pd.DatetimeIndex([ET.localize(datetime.combine(d.date(), time(0, 0))) for d in giorni]).tz_convert(ROME)
    df = pd.DataFrame({"Open": c, "High": c + 5, "Low": c - 5, "Close": c, "Volume": 1}, index=idx)
    monkeypatch.setattr(st, "get_data", lambda *a, **k: df.copy())

    class Finto(datetime):
        @classmethod
        def now(cls, tz=None):
            return ET.localize(datetime(2026, 7, 6, 16, 10)).astimezone(tz)
    monkeypatch.setattr(st, "datetime", Finto)
    pos = {"entry_date": date(2026, 7, 3), "atr": 1000.0, "signal_date": date(2026, 7, 2)}
    msg, nuova, _ = st.analizza_pullback("^GSPC", "pullback_sp500", "S&P 500", pos)
    assert msg["tipo"] == "MANTIENI"
    assert nuova is not None and nuova["entry_date"] == date(2026, 7, 6)
