"""Funzioni di main.py: avvisi, controllo di salute, rischio totale."""
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def inviati(bot, monkeypatch):
    lista = []
    monkeypatch.setitem(bot, "send_testo", lambda t: lista.append(t) or True)
    monkeypatch.setitem(bot, "salva_stato", lambda: None)
    monkeypatch.setitem(bot, "is_news_window", lambda *a, **k: False)
    return lista


def test_errori_di_fila_e_ripristino(bot, inviati):
    def rotta():
        raise RuntimeError("Yahoo ha cambiato formato")
    for _ in range(4):
        bot["run_check"]("orb_cat", rotta, 8)
    bot["run_check"]("orb_cat", lambda: None, 8)
    assert len(inviati) == 2 and "3 controlli di fila" in inviati[0] and "di nuovo funzionante" in inviati[1]


def test_dati_yahoo_assenti(bot, inviati):
    S = bot["SALUTE_DATI"]
    S.clear()
    S["GC=F"] = {"fallimenti": 5, "ultimo_ok": datetime.now(bot["ROME_TZ"]) - timedelta(hours=2), "errore": "timeout"}
    bot["job_salute_dati"](); bot["job_salute_dati"]()
    S["GC=F"]["fallimenti"] = 0
    bot["job_salute_dati"]()
    assert len(inviati) == 2 and "non disponibili" in inviati[0] and "di nuovo disponibili" in inviati[1]


def test_calendario_in_scadenza(bot, inviati, monkeypatch):
    oggi = datetime.now(bot["ROME_TZ"]).date()
    bot["avvisi"].clear()
    monkeypatch.setitem(bot, "fine_calendario", lambda: oggi + timedelta(days=200))
    bot["job_calendario"]()
    assert inviati == []
    monkeypatch.setitem(bot, "fine_calendario", lambda: oggi + timedelta(days=30))
    bot["job_calendario"](); bot["job_calendario"]()            # una sola volta a settimana
    assert len(inviati) == 1 and "tra 30 giorni" in inviati[0]


def test_controllo_di_salute(bot, monkeypatch):
    porta = __import__("os").environ["PORT"]

    def codice():
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{porta}/", timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code
    bot["job_battito"]()
    assert codice() == 200
    bot["ULTIMO_BATTITO"] = datetime.now(bot["ROME_TZ"]) - timedelta(minutes=30)
    assert codice() == 503
    bot["job_battito"]()


def test_rischio_totale(bot, monkeypatch):
    class RegistroFinto:
        @staticmethod
        def aperti():
            return [{"strategia": "orb_sp500", "direzione": "LONG"},
                    {"strategia": "vwap_gold", "direzione": "SHORT"}]
    monkeypatch.setitem(bot, "registro", RegistroFinto)
    monkeypatch.setitem(bot, "posizioni_pb", {"pullback_sp500": {"entry_date": None}})
    testo = bot["blocco_rischio"]("kumo_nasdaq", "LONG")
    assert "ORB S&amp;P500 LONG" in testo and "Ritracciamento S&amp;P500 LONG" in testo
    assert "4 posizioni ≈ 6% di rischio" in testo and "oltre il limite" in testo
    assert "azionario USA: ORB S&amp;P500, Ritracciamento S&amp;P500" in testo   # non l'oro
    # il ritracciamento non conta sé stesso
    assert "Ritracciamento" not in bot["blocco_rischio"]("pullback_sp500", "LONG")


def test_rischio_nessuna_posizione(bot, monkeypatch):
    class Vuoto:
        @staticmethod
        def aperti():
            return []
    monkeypatch.setitem(bot, "registro", Vuoto)
    monkeypatch.setitem(bot, "posizioni_pb", {})
    assert bot["blocco_rischio"]("orb_sp500", "LONG") == ""


def test_messaggio_mostra_il_rischio(bot, monkeypatch):
    import notifier
    testi = []
    monkeypatch.setattr(notifier, "_send", lambda t: testi.append(t) or True)
    sig = {"strategia": "🟢 ORB S&amp;P500", "asset": "S&amp;P 500", "timeframe": "15min", "direzione": "LONG",
           "entry": "100.00", "sl": "99.00", "tp": "102.50", "rr": "1:2.5",
           "rischio_aperti": "📂 <b>Già aperti (teorici):</b> Kumo Nasdaq LONG\n"}
    notifier.send_telegram(sig)
    assert "Già aperti" in testi[0] and "Distanze dall'entry" in testi[0]
