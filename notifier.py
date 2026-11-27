"""NOTIFIER — Invio messaggi Telegram"""

import os, logging, requests
from datetime import datetime, time
import pytz

logger       = logging.getLogger(__name__)
ROME_TZ      = pytz.timezone("Europe/Rome")
ET_TZ        = pytz.timezone("America/New_York")
TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID      = os.environ.get("CHAT_ID", "")
API          = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

def _send(text: str) -> bool:
    if not TOKEN or not CHAT_ID:
        logger.error("TELEGRAM_TOKEN o CHAT_ID mancanti")
        return False
    try:
        r = requests.post(API, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        if r.status_code != 200:
            logger.error(f"Telegram HTTP {r.status_code}: {r.text[:300]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Telegram error: {e}"); return False

def _blocco_slittamento(signal: dict) -> str:
    """Confronta il prezzo attuale con l'entry del segnale ed evidenzia lo slittamento.
    Serve 'prezzo_attuale' nel segnale; se manca, il blocco viene omesso."""
    try:
        px    = float(signal["prezzo_attuale"])
        entry = float(signal["entry"])
        sl    = float(signal["sl"])
        tp    = float(signal["tp"])
    except (KeyError, TypeError, ValueError):
        return ""
    tp1 = None
    try:
        if signal.get("tp1"):
            tp1 = float(signal["tp1"])
    except ValueError:
        pass

    is_long = signal.get("direzione") == "LONG"
    rischio = abs(entry - sl)
    if rischio == 0:
        return ""

    slip    = px - entry                       # + = prezzo salito
    adverso = slip if is_long else -slip       # + = entrata peggiore di quella del segnale
    perc    = adverso / rischio * 100          # slittamento in % del rischio
    scaduto = (px <= sl or px >= tp) if is_long else (px >= sl or px <= tp)
    rischio_ora = abs(px - sl)
    rr_ora = abs(tp - px) / rischio_ora if rischio_ora > 0 else 0.0

    if scaduto or rr_ora < 1.0:
        verdetto = "⛔ <b>NON ENTRARE</b> — segnale scaduto"
    elif perc > 25:
        verdetto = "🔴 <b>Slittamento forte</b> — meglio aspettare"
    elif perc > 10:
        verdetto = "🟡 <b>Slittamento moderato</b>"
    else:
        verdetto = "🟢 <b>Slittamento trascurabile</b>"
    lato = "contro di te" if adverso > 0 else "a tuo favore"

    if scaduto:
        # Stop o target già superati: R:R e livelli ricalcolati non avrebbero senso
        return (
            f"⏱ <b>SLITTAMENTO:</b> {slip:+.2f} pt ({slip / entry * 100:+.2f}%) — {verdetto}\n"
            f"Prezzo ora <code>{px:.2f}</code> ha già superato lo stop o il target "
            f"(entry <code>{entry:.2f}</code>)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )

    d_sl = sl - entry
    d_tp = tp - entry
    ricalcolo = f"Stop <code>{px + d_sl:.2f}</code>"
    if tp1 is not None:
        ricalcolo += f" | TP1 <code>{px + (tp1 - entry):.2f}</code>"
    ricalcolo += f" | Target <code>{px + d_tp:.2f}</code>"

    return (
        f"⏱ <b>SLITTAMENTO:</b> {slip:+.2f} pt ({slip / entry * 100:+.2f}%) — {verdetto}\n"
        f"Prezzo ora <code>{px:.2f}</code> vs entry <code>{entry:.2f}</code> "
        f"({abs(perc):.0f}% del rischio {lato})\n"
        f"📐 R:R entrando ora: 1:{rr_ora:.1f} (stop e target del segnale)\n"
        f"🔁 Stesse distanze dal prezzo ora → {ricalcolo}\n"
        f"ℹ️ Prezzo Yahoo: confrontalo con quello su Revolut\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )


def _blocco_distanze(signal: dict) -> str:
    """Stop e target come DISTANZA dall'entry. Servono quando il prezzo del bot non
    coincide con quello di Revolut (es. oro: il bot legge il future, Revolut lo spot)."""
    try:
        entry = float(signal["entry"])
        sl    = float(signal["sl"])
        tp    = float(signal["tp"])
    except (KeyError, TypeError, ValueError):
        return ""
    is_long = signal.get("direzione") == "LONG"
    parti = [f"stop <code>{sl - entry:+.2f}</code>"]
    try:
        if signal.get("tp1"):
            parti.append(f"TP1 <code>{float(signal['tp1']) - entry:+.2f}</code>")
    except ValueError:
        pass
    parti.append(f"target <code>{tp - entry:+.2f}</code>")
    riga = f"📏 <b>Distanze dall'entry:</b> {' | '.join(parti)}\n"
    if signal.get("prezzi_future"):
        riga += (
            "⚠️ <b>Prezzi del future COMEX</b>, non dello spot di Revolut: possono differire "
            "di decine di $. <b>Su Revolut usa le distanze</b> dal tuo prezzo d'ingresso "
            f"(es. stop = ingresso {'−' if is_long else '+'} {abs(sl - entry):.2f}).\n"
        )
    return riga


def send_telegram(signal: dict) -> bool:
    now  = datetime.now(ROME_TZ).strftime("%d/%m %H:%M")
    chiudi = signal.get("chiudi_entro", "21:45")
    overnight = "⏰ <b>SWING — può restare overnight</b>" if "4H" in signal.get("timeframe","") or "1H" in signal.get("timeframe","") else f"⏱ <b>INTRADAY — chiudi entro le {chiudi} (ora italiana)</b>"

    # Orario della candela su cui si basa il segnale (es. "16:15–16:30 CET").
    # Se la strategia non lo fornisce, la riga viene omessa.
    candela = signal.get("candela")
    riga_candela = f"🕯 <b>Candela:</b> {candela}\n" if candela else ""

    msg = (
        f"<b>{signal['strategia']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Asset:</b> {signal['asset']}\n"
        f"⏱ <b>TF:</b> {signal['timeframe']}\n"
        f"{riga_candela}"
        f"🎯 <b>Entry:</b> <code>{signal['entry']}</code>\n"
        f"🛑 <b>Stop:</b>  <code>{signal['sl']}</code>\n"
        f"✅ <b>Target:</b> <code>{signal['tp']}</code>\n"
        f"📐 <b>R:R:</b> {signal['rr']}\n"
        f"{_blocco_distanze(signal)}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{_blocco_slittamento(signal)}"
        f"📝 {signal.get('note','')}\n\n"
        f"{overnight}\n"
        f"⚠️ <b>Rischia max 1.5% capitale</b>\n"
        f"🕐 Inviato {now}"
    )
    return _send(msg)

def _fmt_d(d):
    return d.strftime("%d/%m")


def _apertura_usa_it(d) -> str:
    """Ora italiana dell'apertura di Wall Street (09:30 ET) nel giorno d."""
    return ET_TZ.localize(datetime.combine(d, time(9, 30))).astimezone(ROME_TZ).strftime("%H:%M")


def send_pullback(m: dict) -> bool:
    """Messaggi del ritracciamento giornaliero: entrata, mantieni, uscita, stop, utili."""
    nome = m["nome"]
    tipo = m["tipo"]
    if tipo == "ENTRATA":
        eventi = m.get("eventi") or []
        riga_ev = f"⚠️ Domani in calendario: {', '.join(eventi)}\n" if eventi else ""
        msg = (
            f"📉 <b>RITRACCIAMENTO NEL TREND — {nome}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Segnale a fine seduta del {_fmt_d(m['data'])}\n"
            f"RSI(2) <b>{m['rsi2']:.1f}</b> (soglia 10) | chiusura <code>{m['close']:.2f}</code> "
            f"sopra la media a 200 giorni (<code>{m['sma200']:.2f}</code>)\n\n"
            f"➡️ <b>ENTRA LONG all'apertura USA del {_fmt_d(m['ingresso'])}</b> ({_apertura_usa_it(m['ingresso'])} italiane)\n"
            f"🛑 Stop: prezzo d'ingresso − <code>{m['stop_dist']:.2f}</code> (3×ATR). "
            f"Con ingresso a {m['close']:.2f} sarebbe ≈ <code>{m['close'] - m['stop_dist']:.2f}</code>\n"
            f"🎯 Uscita: quando la chiusura supera la media a 5 giorni (ora <code>{m['sma5']:.2f}</code>), "
            f"si vende all'apertura del giorno dopo. Massimo 10 giorni.\n"
            f"📏 Size = (capitale × % rischio) / {m['stop_dist']:.2f}\n"
            f"{riga_ev}"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"ℹ️ Strategia giornaliera, SOLO LONG, in prova (demo 4–8 settimane).\n"
            f"ℹ️ Il bot non conosce le tue posizioni: seguirà una posizione teorica (salvata anche dopo i riavvii) e ti avviserà ogni sera.\n"
            f"🕐 Inviato {datetime.now(ROME_TZ).strftime('%d/%m %H:%M')}"
        )
    elif tipo == "MANTIENI":
        msg = (
            f"📉 <b>{nome} — posizione teorica, giorno {m['giorni']} di 10</b>\n"
            f"Ingresso teorico <code>{m['entry']:.2f}</code> | stop teorico <code>{m['stop']:.2f}</code>\n"
            f"Chiusura {_fmt_d(m['data'])} <code>{m['close']:.2f}</code> ≤ media 5gg <code>{m['sma5']:.2f}</code> → <b>mantieni</b>\n"
            f"ℹ️ Se hai davvero aperto il CFD, tieni lo stop."
        )
    elif tipo in ("USCITA", "TEMPO"):
        perche = ("la chiusura ha superato la media a 5 giorni "
                  f"(<code>{m['close']:.2f}</code> > <code>{m['sma5']:.2f}</code>)") if tipo == "USCITA" \
                 else "sono passati 10 giorni"
        quando = "domani all'apertura USA" if m.get("recente", True) else "al più presto (l'evento è di un giorno fa)"
        msg = (
            f"✅ <b>{nome} — ESCI {quando}</b>\n"
            f"Chiusura {_fmt_d(m['data'])}: {perche}. Giorno {m['giorni']}.\n"
            f"Ingresso teorico <code>{m['entry']:.2f}</code>.\n"
            f"ℹ️ Posizione teorica chiusa: aspetta il prossimo segnale."
        )
    elif tipo == "STOP":
        msg = (
            f"🛑 <b>{nome} — stop teorico toccato</b>\n"
            f"Il {_fmt_d(m['data'])} il prezzo è sceso sotto <code>{m['stop']:.2f}</code> "
            f"(ingresso teorico <code>{m['entry']:.2f}</code>).\n"
            f"Se hai uno stop sul CFD dovrebbe essere già scattato: controlla l'app. "
            f"Posizione teorica chiusa."
        )
    elif tipo == "SALTATO_UTILI":
        msg = (
            f"⏭ <b>{nome}</b>: segnale del {_fmt_d(m['data'])} SALTATO\n"
            f"L'ingresso cadrebbe il {_fmt_d(m['ingresso'])}, giorno dei risultati trimestrali: "
            f"gap e volatilità anomali non coperti dallo stop."
        )
    else:
        return False
    return _send(msg)


def send_startup_message(spente=None, nota_stato=""):
    now = datetime.now(ROME_TZ).strftime("%d/%m/%Y %H:%M")
    riga_spente = f"⏸ <b>Spente:</b> {', '.join(spente)}\n" if spente else ""
    riga_stato = f"{nota_stato}\n" if nota_stato else ""
    msg = (
        f"🤖 <b>Trading Bot — Avviato</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Strategie attive:</b>\n"
        f"1️⃣ VWAP Reversal Oro 15min (PF 1.84)\n"
        f"2️⃣ London Sweep Oro 08-10 CET (PF 1.55)\n"
        f"3️⃣ ORB S&amp;P500 15min (PF 1.40)\n"
        f"4️⃣ Kumo Rider Oro 4H swing (PF 1.62)\n"
        f"5️⃣ Kumo Rider Nasdaq 1H swing (PF 1.51)\n"
        f"6️⃣ ORB Anthropic ANT 15min (sperimentale, senza backtest)\n"
        f"7️⃣ ORB Caterpillar CAT 15min (non validata)\n"
        f"8️⃣ ORB Broadcom AVGO 15min (non validata)\n"
        f"9️⃣ Ritracciamento S&amp;P500 giornaliero (solo long, demo)\n"
        f"🔟 Ritracciamento Broadcom AVGO giornaliero (solo long, demo)\n"
        f"{riga_spente}"
        f"{riga_stato}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Max 1.5% per trade | Max 4% totale\n"
        f"📅 Controlla ForexFactory ogni mattina\n"
        f"🕐 {now}"
    )
    return _send(msg)

