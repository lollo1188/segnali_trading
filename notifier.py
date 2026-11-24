"""NOTIFIER — Invio messaggi Telegram"""

import os, logging, requests
from datetime import datetime
import pytz

logger       = logging.getLogger(__name__)
ROME_TZ      = pytz.timezone("Europe/Rome")
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


def send_telegram(signal: dict) -> bool:
    now  = datetime.now(ROME_TZ).strftime("%d/%m %H:%M")
    tipo = "🟢 LONG" if signal.get("direzione") == "LONG" else "🔴 SHORT"
    overnight = "⏰ <b>SWING — può restare overnight</b>" if "4H" in signal.get("timeframe","") or "1H" in signal.get("timeframe","") else "⏱ <b>INTRADAY — chiudi entro 21:45 CET</b>"

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
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{_blocco_slittamento(signal)}"
        f"📝 {signal.get('note','')}\n\n"
        f"{overnight}\n"
        f"⚠️ <b>Rischia max 1.5% capitale</b>\n"
        f"🕐 Inviato {now}"
    )
    return _send(msg)

def send_startup_message():
    now = datetime.now(ROME_TZ).strftime("%d/%m/%Y %H:%M")
    msg = (
        f"🤖 <b>Trading Bot — Avviato</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Strategie attive:</b>\n"
        f"1️⃣ VWAP Reversal Oro 15min (PF 1.84)\n"
        f"2️⃣ London Sweep Oro 08-10 CET (PF 1.55)\n"
        f"3️⃣ ORB S&amp;P500 15min (PF 1.40)\n"
        f"4️⃣ Kumo Rider Oro 4H swing (PF 1.62)\n"
        f"5️⃣ Kumo Rider Nasdaq 1H swing (PF 1.51)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Max 1.5% per trade | Max 4% totale\n"
        f"📅 Controlla ForexFactory ogni mattina\n"
        f"🕐 {now}"
    )
    return _send(msg)

