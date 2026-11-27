import os, logging, threading, time as time_module
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, time, timedelta
import pytz

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ROME_TZ = pytz.timezone("Europe/Rome")


# ── Health server — avvia PRIMA di qualsiasi import pesante ──────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")

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
        get_last_price, fmt, in_orario_et, ET_TZ, TICKER_GOLD, TICKER_SP500, TICKER_NASDAQ,
        TICKER_ANT, TICKER_CAT, TICKER_AVGO,
    )
    logger.info("Strategies importate OK")
except Exception as e:
    logger.error(f"Errore import strategies: {e}")

try:
    from notifier import send_telegram, send_startup_message, send_pullback
    logger.info("Notifier importato OK")
except Exception as e:
    logger.error(f"Errore import notifier: {e}")

try:
    from news_filter import is_news_window, prossimo_sblocco, motivo_blocco
    logger.info("News filter importato OK")
except Exception as e:
    logger.error(f"Errore import news_filter: {e}")
    def is_news_window(name=None): return False
    def prossimo_sblocco(name=None): return None
    def motivo_blocco(name=None): return None


try:
    import stato
    _STATO = stato.carica()
    logger.info(f"Stato salvato in {stato.percorso()}"
                + ("" if stato.su_volume() else " — ⚠️ nessun volume /data: si perde a ogni deploy"))
except Exception as e:
    logger.error(f"Errore import stato: {e}")
    stato = None
    _STATO = {"last_signals": {}, "posizioni_pb": {}, "ultimo_pb": {}}


# ── Logica bot ────────────────────────────────────────────────────────────────
# Memoria del bot: caricata dal file di stato e salvata a ogni modifica.
last_signals: dict = _STATO["last_signals"]


def salva_stato():
    if stato is not None:
        stato.salva(last_signals, posizioni_pb, ultimo_pb)

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


def _leggi_spente():
    grezzo = os.environ.get("STRATEGIE_SPENTE", "")
    nomi = {x.strip().lower() for x in grezzo.replace(";", ",").replace(" ", ",").split(",") if x.strip()}
    sconosciute = nomi - set(NOMI_STRATEGIE)
    if sconosciute:
        logger.warning(f"STRATEGIE_SPENTE: nomi non riconosciuti (ignorati): {sorted(sconosciute)}. "
                       f"Nomi validi: {NOMI_STRATEGIE}")
    return nomi & set(NOMI_STRATEGIE)


STRATEGIE_SPENTE = _leggi_spente()
if STRATEGIE_SPENTE:
    logger.info(f"Strategie SPENTE da STRATEGIE_SPENTE: {sorted(STRATEGIE_SPENTE)}")


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


def run_check(name, fn, cooldown_h=4):
    if name in STRATEGIE_SPENTE:
        return
    try:
        if is_news_window(name):
            logger.info(f"[{name}] Filtro notizie attivo: {motivo_blocco(name)}")
            _pianifica_ripetizione(name, fn, cooldown_h)
            return
        signal = fn()
        if signal and should_send(name, signal["direzione"], cooldown_h):
            # Prezzo attuale al momento dell'invio → il messaggio mostra lo slittamento.
            # Se non disponibile, il messaggio parte senza il blocco slittamento.
            try:
                px = get_last_price(SIGNAL_TICKER.get(name, ""))
                if px:
                    signal["prezzo_attuale"] = fmt(px)
            except Exception as e:
                logger.warning(f"[{name}] Prezzo attuale non disponibile: {type(e).__name__}")
            if send_telegram(signal):
                logger.info(f"[{name}] Segnale inviato: {signal['direzione']}")
                register(name, signal["direzione"])
            else:
                logger.error(f"[{name}] Invio Telegram fallito")
    except Exception as e:
        logger.error(f"[{name}] Errore: {e}")


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
    if not in_orario_et(time(9, 30), time(15, 0)): return       # 15:30–21:00 italiane
    logger.info("▶ Kumo Rider Nasdaq 1H")
    run_check("kumo_nasdaq", check_kumo_nasdaq_1h, 8)


# ── Main ──────────────────────────────────────────────────────────────────────
logger.info("═══════════════════════════════════════")
logger.info(f" TRADING BOT — {len(NOMI_STRATEGIE) - len(STRATEGIE_SPENTE)} strategie attive su {len(NOMI_STRATEGIE)}")
logger.info("═══════════════════════════════════════")

try:
    scheduler = BackgroundScheduler(timezone=ROME_TZ)
    scheduler.add_listener(lambda e: logger.error(f"Job error: {e.exception}"), EVENT_JOB_ERROR)
    def _aggiungi_job(funzione, nome, trigger, **kwargs):
        if nome in STRATEGIE_SPENTE:
            logger.info(f"[{nome}] SPENTA: job non avviato")
            return
        scheduler.add_job(funzione, trigger, id=nome, **kwargs)

    _aggiungi_job(job_vwap_gold, "vwap_gold", "interval", minutes=15)
    _aggiungi_job(job_london_sweep, "london_sweep", "interval", minutes=15)
    _aggiungi_job(job_orb_sp500, "orb_sp500", "interval", minutes=15)
    _aggiungi_job(job_orb_ant, "orb_ant", "interval", minutes=15)
    _aggiungi_job(job_orb_cat, "orb_cat", "interval", minutes=15)
    _aggiungi_job(job_orb_avgo, "orb_avgo", "interval", minutes=15)
    # Ogni ora ai :20: le candele 4H sono allineate in UTC, quindi in ora italiana si
    # chiudono a orari diversi tra ora solare e legale. Controllando ogni ora il segnale
    # arriva sempre ~20 minuti dopo la chiusura (la pausa di 12h evita doppioni).
    _aggiungi_job(job_kumo_gold_4h, "kumo_gold_4h", "cron", minute=20)
    _aggiungi_job(job_kumo_nasdaq, "kumo_nasdaq", "cron", minute=5)
    # Ritracciamento giornaliero: dopo la chiusura USA, in ora di New York (16:10 e 16:40 ET,
    # di solito 22:10 e 22:40 italiane). Due controlli: se Yahoo è in ritardo il secondo recupera.
    for _ora, _min, _suf in ((16, 10, "a"), (16, 40, "b")):
        for _nome, _fn in (("pullback_sp500", job_pullback_sp500), ("pullback_avgo", job_pullback_avgo)):
            if _nome in STRATEGIE_SPENTE:
                continue
            scheduler.add_job(_fn, "cron", day_of_week="mon-fri", hour=_ora, minute=_min,
                              timezone=ET_TZ, id=f"{_nome}_{_suf}")
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
    if send_startup_message(sorted(STRATEGIE_SPENTE), _nota):
        logger.info("Messaggio avvio inviato")
    else:
        logger.error("Messaggio avvio NON inviato (vedi errore Telegram sopra)")
except Exception as e:
    logger.error(f"Errore startup message: {e}")

logger.info("Bot in ascolto...")
while True:
    try:
        time_module.sleep(60)
    except Exception as e:
        logger.error(f"Loop error: {e}")
        time_module.sleep(10)
