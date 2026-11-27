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


def _fermo(bot, ticker, ultima, ora):
    bot["SALUTE_DATI"].clear()
    bot["SALUTE_DATI"][ticker] = {"fallimenti": 0, "ultimo_ok": ora - timedelta(minutes=5),
                                  "errore": "", "ultima_candela": ultima}


def test_dati_fermi_e_ripresa(bot, inviati, monkeypatch):
    """Lo scenario del 27/11/2026: Yahoo risponde ma l'oro è fermo a giovedì 05:45."""
    R = bot["ROME_TZ"]
    ora = R.localize(datetime(2026, 11, 27, 11, 40))
    ultima = R.localize(datetime(2026, 11, 26, 6, 0))            # chiusura candela 05:45–06:00

    class Orologio(datetime):
        @classmethod
        def now(cls, tz=None):
            return ora.astimezone(tz) if tz else ora
    monkeypatch.setitem(bot, "datetime", Orologio)
    bot["ticker_fermi"].clear()
    _fermo(bot, "GC=F", ultima, ora)
    bot["job_salute_dati"](); bot["job_salute_dati"]()           # un solo avviso
    assert len(inviati) == 1 and "Dati fermi" in inviati[0] and "giovedì 26/11 alle 06:00" in inviati[0]
    bot["SALUTE_DATI"]["GC=F"]["ultima_candela"] = ora - timedelta(minutes=10)
    bot["job_salute_dati"]()
    assert "di nuovo aggiornati" in inviati[-1] and len(inviati) == 2


def test_nessun_falso_allarme_nel_weekend(bot, inviati, monkeypatch):
    """Lunedì 01:00 italiane: l'oro ha riaperto domenica alle 18:00 ET (00:00 italiane),
    meno di 2 ore di mercato dall'ultima candela di venerdì → nessun avviso."""
    R = bot["ROME_TZ"]
    ora = R.localize(datetime(2026, 12, 7, 1, 0))
    ultima = R.localize(datetime(2026, 12, 4, 23, 0))            # venerdì 17:00 ET

    class Orologio(datetime):
        @classmethod
        def now(cls, tz=None):
            return ora.astimezone(tz) if tz else ora
    monkeypatch.setitem(bot, "datetime", Orologio)
    bot["ticker_fermi"].clear()
    _fermo(bot, "GC=F", ultima, ora)
    bot["job_salute_dati"]()
    assert inviati == []


def test_nessun_avviso_per_strumenti_non_scaricati(bot, inviati):
    """Strategia spenta o fuori orario: il ticker non viene scaricato da tempo → nessun avviso."""
    R = bot["ROME_TZ"]
    ora = datetime.now(R)
    bot["ticker_fermi"].clear()
    bot["SALUTE_DATI"].clear()
    bot["SALUTE_DATI"]["^GSPC"] = {"fallimenti": 0, "ultimo_ok": ora - timedelta(hours=20),
                                   "errore": "", "ultima_candela": ora - timedelta(days=3)}
    bot["job_salute_dati"]()
    assert inviati == []


def test_solo_registro(bot, inviati, monkeypatch, tmp_path):
    """STRATEGIE_OMBRA: il segnale non va su Telegram, finisce nel registro come 'ombra',
    non conta nel rischio totale e nel resoconto è segnato 'solo registro'."""
    import registro
    monkeypatch.setenv("STATO_PATH", str(tmp_path / "stato.json"))
    monkeypatch.setattr(registro, "_registro", None)
    monkeypatch.setitem(bot, "registro", registro)
    monkeypatch.setitem(bot, "STRATEGIE_OMBRA", {"orb_cat"})
    monkeypatch.setitem(bot, "last_signals", {})
    telegram = []
    monkeypatch.setitem(bot, "send_telegram", lambda s: telegram.append(s) or True)
    R = bot["ROME_TZ"]
    fine = datetime.now(R).replace(second=0, microsecond=0)
    sig = {"direzione": "LONG", "entry": "100.00", "sl": "99.00", "tp": "102.50",
           "strategia": "🟢 ORB CAT", "asset": "CAT", "timeframe": "15min", "rr": "1:2.5",
           "candela_fine": fine}
    bot["run_check"]("orb_cat", lambda: dict(sig), 8)
    bot["run_check"]("orb_cat", lambda: dict(sig), 8)            # pausa: non registra due volte
    assert telegram == []
    recs = registro._tutti()
    assert len(recs) == 1 and recs[0]["strategia"] == "orb_cat" and recs[0]["ombra"] is True
    monkeypatch.setitem(bot, "posizioni_pb", {})
    assert bot["blocco_rischio"]("orb_sp500", "LONG") == ""     # l'ombra non è una posizione vera
    assert "ORB CAT 👻 <i>solo registro</i>" in registro.riepilogo(7)


def test_ombra_vince_su_spente(monkeypatch):
    """Una strategia sia in STRATEGIE_SPENTE sia in STRATEGIE_OMBRA gira in solo registro;
    il ritracciamento non è ammesso in ombra."""
    import os
    monkeypatch.setenv("STRATEGIE_SPENTE", "orb_ant,orb_cat,orb_avgo,pullback_avgo")
    monkeypatch.setenv("STRATEGIE_OMBRA", "orb_cat,orb_avgo,pullback_avgo")
    monkeypatch.setenv("PORT", str(__import__("conftest")._porta_libera()))
    radice = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(radice, "main.py"), encoding="utf-8").read().split("# ── Main ──")[0]
    ns = {"__name__": "main_test2"}
    exec(compile(src, "main.py", "exec"), ns)
    assert ns["STRATEGIE_OMBRA"] == {"orb_cat", "orb_avgo"}
    assert ns["STRATEGIE_SPENTE"] == {"orb_ant", "pullback_avgo"}
