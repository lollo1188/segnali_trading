"""
STRATEGIE DI TRADING
1. VWAP Reversal Oro 15min — intraday (backtest 60 giorni: PF 1,71 su 10 trade, campione troppo piccolo)
2. London Liquidity Sweep Oro — intraday (backtest 60 giorni: PF 0,69 su 8 trade)
3. ORB S&P500 15min — intraday (backtest 60 giorni: PF 1,40 su 59 trade, campione piccolo)
4. Kumo Rider Oro 4H — swing overnight OK (backtest 2 anni: PF 0,83 su 10 trade)
5. Kumo Rider Nasdaq 1H — swing overnight OK (backtest 2 anni: 5 trade, tutti in perdita)
6. ORB Anthropic ANT 15min — intraday (SPERIMENTALE: storico troppo breve per un backtest)
7. ORB Caterpillar CAT 15min — intraday (non validata: usa backtest.py azioni)
8. ORB Broadcom AVGO 15min — intraday (non validata: usa backtest.py azioni)
9-10. Ritracciamento nel trend giornaliero S&P 500 e AVGO — solo long
     (backtest: S&P 500 PF ≈1,43, AVGO PF 1,39–1,77; vantaggio piccolo)

I PF scritti in passato per le strategie 1-5 (1.84, 1.55, 1.40, 1.62, 1.51) non
erano confermati da nessuno script del repository e sono stati tolti. Risultati
attuali: `python3 backtest.py bot` (1, 2, 4, 5) e `python3 backtest.py orb` (3).
`python3 verifica_backtest.py` controlla che il bot e il backtest diano gli stessi segnali.

Orari: le strategie USA (ORB, Kumo Nasdaq, ritracciamento) usano l'ORA DI NEW YORK,
così restano agganciate all'apertura di Wall Street anche nelle settimane in cui
l'ora legale USA ed europea non coincidono (marzo e fine ottobre/inizio novembre).
Le strategie sull'oro restano in ora italiana (sessione europea).
"""
import logging
import os
from datetime import datetime, time, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
import pytz

logger = logging.getLogger(__name__)

ROME_TZ = pytz.timezone("Europe/Rome")
ET_TZ = pytz.timezone("America/New_York")

TICKER_GOLD = "GC=F"   # future COMEX: su Revolut l'oro è SPOT, i prezzi differiscono (vedi notifier)
TICKER_SP500 = "^GSPC"
TICKER_NASDAQ = "^NDX"
TICKER_ANT = os.environ.get("TICKER_ANT", "ANT")     # Anthropic, Nasdaq (da giovedì 19/11/2026)
TICKER_CAT = os.environ.get("TICKER_CAT", "CAT")     # Caterpillar, NYSE
TICKER_AVGO = os.environ.get("TICKER_AVGO", "AVGO")  # Broadcom, Nasdaq


# ── Orari di New York ─────────────────────────────────────────────────────────
APERTURA_USA_ET = time(9, 30)      # prima candela 15 min = opening range
CHIUSURA_INTRADAY_ET = time(15, 45)  # chiudi le intraday USA 15 min prima della chiusura


def adesso_et() -> datetime:
    return datetime.now(ET_TZ)


def in_orario_et(inizio: time, fine: time, ora: Optional[datetime] = None) -> bool:
    """True se l'ora di New York (adesso, o `ora`) è tra inizio e fine compresi."""
    t = (ora.astimezone(ET_TZ) if ora else adesso_et()).time()
    return inizio <= t <= fine


def ora_it(t_et: time, giorno=None) -> str:
    """Ora di New York -> 'HH:MM' italiana per il giorno indicato (default oggi)."""
    d = giorno or adesso_et().date()
    return ET_TZ.localize(datetime.combine(d, t_et)).astimezone(ROME_TZ).strftime("%H:%M")


def _vwap_giornaliero_et(df: pd.DataFrame) -> pd.Series:
    """VWAP che riparte ogni giorno di borsa USA (data di New York)."""
    giorni = df.index.tz_convert(ET_TZ).date
    parts = []
    for d in pd.unique(giorni):
        g = df[giorni == d]
        tp = (g["High"] + g["Low"] + g["Close"]) / 3
        parts.append((tp * g["Volume"]).cumsum() / g["Volume"].cumsum())
    return pd.concat(parts).reindex(df.index)


# ── Download dati ─────────────────────────────────────────────────────────────
# Salute dei dati: per ogni ticker, download falliti di fila e ultimo download riuscito.
# main.py la controlla e avvisa su Telegram se Yahoo non risponde.
SALUTE_DATI: dict = {}


def _segna_download(ticker: str, ok: bool, errore: str = "") -> None:
    s = SALUTE_DATI.setdefault(ticker, {"fallimenti": 0, "ultimo_ok": None, "errore": ""})
    if ok:
        s["fallimenti"] = 0
        s["ultimo_ok"] = datetime.now(ROME_TZ)
    else:
        s["fallimenti"] += 1
        s["errore"] = errore


def get_data(ticker: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    try:
        t = yf.Ticker(ticker)
        df = t.history(period=period, interval=interval, auto_adjust=True)
        if df is None or len(df) < 30:
            logger.warning(f"Dati insufficienti per {ticker}")
            _segna_download(ticker, False, "dati insufficienti")
            return None
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(ROME_TZ)
        _segna_download(ticker, True)
        return df.dropna()
    except Exception as e:
        logger.error(f"Errore download {ticker}: {e}")
        _segna_download(ticker, False, f"{type(e).__name__}: {e}"[:200])
        return None


def get_last_price(ticker: str) -> Optional[float]:
    """Ultimo prezzo disponibile (candele da 1 minuto, poi 5), per misurare lo
    slittamento rispetto all'entry del segnale. None se non disponibile."""
    for interval in ("1m", "5m"):
        try:
            df = yf.Ticker(ticker).history(period="1d", interval=interval, auto_adjust=True)
            if df is not None and len(df) > 0:
                return float(df["Close"].dropna().iloc[-1])
        except Exception as e:
            logger.warning(f"Prezzo attuale {ticker} ({interval}): {type(e).__name__}")
    return None


# ── Indicatori ────────────────────────────────────────────────────────────────

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(span=n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(span=n, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def calc_vwap_bands(df: pd.DataFrame):
    """VWAP con bande ±2σ, reset giornaliero."""
    df = df.copy()
    df["_d"] = df.index.date
    vwap_s, b2u_s, b2d_s = [], [], []
    for d, g in df.groupby("_d"):
        tp = (g["High"] + g["Low"] + g["Close"]) / 3
        v = (tp * g["Volume"]).cumsum() / g["Volume"].cumsum()
        var = ((tp - v) ** 2 * g["Volume"]).cumsum() / g["Volume"].cumsum()
        std = np.sqrt(var)
        vwap_s.append(v)
        b2u_s.append(v + 2 * std)
        b2d_s.append(v - 2 * std)
    vwap = pd.concat(vwap_s).reindex(df.index)
    b2u = pd.concat(b2u_s).reindex(df.index)
    b2d = pd.concat(b2d_s).reindex(df.index)
    return vwap, b2u, b2d


def ichimoku(df: pd.DataFrame, tenkan=9, kijun=26, senkou_b=52, disp=26):
    h, l = df["High"], df["Low"]
    t_sen = (h.rolling(tenkan).max() + l.rolling(tenkan).min()) / 2
    k_sen = (h.rolling(kijun).max() + l.rolling(kijun).min()) / 2
    s_a = ((t_sen + k_sen) / 2).shift(disp)
    s_b = ((h.rolling(senkou_b).max() + l.rolling(senkou_b).min()) / 2).shift(disp)
    chikou = df["Close"].shift(-disp)   # ⚠️ guarda nel futuro: non usarlo per i segnali
    return t_sen, k_sen, s_a, s_b, chikou


def resample_4h(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("4h").agg({
        "Open": "first", "High": "max",
        "Low": "min", "Close": "last", "Volume": "sum"
    }).dropna()


def fmt(value, decimals: int = 2) -> str:
    try:
        return f"{float(value):.{decimals}f}"
    except Exception:
        return str(value)


def chiusura_candela(ts, minutes: int) -> datetime:
    """Orario di chiusura della candela (per il registro dei segnali)."""
    return (ts + pd.Timedelta(minutes=minutes)).tz_convert(ROME_TZ).to_pydatetime()


def candle_label(ts, minutes: int) -> str:
    """Orario CET della candela su cui si basa il segnale, es. '16:15–16:30 CET'.
    ts = timestamp di APERTURA della candela (curr.name), minutes = durata."""
    start = ts.tz_convert(ROME_TZ)
    end = start + pd.Timedelta(minutes=minutes)
    return f"{start:%H:%M}–{end:%H:%M} CET"


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIA 1 — VWAP REVERSAL ORO 15min (backtest: PF 1,71 su 10 trade, non dimostrata)
# Intraday — chiusura entro 21:45 CET
# Prezzo esteso oltre +2σ/−2σ VWAP + RSI estremo + candela di inversione
# ══════════════════════════════════════════════════════════════════════════════

def check_vwap_reversal_gold() -> Optional[dict]:
    df = get_data(TICKER_GOLD, period="5d", interval="15m")
    if df is None or len(df) < 40:
        return None

    df["rsi14"] = rsi(df["Close"], 14)
    df["atr14"] = atr(df, 14)
    df["vwap"], df["vb2u"], df["vb2d"] = calc_vwap_bands(df)
    df = df.dropna()
    if len(df) < 3:
        return None

    # Usa [-2] = ultima candela CHIUSA, [-1] è ancora in formazione.
    # Ultima candela valutata: 21:15 (si chiude alle 21:30), così il segnale arriva
    # prima delle 21:45, l'ora di chiusura delle intraday.
    last_cet = df.index[-2].tz_convert(ROME_TZ)
    if not (time(8, 0) <= last_cet.time() <= time(21, 15)):
        return None

    curr = df.iloc[-2]
    prev = df.iloc[-3]
    entry = curr["Close"]
    a = curr["atr14"]

    # LONG: prezzo sotto -2σ + RSI < 35 + candela verde
    if (entry < curr["vb2d"] and curr["rsi14"] < 35 and
            curr["Close"] > prev["Close"]):
        sl = entry - 1.2 * a
        tp = curr["vwap"]
        if (tp - entry) < 1.5 * (entry - sl):
            return None
        return {
            "strategia": "🟡 VWAP REVERSAL ORO",
            "asset": "Oro (future COMEX GC)",
            "prezzi_future": True,
            "timeframe": "15min",
            "direzione": "LONG",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(tp),
            "rr": "~1.8:1",
            "candela": candle_label(curr.name, 15),
            "candela_fine": chiusura_candela(curr.name, 15),
            "note": f"Prezzo sotto VWAP -2σ | RSI {curr['rsi14']:.0f} | Rimbalzo iniziato | Chiudi entro 21:45"
        }

    # SHORT: prezzo sopra +2σ + RSI > 65 + candela rossa
    if (entry > curr["vb2u"] and curr["rsi14"] > 65 and
            curr["Close"] < prev["Close"]):
        sl = entry + 1.2 * a
        tp = curr["vwap"]
        if (entry - tp) < 1.5 * (sl - entry):
            return None
        return {
            "strategia": "🔴 VWAP REVERSAL ORO",
            "asset": "Oro (future COMEX GC)",
            "prezzi_future": True,
            "timeframe": "15min",
            "direzione": "SHORT",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(tp),
            "rr": "~1.8:1",
            "candela": candle_label(curr.name, 15),
            "candela_fine": chiusura_candela(curr.name, 15),
            "note": f"Prezzo sopra VWAP +2σ | RSI {curr['rsi14']:.0f} | Inversione | Chiudi entro 21:45"
        }

    return None


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIA 2 — LONDON LIQUIDITY SWEEP ORO (backtest: PF 0,69 su 8 trade)
# Intraday — finestra 08:00-10:00 CET
# Falso breakdown del range asiatico 06:00-08:00 → entrata LONG
# ══════════════════════════════════════════════════════════════════════════════

def check_london_sweep_gold() -> Optional[dict]:
    df = get_data(TICKER_GOLD, period="3d", interval="15m")
    if df is None or len(df) < 20:
        return None

    df["sma20"] = sma(df["Close"], 20)
    df["atr14"] = atr(df, 14)
    df = df.dropna()

    now_cet = datetime.now(ROME_TZ)
    if not (time(8, 0) <= now_cet.time() <= time(10, 0)):
        return None

    today = now_cet.date()
    today_df = df[df.index.date == today]

    # Range asiatico 06:00-08:00 CET
    asia = today_df[(today_df.index.time >= time(6, 0)) &
                    (today_df.index.time < time(8, 0))]
    if len(asia) < 3:
        return None

    asia_low = asia["Low"].min()
    asia_high = asia["High"].max()

    # Ultime 2 candele nella finestra operativa
    window = today_df[(today_df.index.time >= time(8, 0)) &
                      (today_df.index.time <= now_cet.time())]
    if len(window) < 3:
        return None

    # Usa [-2] = ultima candela chiusa, [-1] è ancora in formazione
    curr = window.iloc[-2]

    # Sweep: candela corrente ha toccato sotto asia_low di $2-$5
    sweep_depth = asia_low - curr["Low"]
    if not (2.0 <= sweep_depth <= 5.0):
        return None

    # Reclaim: chiude sopra asia_low
    if curr["Close"] <= asia_low:
        return None

    # SMA20 piatta o in discesa (trapped shorts)
    sma_slope = df["sma20"].iloc[-2] - df["sma20"].iloc[-5]
    if sma_slope > 0.5:
        return None

    entry = curr["Close"]
    sl = curr["Low"] - 3.0
    tp = asia_high
    risk = entry - sl
    if risk <= 0 or risk > 15:
        return None

    return {
        "strategia": "🟡 LONDON SWEEP ORO",
        "asset": "Oro (future COMEX GC)",
        "prezzi_future": True,
        "timeframe": "15min",
        "direzione": "LONG",
        "entry": fmt(entry),
        "sl": fmt(sl),
        "tp": fmt(tp),
        "rr": f"1:{(tp-entry)/risk:.1f}",
        "candela": candle_label(curr.name, 15),
        "candela_fine": chiusura_candela(curr.name, 15),
        "note": f"Sweep ${sweep_depth:.2f} sotto range asiatico | Reclaim | TP1=50% a {fmt(tp)} | Trailing $4 dopo TP1"
    }


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIA 3 — ORB S&P500 15min (backtest 60 giorni: PF 1,40 su 59 trade, non dimostrata)
# Intraday — OR = prima candela 09:30 ET (15:30 italiane), ingressi 09:45-15:45 ET
# ══════════════════════════════════════════════════════════════════════════════

def check_orb_sp500() -> Optional[dict]:
    df = get_data(TICKER_SP500, period="5d", interval="15m")
    if df is None or len(df) < 40:
        return None

    df["atr14"] = atr(df, 14)
    df["rsi14"] = rsi(df["Close"], 14)
    df["ema20"] = ema(df["Close"], 20)

    # VWAP giornaliero (giorno di borsa USA)
    df["vwap"] = _vwap_giornaliero_et(df)
    df = df.dropna()

    now_et = adesso_et()
    if not in_orario_et(time(9, 45), CHIUSURA_INTRADAY_ET, now_et):
        return None

    idx_et = df.index.tz_convert(ET_TZ)
    today_df = df[idx_et.date == now_et.date()]

    # Opening Range: prima candela 09:30 ET
    or_bar = today_df[today_df.index.tz_convert(ET_TZ).time == APERTURA_USA_ET]
    if len(or_bar) == 0:
        return None

    or_high = or_bar["High"].iloc[0]
    or_low = or_bar["Low"].iloc[0]
    or_range = or_high - or_low

    # Salta se OR troppo ampio
    atr_day = today_df["atr14"].mean()
    if or_range > 2.5 * atr_day:
        return None

    # Usa [-2] = ultima candela chiusa, [-1] è ancora in formazione
    curr = df.iloc[-2]
    prev = df.iloc[-3]
    entry = curr["Close"]
    a = curr["atr14"]
    chiudi = ora_it(CHIUSURA_INTRADAY_ET)

    # LONG: breakout sopra OR high + sopra VWAP + EMA20 crescente + RSI > 52
    if (entry > or_high + 0.1 * a and
            entry > curr["vwap"] and
            curr["ema20"] > prev["ema20"] and
            curr["rsi14"] > 52):
        sl = max(or_low, entry - 2.0 * a)
        risk = entry - sl
        if risk <= 0:
            return None
        tp1 = entry + 1.5 * risk
        tp2 = entry + 2.5 * risk
        return {
            "strategia": "🟢 ORB S&P500",
            "asset": "S&P 500",
            "timeframe": "15min",
            "direzione": "LONG",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(tp2),
            "rr": "1:2.5",
            "candela": candle_label(curr.name, 15),
            "candela_fine": chiusura_candela(curr.name, 15),
            "tp1": fmt(tp1),
            "chiudi_entro": chiudi,
            "note": f"Breakout OR high {fmt(or_high)} | Sopra VWAP | TP1 {fmt(tp1)} → sposta SL a BE | Chiudi entro {chiudi}"
        }

    # SHORT: breakout sotto OR low + sotto VWAP + EMA20 decrescente + RSI < 48
    if (entry < or_low - 0.1 * a and
            entry < curr["vwap"] and
            curr["ema20"] < prev["ema20"] and
            curr["rsi14"] < 48):
        sl = min(or_high, entry + 2.0 * a)
        risk = sl - entry
        if risk <= 0:
            return None
        tp1 = entry - 1.5 * risk
        tp2 = entry - 2.5 * risk
        return {
            "strategia": "🔴 ORB S&P500",
            "asset": "S&P 500",
            "timeframe": "15min",
            "direzione": "SHORT",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(tp2),
            "rr": "1:2.5",
            "candela": candle_label(curr.name, 15),
            "candela_fine": chiusura_candela(curr.name, 15),
            "tp1": fmt(tp1),
            "chiudi_entro": chiudi,
            "note": f"Breakout sotto OR low {fmt(or_low)} | Sotto VWAP | TP1 {fmt(tp1)} → sposta SL a BE | Chiudi entro {chiudi}"
        }

    return None


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIA 4 — KUMO RIDER ORO 4H (backtest: PF 0,83 su 10 trade)
# Swing — overnight OK se segnale forte
# Ichimoku 9/26/52 + RSI 40-65
# ══════════════════════════════════════════════════════════════════════════════

def check_kumo_gold_4h() -> Optional[dict]:
    df_1h = get_data(TICKER_GOLD, period="2y", interval="1h")
    if df_1h is None or len(df_1h) < 100:
        return None

    df = resample_4h(df_1h)
    if len(df) < 60:
        return None

    t_sen, k_sen, s_a, s_b, _ = ichimoku(df)
    df["t_sen"] = t_sen
    df["k_sen"] = k_sen
    df["s_a"] = s_a
    df["s_b"] = s_b
    # Chikou (regola standard): chiusura attuale contro quella di 26 candele prima.
    # NON usare il chikou "spostato nel futuro" (Close.shift(-26)): per le candele
    # recenti è vuoto, dropna() le scarta e il bot valuterebbe una candela di 27
    # periodi fa (errore corretto il 27/11/2026).
    df["c_26"] = df["Close"].shift(26)
    df["rsi14"] = rsi(df["Close"], 14)
    df["atr14"] = atr(df, 14)
    df = df.dropna()
    if len(df) < 4:
        return None

    # Usa [-2] = ultima candela 4H chiusa, [-1] è ancora in formazione
    curr = df.iloc[-2]
    prev = df.iloc[-3]

    kumo_top = max(curr["s_a"], curr["s_b"])
    kumo_bot = min(curr["s_a"], curr["s_b"])
    cross_up = prev["t_sen"] <= prev["k_sen"] and curr["t_sen"] > curr["k_sen"]
    cross_dn = prev["t_sen"] >= prev["k_sen"] and curr["t_sen"] < curr["k_sen"]
    entry = curr["Close"]
    a = curr["atr14"]

    if (cross_up and entry > kumo_top and
            40 < curr["rsi14"] < 65 and
            entry > curr["c_26"] and
            curr["t_sen"] > curr["s_a"] and curr["t_sen"] > curr["s_b"]):
        sl = curr["k_sen"] - 0.3 * a
        risk = entry - sl
        if risk <= 0:
            return None
        return {
            "strategia": "🟡 KUMO RIDER ORO 4H",
            "asset": "Oro (future COMEX GC)",
            "prezzi_future": True,
            "timeframe": "4H (swing)",
            "direzione": "LONG",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(entry + 2.0 * risk),
            "rr": "1:2",
            "candela": candle_label(curr.name, 240),
            "candela_fine": chiusura_candela(curr.name, 240),
            "note": f"Tenkan cross Kijun | Sopra nuvola | RSI {curr['rsi14']:.0f} | ⚠️ Overnight OK se R:R ≥ 1.5 già raggiunto"
        }

    if (cross_dn and entry < kumo_bot and
            35 < curr["rsi14"] < 60 and
            entry < curr["c_26"] and
            curr["t_sen"] < curr["s_a"] and curr["t_sen"] < curr["s_b"]):
        sl = curr["k_sen"] + 0.3 * a
        risk = sl - entry
        if risk <= 0:
            return None
        return {
            "strategia": "🔴 KUMO RIDER ORO 4H",
            "asset": "Oro (future COMEX GC)",
            "prezzi_future": True,
            "timeframe": "4H (swing)",
            "direzione": "SHORT",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(entry - 2.0 * risk),
            "rr": "1:2",
            "candela": candle_label(curr.name, 240),
            "candela_fine": chiusura_candela(curr.name, 240),
            "note": f"Tenkan cross Kijun al ribasso | Sotto nuvola | RSI {curr['rsi14']:.0f} | ⚠️ Overnight OK se R:R ≥ 1.5"
        }

    return None


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIA 5 — KUMO RIDER NASDAQ 1H (backtest: 5 trade, tutti in perdita)
# Swing — overnight OK se segnale forte
# Ichimoku 9/26/52 + RSI 40-65 + filtro orario 09:30-15:00 ET (15:30-21:00 italiane)
# ══════════════════════════════════════════════════════════════════════════════

def check_kumo_nasdaq_1h() -> Optional[dict]:
    df = get_data(TICKER_NASDAQ, period="2y", interval="1h")
    if df is None or len(df) < 60:
        return None

    t_sen, k_sen, s_a, s_b, _ = ichimoku(df)
    df["t_sen"] = t_sen
    df["k_sen"] = k_sen
    df["s_a"] = s_a
    df["s_b"] = s_b
    # Chikou (regola standard): chiusura attuale contro quella di 26 candele prima.
    # NON usare il chikou "spostato nel futuro" (Close.shift(-26)): per le candele
    # recenti è vuoto, dropna() le scarta e il bot valuterebbe una candela di 27
    # periodi fa (errore corretto il 27/11/2026).
    df["c_26"] = df["Close"].shift(26)
    df["rsi14"] = rsi(df["Close"], 14)
    df["atr14"] = atr(df, 14)
    df = df.dropna()
    if len(df) < 4:
        return None

    # Usa [-2] = ultima candela 1H chiusa, [-1] è ancora in formazione
    if not in_orario_et(APERTURA_USA_ET, time(15, 0), df.index[-2]):
        return None

    curr = df.iloc[-2]
    prev = df.iloc[-3]

    kumo_top = max(curr["s_a"], curr["s_b"])
    kumo_bot = min(curr["s_a"], curr["s_b"])
    cross_up = prev["t_sen"] <= prev["k_sen"] and curr["t_sen"] > curr["k_sen"]
    cross_dn = prev["t_sen"] >= prev["k_sen"] and curr["t_sen"] < curr["k_sen"]
    entry = curr["Close"]
    a = curr["atr14"]

    if (cross_up and entry > kumo_top and
            40 < curr["rsi14"] < 65 and
            entry > curr["c_26"] and
            curr["t_sen"] > curr["s_a"] and curr["t_sen"] > curr["s_b"]):
        sl = curr["k_sen"] - 0.3 * a
        risk = entry - sl
        if risk <= 0:
            return None
        return {
            "strategia": "🔵 KUMO RIDER NASDAQ 1H",
            "asset": "Nasdaq 100",
            "timeframe": "1H (swing)",
            "direzione": "LONG",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(entry + 2.0 * risk),
            "rr": "1:2",
            "candela": candle_label(curr.name, 60),
            "candela_fine": chiusura_candela(curr.name, 60),
            "note": f"Tenkan cross Kijun | Sopra nuvola | RSI {curr['rsi14']:.0f} | ⚠️ Overnight OK se R:R ≥ 1.5"
        }

    if (cross_dn and entry < kumo_bot and
            35 < curr["rsi14"] < 60 and
            entry < curr["c_26"] and
            curr["t_sen"] < curr["s_a"] and curr["t_sen"] < curr["s_b"]):
        sl = curr["k_sen"] + 0.3 * a
        risk = sl - entry
        if risk <= 0:
            return None
        return {
            "strategia": "🔴 KUMO RIDER NASDAQ 1H",
            "asset": "Nasdaq 100",
            "timeframe": "1H (swing)",
            "direzione": "SHORT",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(entry - 2.0 * risk),
            "rr": "1:2",
            "candela": candle_label(curr.name, 60),
            "candela_fine": chiusura_candela(curr.name, 60),
            "note": f"Tenkan cross Kijun al ribasso | Sotto nuvola | RSI {curr['rsi14']:.0f} | ⚠️ Overnight OK se R:R ≥ 1.5"
        }

    return None


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIE 6-8 — ORB SU AZIONI SINGOLE (ANT, CAT, AVGO) 15min
# Intraday — OR = prima candela 09:30 ET, ingressi 10:00-14:30 ET (16:00-20:30 italiane)
#
# Stessa logica dell'ORB S&P500, con adattamenti ai titoli singoli:
#  - OR più largo ammesso (3.0 × ATR invece di 2.5)
#  - stop massimo in % del prezzo (ANT 5%, AVGO 4%, CAT 3%) — valori arbitrari
#  - dati freschi: candela di oggi e chiusa da meno di 25 minuti
#
# ⚠️ Parametri NON validati.
#   ANT: quotata dal 19/11/2026, storico di poche sedute → backtest non significativo.
#   CAT, AVGO: puoi provare `python3 backtest.py azioni` (circa 60 giorni di dati).
# Giorni dei risultati trimestrali: bloccati in automatico SE la data è in
#   UTILI di news_filter.py (gap e volatilità anomali). Aggiorna il calendario.
# Short: ANT disattivato per default (CAT/AVGO attivi). Variabili Railway:
#   ANT_ABILITA_SHORT, CAT_ABILITA_SHORT, AVGO_ABILITA_SHORT  = "1" oppure "0"
# ══════════════════════════════════════════════════════════════════════════════

ANT_ABILITA_SHORT = os.environ.get("ANT_ABILITA_SHORT", "0") == "1"
CAT_ABILITA_SHORT = os.environ.get("CAT_ABILITA_SHORT", "1") == "1"
AVGO_ABILITA_SHORT = os.environ.get("AVGO_ABILITA_SHORT", "1") == "1"


def _check_orb_azione(ticker: str, nome: str, asset: str, short_ok: bool,
                      max_risk_pct: float, nota_extra: str) -> Optional[dict]:
    df = get_data(ticker, period="5d", interval="15m")
    if df is None or len(df) < 40:
        return None

    df["atr14"] = atr(df, 14)
    df["rsi14"] = rsi(df["Close"], 14)
    df["ema20"] = ema(df["Close"], 20)

    # VWAP giornaliero (giorno di borsa USA)
    df["vwap"] = _vwap_giornaliero_et(df)
    df = df.dropna()
    if len(df) < 3:
        return None

    now_et = adesso_et()
    if not in_orario_et(time(10, 0), time(14, 30), now_et):
        return None

    # Usa [-2] = ultima candela chiusa, [-1] è ancora in formazione
    curr = df.iloc[-2]
    prev = df.iloc[-3]

    # Dati freschi: la candela deve essere di oggi e chiusa da poco.
    # Evita segnali su dati vecchi (festivi, mezze sedute, ritardi di Yahoo).
    fine_candela = curr.name + pd.Timedelta(minutes=15)
    if (curr.name.tz_convert(ET_TZ).date() != now_et.date()
            or (now_et - fine_candela) > pd.Timedelta(minutes=25)):
        return None

    today_df = df[df.index.tz_convert(ET_TZ).date == now_et.date()]

    # Opening Range: prima candela 09:30 ET
    or_bar = today_df[today_df.index.tz_convert(ET_TZ).time == APERTURA_USA_ET]
    if len(or_bar) == 0:
        return None

    or_high = or_bar["High"].iloc[0]
    or_low = or_bar["Low"].iloc[0]
    or_range = or_high - or_low

    # Salta se OR troppo ampio
    atr_day = today_df["atr14"].mean()
    if or_range > 3.0 * atr_day:
        return None

    entry = curr["Close"]
    a = curr["atr14"]
    chiudi = ora_it(CHIUSURA_INTRADAY_ET)

    # LONG: breakout sopra OR high + sopra VWAP + EMA20 crescente + RSI > 52
    if (entry > or_high + 0.1 * a and
            entry > curr["vwap"] and
            curr["ema20"] > prev["ema20"] and
            curr["rsi14"] > 52):
        sl = max(or_low, entry - 2.0 * a)
        risk = entry - sl
        if risk <= 0 or risk / entry > max_risk_pct:
            return None
        tp1 = entry + 1.5 * risk
        tp2 = entry + 2.5 * risk
        return {
            "strategia": f"🟢 {nome}",
            "asset": asset,
            "timeframe": "15min",
            "direzione": "LONG",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(tp2),
            "rr": "1:2.5",
            "candela": candle_label(curr.name, 15),
            "candela_fine": chiusura_candela(curr.name, 15),
            "tp1": fmt(tp1),
            "chiudi_entro": chiudi,
            "note": f"Breakout OR high {fmt(or_high)} | Sopra VWAP | TP1 {fmt(tp1)} → sposta SL a BE | "
                    f"{nota_extra} | Chiudi entro {chiudi}"
        }

    # SHORT (se abilitato): breakout sotto OR low + sotto VWAP + EMA20 decrescente + RSI < 48
    if (short_ok and
            entry < or_low - 0.1 * a and
            entry < curr["vwap"] and
            curr["ema20"] < prev["ema20"] and
            curr["rsi14"] < 48):
        sl = min(or_high, entry + 2.0 * a)
        risk = sl - entry
        if risk <= 0 or risk / entry > max_risk_pct:
            return None
        tp1 = entry - 1.5 * risk
        tp2 = entry - 2.5 * risk
        return {
            "strategia": f"🔴 {nome}",
            "asset": asset,
            "timeframe": "15min",
            "direzione": "SHORT",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(tp2),
            "rr": "1:2.5",
            "candela": candle_label(curr.name, 15),
            "candela_fine": chiusura_candela(curr.name, 15),
            "tp1": fmt(tp1),
            "chiudi_entro": chiudi,
            "note": f"Breakout sotto OR low {fmt(or_low)} | Sotto VWAP | TP1 {fmt(tp1)} → sposta SL a BE | "
                    f"{nota_extra} | Chiudi entro {chiudi}"
        }

    return None


def check_orb_ant() -> Optional[dict]:
    return _check_orb_azione(
        TICKER_ANT, "ORB ANTHROPIC (ANT)", "Anthropic (ANT)", ANT_ABILITA_SHORT, 0.05,
        "⚠️ Titolo IPO recente: gap e volatilità elevati, strategia NON testata")


def check_orb_cat() -> Optional[dict]:
    return _check_orb_azione(
        TICKER_CAT, "ORB CATERPILLAR (CAT)", "Caterpillar (CAT)", CAT_ABILITA_SHORT, 0.03,
        "⚠️ Strategia non validata | giorni degli utili bloccati solo se in calendario")


def check_orb_avgo() -> Optional[dict]:
    return _check_orb_azione(
        TICKER_AVGO, "ORB BROADCOM (AVGO)", "Broadcom (AVGO)", AVGO_ABILITA_SHORT, 0.04,
        "⚠️ Strategia non validata | giorni degli utili bloccati solo se in calendario")


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIE 9-10 — RITRACCIAMENTO NEL TREND GIORNALIERO (S&P 500, AVGO)
# Segnale a fine seduta USA, ingresso all'APERTURA del giorno dopo. SOLO LONG.
#   Trend:   chiusura sopra la media a 200 giorni
#   Ingresso: RSI(2) < 10 alla chiusura
#   Uscita:  chiusura sopra la media a 5 giorni | stop = 3 × ATR(14) | max 10 giorni
# Stesse regole di `python3 backtest.py trend` / `trend_azioni`.
# ⚠️ Il bot NON conosce le tue posizioni: segue una posizione TEORICA dal segnale
#    e ti manda un promemoria serale. La posizione teorica è salvata su file (stato.py).
# ⚠️ Nel backtest l'uscita avviene alla chiusura; qui si esce all'apertura del
#    giorno dopo (riga "Uscita apertura dopo" del backtest).
# ══════════════════════════════════════════════════════════════════════════════

PB_RSI_SOGLIA = 10
PB_ATR_STOP = 3.0
PB_MAX_GIORNI = 10


def analizza_pullback(ticker: str, chiave: str, nome: str, posizione: Optional[dict]):
    """Analizza l'ultima candela giornaliera chiusa.
    Ritorna (messaggio | None, posizione_aggiornata, data_candela | None).
    messaggio = dict per notifier.send_pullback; None se non c'è nulla da dire.
    data_candela = None se i dati non sono ancora aggiornati (si può riprovare)."""
    try:
        from news_filter import prossimo_giorno_usa, utili_il, eventi_del_giorno
    except Exception:
        def prossimo_giorno_usa(d):
            x = d + timedelta(days=1)
            while x.weekday() >= 5:
                x += timedelta(days=1)
            return x
        def utili_il(k, d): return False
        def eventi_del_giorno(d): return []

    df = get_data(ticker, period="2y", interval="1d")
    if df is None or len(df) < 230:
        return None, posizione, None

    now_et = datetime.now(ET_TZ)
    last_date = df.index[-1].date()
    # Candela di oggi, a borsa chiusa: altrimenti dati vecchi (festivo) o non ancora aggiornati
    if last_date != now_et.date() or now_et.time() < time(16, 5):
        return None, posizione, None

    df = df.copy()
    df["sma200"] = df["Close"].rolling(200).mean()
    df["sma5"] = df["Close"].rolling(5).mean()
    df["rsi2"] = rsi(df["Close"], 2)
    df["atr14"] = atr(df, 14)
    df = df.dropna()
    if len(df) < 3 or df.index[-1].date() != last_date:
        return None, posizione, None
    last = df.iloc[-1]

    # ── Posizione teorica aperta: stato e promemoria di uscita ───────────────
    if posizione:
        ed = posizione["entry_date"]
        if last_date < ed:                       # ingresso ancora da fare (domani)
            return None, posizione, last_date
        barre = df[[d.date() >= ed for d in df.index]]
        if len(barre) == 0:
            return None, posizione, last_date    # ingresso non ancora avvenuto
        if barre.index[0].date() != ed:
            # Il giorno d'ingresso previsto non c'è nei dati (borsa chiusa per una festività
            # non presente nel calendario): l'ingresso è il primo giorno di borsa successivo.
            # Prima la posizione teorica veniva scartata e i promemoria si fermavano.
            posizione = dict(posizione, entry_date=barre.index[0].date())
        entry = float(barre.iloc[0]["Open"])
        stop = entry - PB_ATR_STOP * posizione["atr"]
        for k in range(len(barre)):
            b = barre.iloc[k]
            giorno = barre.index[k].date()
            gg = k + 1
            evento = None
            if b["Open"] <= stop or b["Low"] <= stop:
                evento = "STOP"
            elif b["Close"] > b["sma5"]:
                evento = "USCITA"
            elif gg >= PB_MAX_GIORNI:
                evento = "TEMPO"
            if evento:
                msg = {"tipo": evento, "nome": nome, "ticker": ticker, "data": giorno,
                       "close": float(b["Close"]), "sma5": float(b["sma5"]),
                       "entry": entry, "stop": stop, "giorni": gg,
                       "recente": giorno == last_date}
                return msg, None, last_date
        b = barre.iloc[-1]
        msg = {"tipo": "MANTIENI", "nome": nome, "ticker": ticker, "data": last_date,
               "close": float(b["Close"]), "sma5": float(b["sma5"]),
               "entry": entry, "stop": stop, "giorni": len(barre)}
        return msg, posizione, last_date

    # ── Nessuna posizione: cerca il segnale di ingresso ─────────────────────
    if last["Close"] > last["sma200"] and last["rsi2"] < PB_RSI_SOGLIA:
        domani = prossimo_giorno_usa(last_date)
        if utili_il(chiave, domani):
            msg = {"tipo": "SALTATO_UTILI", "nome": nome, "ticker": ticker,
                   "data": last_date, "ingresso": domani}
            return msg, None, last_date
        msg = {"tipo": "ENTRATA", "nome": nome, "ticker": ticker, "data": last_date,
               "ingresso": domani, "close": float(last["Close"]),
               "rsi2": float(last["rsi2"]), "sma200": float(last["sma200"]),
               "sma5": float(last["sma5"]), "atr": float(last["atr14"]),
               "stop_dist": PB_ATR_STOP * float(last["atr14"]),
               "eventi": eventi_del_giorno(domani)}
        pos = {"entry_date": domani, "atr": float(last["atr14"]), "signal_date": last_date}
        return msg, pos, last_date

    return None, None, last_date
