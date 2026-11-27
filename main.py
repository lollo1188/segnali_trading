import os, sys, logging, threading, time as time_module
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, time, timedelta
import pytz

# Log su stdout: Railway marca come "error" tutto ciò che arriva da stderr, e così
# anche i messaggi normali sembravano errori. Il livello resta nel testo ([INFO], [ERROR]).
logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ROME_TZ = pytz.timezone("Europe/Rome")


# ── Health server — avvia PRIMA di qualsiasi import pesante ──────────────────
# Risponde 503 se lo scheduler non dà segni di vita da più di MAX_SILENZIO_MIN minuti
# (battito aggiornato da un job ogni 5 minuti). Il ciclo principale, nello stesso caso,
# chiude il processo e Railway lo riavvia (restartPolicyType = "always").
ULTIMO_BATTITO = None
MAX_SILENZIO_MIN = 20


def scheduler_vivo() -> bool:
    if ULTIMO_BATTITO is None:          # appena avviato: il primo battito arriva a breve
        return True
    return (datetime.now(ROME_TZ) - ULTIMO_BATTITO) < timedelta(minutes=MAX_SILENZIO_MIN)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = scheduler_vivo()
        self.send_response(200 if ok else 503); self.end_headers()
        self.wfile.write(b"OK" if ok else b"SCHEDULER FERMO")

    def log_message(self, format, *args): pass


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health server su porta {port}")
    server.serve_forever()


# Avvia health server nel thread principale subito
health_thread = threading.Thread(target=run_health_server, daemon=True)
health_thread.start()
time_module.sleep(2)  # dai tempo al server di partire
logger.info("Health server avviato")

# ── Ora importa il resto ──────────────────────────────────────────────────────
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.events import EVENT_JOB_ERROR
    logger.info("APScheduler importato OK")
except Exception as e:
    logger.error(f"Errore import APScheduler: {e}")

try:
    from strategies import (
        check_vwap_reversal_gold, check_london_sweep_gold,
        check_orb_sp500, check_kumo_gold_4h, check_kumo_nasdaq_1h,
        check_orb_ant, check_orb_cat, check_orb_avgo, analizza_pullback,
        get_last_price, get_data, fmt, in_orario_et, ET_TZ, SALUTE_DATI, TICKER_GOLD, TICKER_SP500, TICKER_NASDAQ,
        TICKER_ANT, TICKER_CAT, TICKER_AVGO,
    )
    logger.info("Strategies importate OK")
except Exception as e:
    logger.error(f"Errore import strategies: {e}")

try:
    from notifier import send_telegram, send_startup_message, send_pullback, send_testo
    logger.info("Notifier importato OK")
except Exception as e:
    logger.error(f"Errore import notifier: {e}")

try:
    from news_filter import (is_news_window, prossimo_sblocco, motivo_blocco, fine_calendario,
                             minuti_di_mercato)
    logger.info("News filter importato OK")
except Exception as e:
    logger.error(f"Errore import news_filter: {e}")
    def is_news_window(name=None): return False
    def prossimo_sblocco(name=None): return None
    def motivo_blocco(name=None): return None
    def fine_calendario(): return None
    def minuti_di_mercato(tipo, da, a, passo_min=5): return 0


try:
    import stato
    _STATO = stato.carica()
    logger.info(f"Stato salvato in {stato.percorso()}"
                + ("" if stato.su_volume() else " — ⚠️ nessun volume /data: si perde a ogni deploy"))
except Exception as e:
    logger.error(f"Errore import stato: {e}")
    stato = None
    _STATO = {"last_signals": {}, "posizioni_pb": {}, "ultimo_pb": {}, "avvisi": {}}

try:
    import registro
    logger.info(f"Registro segnali in {registro.percorso()}")
except Exception as e:
    logger.error(f"Errore import registro: {e}")
    registro = None


# ── Logica bot ────────────────────────────────────────────────────────────────
# Memoria del bot: caricata dal file di stato e salvata a ogni modifica.
last_signals: dict = _STATO["last_signals"]
avvisi: dict = _STATO.get("avvisi", {})       # quando sono stati inviati gli avvisi periodici


def salva_stato():
    if stato is not None:
        stato.salva(last_signals, posizioni_pb, ultimo_pb, avvisi)

# Ticker da cui leggere il prezzo attuale per ogni strategia (slittamento)
SIGNAL_TICKER = {
    "vwap_gold": TICKER_GOLD,
    "london_sweep": TICKER_GOLD,
    "orb_sp500": TICKER_SP500,
    "kumo_gold_4h": TICKER_GOLD,
    "kumo_nasdaq": TICKER_NASDAQ,
    "orb_ant": TICKER_ANT,
    "orb_cat": TICKER_CAT,
    "orb_avgo": TICKER_AVGO,
    "pullback_sp500": TICKER_SP500,
    "pullback_avgo": TICKER_AVGO,
}


# ── Interruttore strategie ────────────────────────────────────────────────────
# Per SPEGNERE strategie senza toccare il codice: su Railway → Variables aggiungi
#   STRATEGIE_SPENTE = orb_cat,orb_avgo
# Nomi validi: vwap_gold, london_sweep, orb_sp500, orb_ant, orb_cat, orb_avgo,
#              kumo_gold_4h, kumo_nasdaq
# Variabile assente o vuota = tutte attive. Per riaccenderle togli il nome dall'elenco
# (Railway riavvia il bot da solo).
NOMI_STRATEGIE = list(SIGNAL_TICKER.keys())


def _leggi_elenco(variabile, validi=None):
    validi = set(validi or NOMI_STRATEGIE)
    grezzo = os.environ.get(variabile, "")
    nomi = {x.strip().lower() for x in grezzo.replace(";", ",").replace(" ", ",").split(",") if x.strip()}
    sconosciute = nomi - validi
    if sconosciute:
        logger.warning(f"{variabile}: nomi non riconosciuti (ignorati): {sorted(sconosciute)}. "
                       f"Nomi validi: {sorted(validi)}")
    return nomi & validi


def _leggi_spente():
    return _leggi_elenco("STRATEGIE_SPENTE")


# ── Modalità "solo registro" (ombra) ──────────────────────────────────────────
# Su Railway → Variables:  STRATEGIE_OMBRA = orb_cat,orb_avgo
# La strategia gira normalmente ma i segnali NON vengono inviati su Telegram: finiscono
# solo nel registro e nel resoconto del sabato ("solo registro"), così la misuri senza
# riceverla. Vince su STRATEGIE_SPENTE: puoi lasciarla anche tra le spente.
# Il ritracciamento giornaliero (pullback_*) non è supportato: il registro non lo segue.
STRATEGIE_OMBRA = _leggi_elenco("STRATEGIE_OMBRA",
                                [n for n in NOMI_STRATEGIE if not n.startswith("pullback_")])
STRATEGIE_SPENTE = _leggi_spente() - STRATEGIE_OMBRA
if STRATEGIE_SPENTE:
    logger.info(f"Strategie SPENTE da STRATEGIE_SPENTE: {sorted(STRATEGIE_SPENTE)}")
if STRATEGIE_OMBRA:
    logger.info(f"Strategie SOLO REGISTRO da STRATEGIE_OMBRA: {sorted(STRATEGIE_OMBRA)}")


def should_send(name, direction, cooldown_h=4):
    now = datetime.now(ROME_TZ)
    if name not in last_signals: return True
    last = last_signals[name]
    hours = (now - last["timestamp"]).total_seconds() / 3600
    return not (last["direction"] == direction and hours < cooldown_h)


def register(name, direction):
    last_signals[name] = {"direction": direction, "timestamp": datetime.now(ROME_TZ)}
    salva_stato()


def _pianifica_ripetizione(name, fn, cooldown_h):
    """Se il controllo è stato saltato per una notizia, lo ripete subito dopo la
    fine del blocco (così la candela non va persa). Per blocchi lunghi (festivi,
    utili) non ripete."""
    try:
        fine = prossimo_sblocco(name)
        if fine is None: return
        if fine - datetime.now(ROME_TZ) > timedelta(hours=2): return
        scheduler.add_job(run_check, "date", run_date=fine + timedelta(minutes=1),
                          args=[name, fn, cooldown_h], id=f"retry_{name}",
                          replace_existing=True)
        logger.info(f"[{name}] Controllo ripetuto alle {fine + timedelta(minutes=1):%H:%M}")
    except Exception as e:
        logger.warning(f"[{name}] Ripetizione non pianificata: {type(e).__name__}")


# Errori consecutivi per strategia: al terzo di fila avviso su Telegram (una volta),
# e avviso di nuovo quando torna a funzionare.
ERRORI_DI_FILA = 3
errori_job: dict = {}
job_in_errore: set = set()


def _esito_job(name, errore=None):
    if errore is None:
        errori_job[name] = 0
        if name in job_in_errore:
            job_in_errore.discard(name)
            send_testo(f"✅ <b>{name}</b>: di nuovo funzionante")
        return
    errori_job[name] = errori_job.get(name, 0) + 1
    if errori_job[name] >= ERRORI_DI_FILA and name not in job_in_errore:
        job_in_errore.add(name)
        send_testo(f"🚨 <b>{name}</b>: {errori_job[name]} controlli di fila in errore.\n"
                   f"Ultimo errore: <code>{str(errore)[:200]}</code>\n"
                   f"I segnali di questa strategia potrebbero non arrivare: guarda i log su Railway.")


# ── Rischio totale delle posizioni teoriche aperte ───────────────────────────
# Su Railway → Variables (facoltative): RISCHIO_PER_TRADE (default 1.5, in % del capitale)
# e MAX_RISCHIO_TOTALE (default 4). Servono solo per il calcolo mostrato nel messaggio.
def _percentuale(nome_var, default):
    try:
        return float(os.environ.get(nome_var, default))
    except ValueError:
        logger.warning(f"{nome_var} non valido: uso {default}")
        return default


RISCHIO_PER_TRADE = _percentuale("RISCHIO_PER_TRADE", 1.5)
MAX_RISCHIO_TOTALE = _percentuale("MAX_RISCHIO_TOTALE", 4.0)
# Strumenti che si muovono insieme: due posizioni nella stessa direzione sullo stesso
# gruppo sono quasi la stessa scommessa.
GRUPPO = {"vwap_gold": "oro", "london_sweep": "oro", "kumo_gold_4h": "oro",
          "orb_sp500": "azionario USA", "kumo_nasdaq": "azionario USA", "orb_ant": "azionario USA",
          "orb_cat": "azionario USA", "orb_avgo": "azionario USA",
          "pullback_sp500": "azionario USA", "pullback_avgo": "azionario USA"}
NOME_BREVE = {"vwap_gold": "VWAP Oro", "london_sweep": "London Sweep", "kumo_gold_4h": "Kumo Oro",
              "orb_sp500": "ORB S&amp;P500", "kumo_nasdaq": "Kumo Nasdaq", "orb_ant": "ORB ANT",
              "orb_cat": "ORB CAT", "orb_avgo": "ORB AVGO",
              "pullback_sp500": "Ritracciamento S&amp;P500", "pullback_avgo": "Ritracciamento AVGO"}


def _fmt_pct(x):
    return f"{x:.1f}".replace(".", ",").replace(",0", "") + "%"


def blocco_rischio(name, direzione):
    """Righe per il messaggio: posizioni teoriche già aperte, rischio totale e concentrazione.
    Stringa vuota se non c'è nulla di aperto (o se il registro non è disponibile)."""
    try:
        aperte = []                                   # (strategia, direzione)
        if registro is not None:
            aperte += [(r["strategia"], r["direzione"]) for r in registro.aperti() if not r.get("ombra")]
        aperte += [(k, "LONG") for k in posizioni_pb if k != name]
        if not aperte:
            return ""
        elenco = " · ".join(f"{NOME_BREVE.get(k, k)} {d}" for k, d in aperte)
        totale = (len(aperte) + 1) * RISCHIO_PER_TRADE
        oltre = totale > MAX_RISCHIO_TOTALE + 1e-9
        righe = [f"📂 <b>Già aperti (teorici):</b> {elenco}",
                 f"💼 Con questo: {len(aperte) + 1} posizioni ≈ {_fmt_pct(totale)} di rischio "
                 f"(limite {_fmt_pct(MAX_RISCHIO_TOTALE)})"
                 + (" — ⚠️ <b>oltre il limite</b>: salta il segnale o riduci la size" if oltre else "")]
        gruppo = GRUPPO.get(name)
        stessi = [NOME_BREVE.get(k, k) for k, d in aperte if GRUPPO.get(k) == gruppo and d == direzione]
        if gruppo and stessi:
            righe.append(f"🔗 Stessa direzione sull'{gruppo}: {', '.join(stessi)} → si muovono insieme, "
                         f"rischio concentrato")
        return "\n".join(righe) + "\n"
    except Exception as e:
        logger.warning(f"[{name}] Rischio totale non calcolato: {type(e).__name__}: {e}")
        return ""


def run_check(name, fn, cooldown_h=4):
    if name in STRATEGIE_SPENTE:
        return
    try:
        if is_news_window(name):
            logger.info(f"[{name}] Filtro notizie attivo: {motivo_blocco(name)}")
            _pianifica_ripetizione(name, fn, cooldown_h)
            return
        signal = fn()
        if signal and name in STRATEGIE_OMBRA:
            # Solo registro: niente Telegram, stessa pausa tra segnali del bot vero
            if should_send(name, signal["direzione"], cooldown_h):
                register(name, signal["direzione"])
                if registro is not None:
                    registro.registra(name, SIGNAL_TICKER.get(name, ""), signal, ombra=True)
                logger.info(f"[{name}] Segnale SOLO REGISTRO: {signal['direzione']} (non inviato)")
            _esito_job(name)
            return
        if signal and should_send(name, signal["direzione"], cooldown_h):
            # Prezzo attuale al momento dell'invio → il messaggio mostra lo slittamento.
            # Se non disponibile, il messaggio parte senza il blocco slittamento.
            try:
                px = get_last_price(SIGNAL_TICKER.get(name, ""))
                if px:
                    signal["prezzo_attuale"] = fmt(px)
            except Exception as e:
                logger.warning(f"[{name}] Prezzo attuale non disponibile: {type(e).__name__}")
            signal["rischio_aperti"] = blocco_rischio(name, signal["direzione"])
            if send_telegram(signal):
                logger.info(f"[{name}] Segnale inviato: {signal['direzione']}")
                register(name, signal["direzione"])
                if registro is not None:
                    registro.registra(name, SIGNAL_TICKER.get(name, ""), signal)
            else:
                logger.error(f"[{name}] Invio Telegram fallito")
        _esito_job(name)
    except Exception as e:
        logger.error(f"[{name}] Errore: {e}")
        _esito_job(name, f"{type(e).__name__}: {e}")


def job_vwap_gold():
    logger.info("▶ VWAP Reversal Oro")
    run_check("vwap_gold", check_vwap_reversal_gold, 4)


def job_london_sweep():
    now = datetime.now(ROME_TZ)
    if not (time(8, 0) <= now.time() <= time(10, 0)): return
    logger.info("▶ London Sweep Oro")
    run_check("london_sweep", check_london_sweep_gold, 12)


# Strategie USA: finestre in ORA DI NEW YORK (seguono l'apertura di Wall Street
# anche nelle settimane con ora legale USA/Europa sfasata).
def job_orb_sp500():
    if not in_orario_et(time(9, 45), time(15, 45)): return      # 15:45–21:45 italiane
    logger.info("▶ ORB S&P500")
    run_check("orb_sp500", check_orb_sp500, 8)


def _job_orb_azione(name, label, fn):
    if not in_orario_et(time(10, 0), time(14, 30)): return      # 16:00–20:30 italiane
    logger.info(f"▶ ORB {label}")
    run_check(name, fn, 8)


def job_orb_ant():
    _job_orb_azione("orb_ant", "Anthropic (ANT)", check_orb_ant)


def job_orb_cat():
    _job_orb_azione("orb_cat", "Caterpillar (CAT)", check_orb_cat)


def job_orb_avgo():
    _job_orb_azione("orb_avgo", "Broadcom (AVGO)", check_orb_avgo)


# ── Ritracciamento nel trend (giornaliero) ────────────────────────────────────
# name -> (ticker, nome mostrato). Controllo ogni sera dopo la chiusura USA.
PULLBACK = {
    "pullback_sp500": (TICKER_SP500, "S&P 500"),
    "pullback_avgo": (TICKER_AVGO, "Broadcom (AVGO)"),
}
posizioni_pb: dict = _STATO["posizioni_pb"]   # posizione TEORICA per strategia (salvata su file)
ultimo_pb: dict = _STATO["ultimo_pb"]         # data dell'ultima candela già elaborata


def job_pullback(name):
    if name in STRATEGIE_SPENTE:
        return
    ticker, nome = PULLBACK[name]
    try:
        logger.info(f"▶ Ritracciamento {nome}")
        msg, pos, data = analizza_pullback(ticker, name, nome, posizioni_pb.get(name))
        if data is None:
            logger.info(f"[{name}] Candela giornaliera non ancora disponibile (si riprova)")
            return
        if ultimo_pb.get(name) == data:
            return                                   # già elaborata (secondo controllo serale)
        ultimo_pb[name] = data
        if pos:
            posizioni_pb[name] = pos
        else:
            posizioni_pb.pop(name, None)
        salva_stato()
        if msg and msg.get("tipo") == "ENTRATA":
            msg["rischio_aperti"] = blocco_rischio(name, "LONG")
        if msg and send_pullback(msg):
            logger.info(f"[{name}] Messaggio inviato: {msg['tipo']}")
        elif msg:
            logger.error(f"[{name}] Invio Telegram fallito")
    except Exception as e:
        logger.error(f"[{name}] Errore: {type(e).__name__}: {e}")


def job_pullback_sp500():
    job_pullback("pullback_sp500")


def job_pullback_avgo():
    job_pullback("pullback_avgo")



def job_kumo_gold_4h():
    logger.info("▶ Kumo Rider Oro 4H")
    run_check("kumo_gold_4h", check_kumo_gold_4h, 12)


def job_kumo_nasdaq():
    # Gira ai :32 (le candele orarie del Nasdaq si chiudono alle :30 di New York).
    # Fino alle 15:35 ET così valuta anche la candela 14:30–15:30.
    if not in_orario_et(time(9, 30), time(15, 35)): return      # 15:30–21:35 italiane
    logger.info("▶ Kumo Rider Nasdaq 1H")
    run_check("kumo_nasdaq", check_kumo_nasdaq_1h, 8)


# ── Registro segnali e resoconto settimanale ──────────────────────────────────
def job_valuta_registro():
    if registro is None:
        return
    try:
        chiusi = registro.valuta(get_data)
        if chiusi:
            logger.info(f"[registro] {len(chiusi)} posizioni teoriche chiuse")
    except Exception as e:
        logger.error(f"[registro] Errore valutazione: {type(e).__name__}: {e}")


def job_resoconto():
    if registro is None:
        return
    job_valuta_registro()
    try:
        if send_testo(registro.riepilogo(7)):
            logger.info("[registro] Resoconto settimanale inviato")
    except Exception as e:
        logger.error(f"[registro] Errore resoconto: {type(e).__name__}: {e}")


# ── Avvisi: dati Yahoo, calendario, battito ───────────────────────────────────
DATI_FALLITI_DI_FILA = 3
DATI_SILENZIO_MIN = 45
ticker_in_errore: set = set()


# Dati fermi: Yahoo risponde ma l'ultima candela non avanza. Avviso se, dalla chiusura
# dell'ultima candela ricevuta, il mercato è rimasto aperto per più di DATI_FERMI_MIN minuti.
DATI_FERMI_MIN = 120
ticker_fermi: set = set()


def tipo_mercato(ticker):
    """'future' per i future CME (oro: GC=F), 'usa' per indici e azioni USA."""
    return "future" if ticker.endswith("=F") else "usa"


def _descrivi_ora(t):
    oggi = datetime.now(ROME_TZ).date()
    giorni = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    t = t.astimezone(ROME_TZ)
    quando = "oggi" if t.date() == oggi else f"{giorni[t.weekday()]} {t:%d/%m}"
    return f"{quando} alle {t:%H:%M}"


def job_salute_dati():
    """Avvisa se un ticker non scarica dati da un po', o se i dati sono fermi a mercato
    aperto (e quando tornano a posto)."""
    now = datetime.now(ROME_TZ)
    for ticker, s in list(SALUTE_DATI.items()):
        # ── dati fermi: solo se il ticker è stato scaricato di recente (strategia attiva)
        ultima = s.get("ultima_candela")
        recente = s.get("ultimo_ok") is not None and now - s["ultimo_ok"] < timedelta(minutes=45)
        if ultima is not None and recente:
            aperto_min = minuti_di_mercato(tipo_mercato(ticker), ultima, now)
            ore = (now - ultima).total_seconds() / 3600
            if aperto_min > DATI_FERMI_MIN and ticker not in ticker_fermi:
                ticker_fermi.add(ticker)
                send_testo(f"⏸ <b>Dati fermi</b> per <code>{ticker}</code>: ultima candela chiusa "
                           f"{_descrivi_ora(ultima)} ({ore:.0f} ore fa), anche se il mercato dovrebbe "
                           f"essere aperto.\nYahoo risponde ma non aggiorna: le strategie su questo "
                           f"strumento non possono dare segnali. Può essere un orario festivo che il bot "
                           f"non conosce o un problema di Yahoo.")
            elif aperto_min <= DATI_FERMI_MIN and ticker in ticker_fermi:
                ticker_fermi.discard(ticker)
                send_testo(f"✅ Dati di nuovo aggiornati per <code>{ticker}</code>")
        # ── download falliti
        fermo = s["fallimenti"] >= DATI_FALLITI_DI_FILA and (
            s["ultimo_ok"] is None or now - s["ultimo_ok"] > timedelta(minutes=DATI_SILENZIO_MIN))
        if fermo and ticker not in ticker_in_errore:
            ticker_in_errore.add(ticker)
            quando = f"dalle {s['ultimo_ok']:%H:%M}" if s["ultimo_ok"] else "dall'avvio"
            send_testo(f"🚨 <b>Dati Yahoo non disponibili</b> per <code>{ticker}</code> {quando} "
                       f"({s['fallimenti']} tentativi falliti).\nUltimo errore: <code>{s['errore']}</code>\n"
                       f"Le strategie su questo strumento non possono dare segnali.")
        elif s["fallimenti"] == 0 and ticker in ticker_in_errore:
            ticker_in_errore.discard(ticker)
            send_testo(f"✅ Dati Yahoo di nuovo disponibili per <code>{ticker}</code>")


GIORNI_AVVISO_CALENDARIO = 60


def job_calendario():
    """Avvisa (al massimo una volta a settimana) quando il calendario notizie sta finendo."""
    try:
        fine = fine_calendario()
        if fine is None:
            return
        oggi = datetime.now(ROME_TZ).date()
        giorni = (fine - oggi).days
        if giorni > GIORNI_AVVISO_CALENDARIO:
            return
        ultimo = avvisi.get("calendario")
        if ultimo is not None and (oggi - ultimo).days < 7:
            return
        testo = (f"📅 <b>Calendario notizie in scadenza</b>: copre fino al {fine:%d/%m/%Y} "
                 + (f"(tra {giorni} giorni)." if giorni >= 0 else f"(scaduto da {-giorni} giorni!)."))
        testo += ("\nAggiungi NFP, CPI, FOMC e chiusure di Wall Street in <code>news_filter.py</code>: "
                  "dopo quella data il filtro non blocca più le notizie.")
        if send_testo(testo):
            avvisi["calendario"] = oggi
            salva_stato()
    except Exception as e:
        logger.error(f"[calendario] Errore: {type(e).__name__}: {e}")


def job_battito():
    global ULTIMO_BATTITO
    ULTIMO_BATTITO = datetime.now(ROME_TZ)


# ── Main ──────────────────────────────────────────────────────────────────────
logger.info("═══════════════════════════════════════")
logger.info(f" TRADING BOT — {len(NOMI_STRATEGIE) - len(STRATEGIE_SPENTE) - len(STRATEGIE_OMBRA)} strategie attive"
            f" su {len(NOMI_STRATEGIE)}" + (f", {len(STRATEGIE_OMBRA)} solo registro" if STRATEGIE_OMBRA else ""))
logger.info("═══════════════════════════════════════")

try:
    # misfire_grace_time: un job partito con qualche secondo di ritardo (thread occupati)
    # viene eseguito lo stesso invece di essere saltato; coalesce evita esecuzioni doppie.
    scheduler = BackgroundScheduler(timezone=ROME_TZ,
                                    job_defaults={"misfire_grace_time": 120, "coalesce": True})
    scheduler.add_listener(lambda e: logger.error(f"Job error: {e.exception}"), EVENT_JOB_ERROR)
    def _aggiungi_job(funzione, nome, trigger, **kwargs):
        if nome in STRATEGIE_SPENTE:
            logger.info(f"[{nome}] SPENTA: job non avviato")
            return
        scheduler.add_job(funzione, trigger, id=nome, **kwargs)

    # Controlli 2 minuti dopo la chiusura di ogni candela da 15 minuti (:02, :17, :32, :47):
    # il segnale arriva subito, non fino a 15 minuti dopo come con un intervallo libero.
    # I 2 minuti lasciano a Yahoo il tempo di pubblicare la candela chiusa.
    QUARTI = "2,17,32,47"
    _aggiungi_job(job_vwap_gold, "vwap_gold", "cron", minute=QUARTI)
    _aggiungi_job(job_london_sweep, "london_sweep", "cron", minute=QUARTI)
    _aggiungi_job(job_orb_sp500, "orb_sp500", "cron", minute=QUARTI)
    _aggiungi_job(job_orb_ant, "orb_ant", "cron", minute=QUARTI)
    _aggiungi_job(job_orb_cat, "orb_cat", "cron", minute=QUARTI)
    _aggiungi_job(job_orb_avgo, "orb_avgo", "cron", minute=QUARTI)
    # Ogni ora ai :02: le candele 4H sono allineate in UTC, quindi in ora italiana si
    # chiudono a orari diversi tra ora solare e legale. Controllando ogni ora il segnale
    # arriva sempre ~2 minuti dopo la chiusura (la pausa di 12h evita doppioni).
    _aggiungi_job(job_kumo_gold_4h, "kumo_gold_4h", "cron", minute=2)
    # Le candele orarie del Nasdaq iniziano alle :30 di New York
    _aggiungi_job(job_kumo_nasdaq, "kumo_nasdaq", "cron", minute=32)
    # Ritracciamento giornaliero: dopo la chiusura USA, in ora di New York (16:10 e 16:40 ET,
    # di solito 22:10 e 22:40 italiane). Due controlli: se Yahoo è in ritardo il secondo recupera.
    for _ora, _min, _suf in ((16, 10, "a"), (16, 40, "b")):
        for _nome, _fn in (("pullback_sp500", job_pullback_sp500), ("pullback_avgo", job_pullback_avgo)):
            if _nome in STRATEGIE_SPENTE:
                continue
            scheduler.add_job(_fn, "cron", day_of_week="mon-fri", hour=_ora, minute=_min,
                              timezone=ET_TZ, id=f"{_nome}_{_suf}")
    # Registro, resoconto e avvisi
    scheduler.add_job(job_valuta_registro, "cron", minute="8,38", id="registro_valuta")
    scheduler.add_job(job_resoconto, "cron", day_of_week="sat", hour=10, minute=7, id="resoconto")
    scheduler.add_job(job_salute_dati, "cron", minute="10,40", id="salute_dati")
    scheduler.add_job(job_calendario, "cron", hour=9, minute=5, id="calendario")
    scheduler.add_job(job_battito, "interval", minutes=5, id="battito",
                      next_run_time=datetime.now(ROME_TZ))
    scheduler.start()
    logger.info("Scheduler avviato")
except Exception as e:
    logger.error(f"Errore scheduler: {e}")

try:
    if stato is None:
        _nota = "⚠️ Memoria su file non disponibile (vedi log)"
    elif not stato.su_volume():
        _nota = "⚠️ Nessun volume /data: la memoria si perde a ogni deploy"
    elif posizioni_pb:
        _nota = "💾 Posizioni teoriche ripristinate: " + ", ".join(
            f"{PULLBACK[k][1]} (ingresso {v['entry_date']:%d/%m})" for k, v in posizioni_pb.items() if k in PULLBACK)
    else:
        _nota = "💾 Memoria ripristinata"
    if send_startup_message(sorted(STRATEGIE_SPENTE), _nota, sorted(STRATEGIE_OMBRA)):
        logger.info("Messaggio avvio inviato")
    else:
        logger.error("Messaggio avvio NON inviato (vedi errore Telegram sopra)")
except Exception as e:
    logger.error(f"Errore startup message: {e}")

try:
    job_calendario()          # avviso subito se il calendario è già in scadenza
except Exception as e:
    logger.error(f"Errore controllo calendario: {e}")

logger.info("Bot in ascolto...")
while True:
    try:
        time_module.sleep(60)
        if not scheduler_vivo():
            logger.error(f"Scheduler fermo da oltre {MAX_SILENZIO_MIN} minuti: riavvio il processo")
            try:
                send_testo("🔄 <b>Bot bloccato</b>: lo scheduler non risponde, riavvio automatico in corso.")
            finally:
                os._exit(1)       # Railway riavvia il servizio (restartPolicyType = "always")
    except Exception as e:
        logger.error(f"Loop error: {e}")
        time_module.sleep(10)
