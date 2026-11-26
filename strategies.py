"""
STRATEGIE DI TRADING — 5 strategie profittevoli
1. VWAP Reversal Oro 15min (PF 1.84) — intraday
2. London Liquidity Sweep Oro (PF 1.55) — intraday
3. ORB S&P500 15min (PF 1.40) — intraday
4. Kumo Rider Oro 4H (PF 1.62) — swing overnight OK
5. Kumo Rider Nasdaq 1H (PF 1.51) — swing overnight OK
6. ORB Anthropic ANT 15min — intraday (SPERIMENTALE: nessun backtest possibile)
7. ORB Caterpillar CAT 15min — intraday (non validata: usa backtest.py azioni)
8. ORB Broadcom AVGO 15min — intraday (non validata: usa backtest.py azioni)
"""
import logging
import os
from datetime import datetime, time, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import pytz

logger = logging.getLogger(__name__)

ROME_TZ = pytz.timezone("Europe/Rome")
ET_TZ = pytz.timezone("America/New_York")

TICKER_GOLD = "GC=F"
TICKER_SP500 = "^GSPC"
TICKER_NASDAQ = "^NDX"
TICKER_ANT = os.environ.get("TICKER_ANT", "ANT")     # Anthropic, Nasdaq (da giovedì 19/11/2026)
TICKER_CAT = os.environ.get("TICKER_CAT", "CAT")     # Caterpillar, NYSE
TICKER_AVGO = os.environ.get("TICKER_AVGO", "AVGO")  # Broadcom, Nasdaq


# ── Download dati ─────────────────────────────────────────────────────────────

def get_data(ticker: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    try:
        t = yf.Ticker(ticker)
        df = t.history(period=period, interval=interval, auto_adjust=True)
        if df is None or len(df) < 30:
            logger.warning(f"Dati insufficienti per {ticker}")
            return None
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(ROME_TZ)
        return df.dropna()
    except Exception as e:
        logger.error(f"Errore download {ticker}: {e}")
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
    chikou = df["Close"].shift(-disp)
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


def candle_label(ts, minutes: int) -> str:
    """Orario CET della candela su cui si basa il segnale, es. '16:15–16:30 CET'.
    ts = timestamp di APERTURA della candela (curr.name), minutes = durata."""
    start = ts.tz_convert(ROME_TZ)
    end = start + pd.Timedelta(minutes=minutes)
    return f"{start:%H:%M}–{end:%H:%M} CET"


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIA 1 — VWAP REVERSAL ORO 15min (PF 1.84)
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
    df.dropna(inplace=True)
    if len(df) < 3:
        return None

    # Usa [-2] = ultima candela CHIUSA, [-1] è ancora in formazione
    last_cet = df.index[-2].tz_convert(ROME_TZ)
    if not (time(8, 0) <= last_cet.time() <= time(21, 45)):
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
            "asset": "Oro (XAU/USD)",
            "timeframe": "15min",
            "direzione": "LONG",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(tp),
            "rr": "~1.8:1",
            "candela": candle_label(curr.name, 15),
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
            "asset": "Oro (XAU/USD)",
            "timeframe": "15min",
            "direzione": "SHORT",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(tp),
            "rr": "~1.8:1",
            "candela": candle_label(curr.name, 15),
            "note": f"Prezzo sopra VWAP +2σ | RSI {curr['rsi14']:.0f} | Inversione | Chiudi entro 21:45"
        }

    return None


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIA 2 — LONDON LIQUIDITY SWEEP ORO (PF 1.55)
# Intraday — finestra 08:00-10:00 CET
# Falso breakdown del range asiatico 06:00-08:00 → entrata LONG
# ══════════════════════════════════════════════════════════════════════════════

def check_london_sweep_gold() -> Optional[dict]:
    df = get_data(TICKER_GOLD, period="3d", interval="15m")
    if df is None or len(df) < 20:
        return None

    df["sma20"] = sma(df["Close"], 20)
    df["atr14"] = atr(df, 14)
    df.dropna(inplace=True)

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
    a = curr["atr14"]

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
        "asset": "Oro (XAU/USD)",
        "timeframe": "15min",
        "direzione": "LONG",
        "entry": fmt(entry),
        "sl": fmt(sl),
        "tp": fmt(tp),
        "rr": f"1:{(tp-entry)/risk:.1f}",
        "candela": candle_label(curr.name, 15),
        "note": f"Sweep ${sweep_depth:.2f} sotto range asiatico | Reclaim | TP1=50% a {fmt(tp)} | Trailing $4 dopo TP1"
    }


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIA 3 — ORB S&P500 15min (PF 1.40)
# Intraday — OR = prima candela 15:30, trading 15:45-21:45 CET
# ══════════════════════════════════════════════════════════════════════════════

def check_orb_sp500() -> Optional[dict]:
    df = get_data(TICKER_SP500, period="5d", interval="15m")
    if df is None or len(df) < 40:
        return None

    df["atr14"] = atr(df, 14)
    df["rsi14"] = rsi(df["Close"], 14)
    df["ema20"] = ema(df["Close"], 20)

    # VWAP giornaliero
    df_copy = df.copy()
    df_copy["_d"] = df_copy.index.date
    parts = []
    for d, g in df_copy.groupby("_d"):
        tp = (g["High"] + g["Low"] + g["Close"]) / 3
        parts.append((tp * g["Volume"]).cumsum() / g["Volume"].cumsum())
    df["vwap"] = pd.concat(parts).reindex(df.index)
    df.dropna(inplace=True)

    now_cet = datetime.now(ROME_TZ)
    if not (time(15, 45) <= now_cet.time() <= time(21, 45)):
        return None

    today = now_cet.date()
    today_df = df[df.index.date == today]

    # Opening Range: prima candela 15:30
    or_bar = today_df[today_df.index.time == time(15, 30)]
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
            "tp1": fmt(tp1),
            "note": f"Breakout OR high {fmt(or_high)} | Sopra VWAP | TP1 {fmt(tp1)} → sposta SL a BE | Chiudi entro 21:45"
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
            "tp1": fmt(tp1),
            "note": f"Breakout sotto OR low {fmt(or_low)} | Sotto VWAP | TP1 {fmt(tp1)} → sposta SL a BE | Chiudi entro 21:45"
        }

    return None


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIA 4 — KUMO RIDER ORO 4H (PF 1.62)
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

    t_sen, k_sen, s_a, s_b, chikou = ichimoku(df)
    df["t_sen"] = t_sen
    df["k_sen"] = k_sen
    df["s_a"] = s_a
    df["s_b"] = s_b
    df["chikou"] = chikou
    df["rsi14"] = rsi(df["Close"], 14)
    df["atr14"] = atr(df, 14)
    df.dropna(inplace=True)
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
    chikou_ref = df["Close"].iloc[-28] if len(df) > 28 else df["Close"].iloc[0]

    if (cross_up and entry > kumo_top and
            40 < curr["rsi14"] < 65 and
            curr["chikou"] > chikou_ref and
            curr["t_sen"] > curr["s_a"] and curr["t_sen"] > curr["s_b"]):
        sl = curr["k_sen"] - 0.3 * a
        risk = entry - sl
        if risk <= 0:
            return None
        return {
            "strategia": "🟡 KUMO RIDER ORO 4H",
            "asset": "Oro (XAU/USD)",
            "timeframe": "4H (swing)",
            "direzione": "LONG",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(entry + 2.0 * risk),
            "rr": "1:2",
            "candela": candle_label(curr.name, 240),
            "note": f"Tenkan cross Kijun | Sopra nuvola | RSI {curr['rsi14']:.0f} | ⚠️ Overnight OK se R:R ≥ 1.5 già raggiunto"
        }

    if (cross_dn and entry < kumo_bot and
            35 < curr["rsi14"] < 60 and
            curr["chikou"] < chikou_ref and
            curr["t_sen"] < curr["s_a"] and curr["t_sen"] < curr["s_b"]):
        sl = curr["k_sen"] + 0.3 * a
        risk = sl - entry
        if risk <= 0:
            return None
        return {
            "strategia": "🔴 KUMO RIDER ORO 4H",
            "asset": "Oro (XAU/USD)",
            "timeframe": "4H (swing)",
            "direzione": "SHORT",
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(entry - 2.0 * risk),
            "rr": "1:2",
            "candela": candle_label(curr.name, 240),
            "note": f"Tenkan cross Kijun al ribasso | Sotto nuvola | RSI {curr['rsi14']:.0f} | ⚠️ Overnight OK se R:R ≥ 1.5"
        }

    return None


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIA 5 — KUMO RIDER NASDAQ 1H (PF 1.51)
# Swing — overnight OK se segnale forte
# Ichimoku 9/26/52 + RSI 40-65 + filtro orario 15:30-21:00
# ══════════════════════════════════════════════════════════════════════════════

def check_kumo_nasdaq_1h() -> Optional[dict]:
    df = get_data(TICKER_NASDAQ, period="2y", interval="1h")
    if df is None or len(df) < 60:
        return None

    t_sen, k_sen, s_a, s_b, chikou = ichimoku(df)
    df["t_sen"] = t_sen
    df["k_sen"] = k_sen
    df["s_a"] = s_a
    df["s_b"] = s_b
    df["chikou"] = chikou
    df["rsi14"] = rsi(df["Close"], 14)
    df["atr14"] = atr(df, 14)
    df.dropna(inplace=True)
    if len(df) < 4:
        return None

    # Usa [-2] = ultima candela 1H chiusa, [-1] è ancora in formazione
    last_cet = df.index[-2].tz_convert(ROME_TZ)
    if not (time(15, 30) <= last_cet.time() <= time(21, 0)):
        return None

    curr = df.iloc[-2]
    prev = df.iloc[-3]

    kumo_top = max(curr["s_a"], curr["s_b"])
    kumo_bot = min(curr["s_a"], curr["s_b"])
    cross_up = prev["t_sen"] <= prev["k_sen"] and curr["t_sen"] > curr["k_sen"]
    cross_dn = prev["t_sen"] >= prev["k_sen"] and curr["t_sen"] < curr["k_sen"]
    entry = curr["Close"]
    a = curr["atr14"]
    chikou_ref = df["Close"].iloc[-28] if len(df) > 28 else df["Close"].iloc[0]

    if (cross_up and entry > kumo_top and
            40 < curr["rsi14"] < 65 and
            curr["chikou"] > chikou_ref and
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
            "note": f"Tenkan cross Kijun | Sopra nuvola | RSI {curr['rsi14']:.0f} | ⚠️ Overnight OK se R:R ≥ 1.5"
        }

    if (cross_dn and entry < kumo_bot and
            35 < curr["rsi14"] < 60 and
            curr["chikou"] < chikou_ref and
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
            "note": f"Tenkan cross Kijun al ribasso | Sotto nuvola | RSI {curr['rsi14']:.0f} | ⚠️ Overnight OK se R:R ≥ 1.5"
        }

    return None


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIE 6-8 — ORB SU AZIONI SINGOLE (ANT, CAT, AVGO) 15min
# Intraday — OR = prima candela 15:30, ingressi 16:00-20:30 CET
#
# Stessa logica dell'ORB S&P500, con adattamenti ai titoli singoli:
#  - OR più largo ammesso (3.0 × ATR invece di 2.5)
#  - stop massimo in % del prezzo (ANT 5%, AVGO 4%, CAT 3%) — valori arbitrari
#  - dati freschi: candela di oggi e chiusa da meno di 25 minuti
#
# ⚠️ Parametri NON validati.
#   ANT: quotata dal 19/11/2026, nessuno storico → niente backtest possibile.
#   CAT, AVGO: puoi provare `python3 backtest.py azioni` (circa 60 giorni di dati).
# ⚠️ Evita i giorni dei risultati trimestrali (gap e volatilità anomali).
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

    # VWAP giornaliero
    df_copy = df.copy()
    df_copy["_d"] = df_copy.index.date
    parts = []
    for d, g in df_copy.groupby("_d"):
        tp = (g["High"] + g["Low"] + g["Close"]) / 3
        parts.append((tp * g["Volume"]).cumsum() / g["Volume"].cumsum())
    df["vwap"] = pd.concat(parts).reindex(df.index)
    df.dropna(inplace=True)
    if len(df) < 3:
        return None

    now_cet = datetime.now(ROME_TZ)
    if not (time(16, 0) <= now_cet.time() <= time(20, 30)):
        return None

    # Usa [-2] = ultima candela chiusa, [-1] è ancora in formazione
    curr = df.iloc[-2]
    prev = df.iloc[-3]

    # Dati freschi: la candela deve essere di oggi e chiusa da poco.
    # Evita segnali su dati vecchi (festivi, mezze sedute, ritardi di Yahoo).
    fine_candela = curr.name + pd.Timedelta(minutes=15)
    if curr.name.date() != now_cet.date() or (now_cet - fine_candela) > pd.Timedelta(minutes=25):
        return None

    today_df = df[df.index.date == now_cet.date()]

    # Opening Range: prima candela 15:30
    or_bar = today_df[today_df.index.time == time(15, 30)]
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
            "tp1": fmt(tp1),
            "note": f"Breakout OR high {fmt(or_high)} | Sopra VWAP | TP1 {fmt(tp1)} → sposta SL a BE | "
                    f"{nota_extra} | Chiudi entro 21:45"
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
            "tp1": fmt(tp1),
            "note": f"Breakout sotto OR low {fmt(or_low)} | Sotto VWAP | TP1 {fmt(tp1)} → sposta SL a BE | "
                    f"{nota_extra} | Chiudi entro 21:45"
        }

    return None


def check_orb_ant() -> Optional[dict]:
    return _check_orb_azione(
        TICKER_ANT, "ORB ANTHROPIC (ANT)", "Anthropic (ANT)", ANT_ABILITA_SHORT, 0.05,
        "⚠️ Titolo IPO recente: gap e volatilità elevati, strategia NON testata")


def check_orb_cat() -> Optional[dict]:
    return _check_orb_azione(
        TICKER_CAT, "ORB CATERPILLAR (CAT)", "Caterpillar (CAT)", CAT_ABILITA_SHORT, 0.03,
        "⚠️ Evita i giorni dei risultati trimestrali | strategia non validata")


def check_orb_avgo() -> Optional[dict]:
    return _check_orb_azione(
        TICKER_AVGO, "ORB BROADCOM (AVGO)", "Broadcom (AVGO)", AVGO_ABILITA_SHORT, 0.04,
        "⚠️ Evita i giorni dei risultati trimestrali | strategia non validata")


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIE 9-10 — RITRACCIAMENTO NEL TREND GIORNALIERO (S&P 500, AVGO)
# Segnale a fine seduta USA, ingresso all'APERTURA del giorno dopo. SOLO LONG.
#   Trend:   chiusura sopra la media a 200 giorni
#   Ingresso: RSI(2) < 10 alla chiusura
#   Uscita:  chiusura sopra la media a 5 giorni | stop = 3 × ATR(14) | max 10 giorni
# Stesse regole di `python3 backtest.py trend` / `trend_azioni`.
# ⚠️ Il bot NON conosce le tue posizioni: segue una posizione TEORICA dal segnale
#    e ti manda un promemoria serale. Dopo un riavvio la memoria si azzera.
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
        if len(barre) == 0 or barre.index[0].date() != ed:
            return None, None, last_date         # dati incoerenti: azzera
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
