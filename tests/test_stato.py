from datetime import date, datetime

import stato
import strategies as st


def test_salva_e_ricarica(tmp_path, monkeypatch):
    monkeypatch.setenv("STATO_PATH", str(tmp_path / "stato.json"))
    adesso = datetime.now(st.ROME_TZ)
    ls = {"orb_sp500": {"direction": "LONG", "timestamp": adesso}}
    pb = {"pullback_sp500": {"entry_date": date(2027, 3, 17), "atr": 3.2, "signal_date": date(2027, 3, 16)}}
    ub = {"pullback_sp500": date(2027, 3, 16)}
    assert stato.salva(ls, pb, ub, {"calendario": date(2027, 1, 1)})
    r = stato.carica()
    assert r["posizioni_pb"] == pb and r["ultimo_pb"] == ub
    assert r["avvisi"] == {"calendario": date(2027, 1, 1)}
    assert abs((r["last_signals"]["orb_sp500"]["timestamp"] - adesso).total_seconds()) < 1


def test_file_illeggibile_riparte_da_zero(tmp_path, monkeypatch):
    p = tmp_path / "stato.json"
    p.write_text("{non è json")
    monkeypatch.setenv("STATO_PATH", str(p))
    assert stato.carica()["posizioni_pb"] == {}
