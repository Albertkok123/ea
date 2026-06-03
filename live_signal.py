#!/usr/bin/env python3
"""
GoldTrendEA v6.5 — 实时信号监控 + Telegram 通知
================================================
两种运行模式:

[本地模式] 无 PORT 环境变量，无限循环每5分钟检测一次:
    export TG_BOT_TOKEN="7xxx:AAAxxx"
    export TG_CHAT_ID="-1001234567890"
    python3.11 live_signal.py

[Web模式] 有 PORT 环境变量，启动 Flask 服务器:
    - Render Web Service 免费部署
    - UptimeRobot 每5分钟 GET /health → 触发信号检测
    - GET / → 状态页面

注意: yfinance 数据有约15分钟延迟（免费行情）
"""

import os, time, warnings
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from datetime import datetime, timezone
from flask import Flask, jsonify

warnings.filterwarnings("ignore")

app      = Flask(__name__)
last_bar = None   # 全局：记录已发送的最后一根bar，防重复

# ══════════════════════════════════════════
#  配置
# ══════════════════════════════════════════
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID",   "")

# 策略参数 (与 v6.5 Pine Script 一致)
TP_MULTI      = 3.0
SL_MULTI      = 0.5
ATR_MIN       = 2.0
ATR_MAX       = 8.0
ADX_MIN       = 15.0
SLOPE_MIN     = 3.0
RSI_BULL_MAX  = 80.0
RSI_BEAR_MIN  = 20.0
TRADE_START   = 7     # UTC
TRADE_END     = 18    # UTC
H1_TRIG_BARS  = 2     # H1触发窗口宽度

# ══════════════════════════════════════════
#  指标函数 (与 backtest.py 相同)
# ══════════════════════════════════════════

def calc_ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def calc_rsi(c, p=14):
    d = c.diff()
    g = d.clip(lower=0)
    l = (-d).clip(lower=0)
    return 100 - 100 / (1 + g.ewm(alpha=1/p, adjust=False).mean()
                           / (l.ewm(alpha=1/p, adjust=False).mean() + 1e-10))

def calc_stoch(hi, lo, c, k=8, d=3, sl=3):
    raw = 100 * (c - lo.rolling(k).min()) / (hi.rolling(k).max() - lo.rolling(k).min() + 1e-10)
    K   = raw.rolling(sl).mean()
    D   = K.rolling(d).mean()
    return K, D

def calc_atr(hi, lo, c, p=14):
    tr = pd.concat([hi - lo,
                    (hi - c.shift()).abs(),
                    (lo - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(p).mean()

def calc_adx(hi, lo, c, p=14):
    up  = hi.diff()
    dn  = -lo.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up,  0.), index=hi.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn,  0.), index=hi.index)
    tr  = pd.concat([hi - lo,
                     (hi - c.shift()).abs(),
                     (lo - c.shift()).abs()], axis=1).max(axis=1)
    a   = 1.0 / p
    pdi = 100 * pdm.ewm(alpha=a, adjust=False).mean() / (tr.ewm(alpha=a, adjust=False).mean() + 1e-10)
    mdi = 100 * mdm.ewm(alpha=a, adjust=False).mean() / (tr.ewm(alpha=a, adjust=False).mean() + 1e-10)
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-10)
    return dx.ewm(alpha=a, adjust=False).mean()

# ══════════════════════════════════════════
#  Telegram
# ══════════════════════════════════════════

def tg_send(text: str) -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[ERROR] 未设置 TG_BOT_TOKEN 或 TG_CHAT_ID")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_notification": False},
            timeout=10)
        return resp.ok
    except Exception as e:
        print(f"[TG ERROR] {e}")
        return False


def format_signal(direction, price, tp, sl, atr, bar_time):
    is_buy   = direction == "BUY"
    arrow    = "🟢" if is_buy else "🔴"
    action   = "做多 BUY"  if is_buy else "做空 SELL"
    emoji    = "📈" if is_buy else "📉"
    tp_dist  = abs(tp - price)
    sl_dist  = abs(price - sl)
    rr       = tp_dist / sl_dist if sl_dist > 0 else 0
    bar_str  = bar_time.strftime("%m-%d %H:%M UTC")
    now_str  = datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")

    return (
        f"{arrow} <b>GoldTrendEA v6.5 — {action}!</b>\n"
        f"\n"
        f"📌 品种: <b>XAUUSD (GC=F)</b>  [M5]\n"
        f"{emoji} 进场价: <b>{price:.2f}</b>\n"
        f"🎯 止盈 TP: <b>{tp:.2f}</b>  (+${tp_dist:.2f})\n"
        f"🛡️ 止损 SL: <b>{sl:.2f}</b>  (-${sl_dist:.2f})\n"
        f"📊 R:R = 1 : {rr:.1f}\n"
        f"📉 M5 ATR: {atr:.2f}\n"
        f"\n"
        f"⚠️ <b>下一根K线开盘进场</b>\n"
        f"📅 信号K线: {bar_str}\n"
        f"⏰ 发送时间: {now_str}"
    )


# ══════════════════════════════════════════
#  数据下载
# ══════════════════════════════════════════

def download_m5():
    """下载 5 天 M5 数据 (含指标预热)"""
    df = yf.download("GC=F", period="5d", interval="5m",
                     progress=False, auto_adjust=True)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df.index   = pd.to_datetime(df.index, utc=True).tz_convert("UTC")
    return df.dropna()


# ══════════════════════════════════════════
#  信号检测 (v6.5 完整逻辑)
# ══════════════════════════════════════════

def detect_signal(m5: pd.DataFrame):
    """
    在最近一根完成的M5 bar上检测信号
    返回 ("BUY"/"SELL", price, tp, sl, atr, bar_time) 或 None
    """
    if len(m5) < 60:
        return None

    # ── H4 EMA50 趋势 + 斜率 ──
    h4       = m5.resample("4h").agg({"High":"max","Low":"min","Close":"last","Open":"first"}).dropna()
    h4_ema_r = calc_ema(h4["Close"], 50)
    h4_ema   = h4_ema_r.reindex(m5.index, method="ffill")
    h4_slope = h4_ema_r.diff(5).reindex(m5.index, method="ffill")

    # ── H1 指标 ──
    h1       = m5.resample("1h").agg({"High":"max","Low":"min","Close":"last","Open":"first"}).dropna()
    h1_adx   = calc_adx(h1["High"], h1["Low"], h1["Close"]).reindex(m5.index, method="ffill")
    h1_ema50 = calc_ema(h1["Close"], 50).reindex(m5.index, method="ffill")
    h1_stK, h1_stD = calc_stoch(h1["High"], h1["Low"], h1["Close"])

    # ── H1 Stoch 交叉触发窗口 (v6.5) ──
    h1_cross_bull = ((h1_stK.shift(1) <= h1_stD.shift(1)) &
                     (h1_stK > h1_stD) & (h1_stK < 50.0))
    h1_cross_bear = ((h1_stK.shift(1) >= h1_stD.shift(1)) &
                     (h1_stK < h1_stD) & (h1_stK > 50.0))
    h1_bull_win = h1_cross_bull.rolling(H1_TRIG_BARS).max().fillna(0).astype(bool)
    h1_bear_win = h1_cross_bear.rolling(H1_TRIG_BARS).max().fillna(0).astype(bool)
    h1_bull_win_m5 = h1_bull_win.reindex(m5.index, method="ffill")
    h1_bear_win_m5 = h1_bear_win.reindex(m5.index, method="ffill")

    # ── M5 指标 ──
    rsi        = calc_rsi(m5["Close"])
    stK, stD   = calc_stoch(m5["High"], m5["Low"], m5["Close"])
    atr        = calc_atr(m5["High"], m5["Low"], m5["Close"])

    # ── 检查最近一根完成的bar (index = -2，避免用未完成bar) ──
    i = len(m5) - 2

    bar_time  = m5.index[i]
    close_p   = float(m5["Close"].iloc[i])
    open_p    = float(m5["Open"].iloc[i])
    cur_atr   = float(atr.iloc[i])
    cur_slope = float(h4_slope.iloc[i])

    if np.isnan(cur_atr) or cur_atr < 0.1:
        return None

    # 过滤器
    time_ok       = TRADE_START <= bar_time.hour < TRADE_END
    atr_ok        = ATR_MIN <= cur_atr <= ATR_MAX
    adx_ok        = float(h1_adx.iloc[i]) >= ADX_MIN
    slope_bull_ok = cur_slope > SLOPE_MIN
    slope_bear_ok = cur_slope < -SLOPE_MIN
    h1_ema_bull   = close_p > float(h1_ema50.iloc[i])
    h1_ema_bear   = close_p < float(h1_ema50.iloc[i])
    h4_bull       = close_p > float(h4_ema.iloc[i])
    h4_bear       = close_p < float(h4_ema.iloc[i])
    h1_trig_bull  = bool(h1_bull_win_m5.iloc[i])
    h1_trig_bear  = bool(h1_bear_win_m5.iloc[i])
    candle_bull   = close_p > open_p
    candle_bear   = close_p < open_p

    cur_stK  = float(stK.iloc[i])
    cur_stD  = float(stD.iloc[i])
    prev_stK = float(stK.iloc[i-1])
    prev_stD = float(stD.iloc[i-1])
    cur_rsi  = float(rsi.iloc[i])

    bull_cross = (prev_stK <= prev_stD and cur_stK > cur_stD
                  and cur_stK < 50.0 and cur_rsi < RSI_BULL_MAX)
    bear_cross = (prev_stK >= prev_stD and cur_stK < cur_stD
                  and cur_stK > 50.0 and cur_rsi > RSI_BEAR_MIN)

    buy_signal  = (h4_bull and h1_ema_bull and h1_trig_bull
                   and slope_bull_ok and bull_cross
                   and adx_ok and time_ok and atr_ok and candle_bull)
    sell_signal = (h4_bear and h1_ema_bear and h1_trig_bear
                   and slope_bear_ok and bear_cross
                   and adx_ok and time_ok and atr_ok and candle_bear)

    if buy_signal:
        tp = close_p + cur_atr * TP_MULTI
        sl = close_p - cur_atr * SL_MULTI
        return ("BUY", close_p, tp, sl, cur_atr, bar_time)

    if sell_signal:
        tp = close_p - cur_atr * TP_MULTI
        sl = close_p + cur_atr * SL_MULTI
        return ("SELL", close_p, tp, sl, cur_atr, bar_time)

    return None


# ══════════════════════════════════════════
#  睡到下一根M5收盘
# ══════════════════════════════════════════

def sleep_to_next_bar():
    """等到下一个5分钟整点 + 10秒 buffer"""
    now     = datetime.now(timezone.utc)
    elapsed = now.second + (now.minute % 5) * 60
    wait    = (5 * 60 - elapsed) + 10
    next_t  = now.replace(second=0, microsecond=0)
    mins    = (now.minute // 5 + 1) * 5
    print(f"  💤 等待 {wait}秒 → 下一根M5收盘后检查...")
    time.sleep(wait)


# ══════════════════════════════════════════
#  核心：单次信号检测（两种模式共用）
# ══════════════════════════════════════════

def check_once() -> dict:
    """下载数据、检测信号、发送 Telegram。返回状态字典。"""
    global last_bar
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    try:
        print(f"\n[{ts}] 📡 下载M5数据...", end=" ", flush=True)
        m5 = download_m5()
        print(f"{len(m5)}根", end="  ", flush=True)

        result = detect_signal(m5)

        if result is None:
            bar_t = m5.index[-2]
            print(f"无信号  bar={bar_t.strftime('%m-%d %H:%M')}")
            return {"signal": None, "bar": bar_t.strftime("%m-%d %H:%M")}

        direction, price, tp, sl, atr_v, bar_time = result

        if bar_time == last_bar:
            print(f"重复信号跳过  {direction} @ {price:.2f}")
            return {"signal": direction, "price": price, "duplicate": True}

        last_bar = bar_time
        print(f"\n  🎯 {direction}  price={price:.2f}  tp={tp:.2f}  sl={sl:.2f}")
        msg = format_signal(direction, price, tp, sl, atr_v, bar_time)
        ok  = tg_send(msg)
        print(f"  Telegram: {'✅ 已发送' if ok else '❌ 发送失败'}")
        return {"signal": direction, "price": price, "tg_ok": ok}

    except Exception as e:
        print(f"\n[ERROR] {e}")
        tg_send(f"⚠️ GoldTrendEA 监控异常: {e}")
        return {"error": str(e)}


# ══════════════════════════════════════════
#  Web 模式：Flask 端点（Render Web Service）
# ══════════════════════════════════════════

@app.route("/")
def index():
    bot_ok  = "✅" if TG_BOT_TOKEN else "❌ 未设置"
    chat_ok = "✅" if TG_CHAT_ID   else "❌ 未设置"
    now     = datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")
    return (
        f"<h2>GoldTrendEA v6.5 Live Signal ✅</h2>"
        f"<p>Bot Token: {bot_ok}<br>"
        f"Chat ID: {chat_ok}<br>"
        f"服务器时间: {now}</p>"
        f"<p>UptimeRobot 每5分钟 ping: "
        f"<code>GET /health</code></p>"
    )


@app.route("/health")
def health():
    """UptimeRobot 每5分钟 ping 这里 → 触发信号检测"""
    result = check_once()
    return jsonify({"ok": True, **result})


# ══════════════════════════════════════════
#  入口：自动判断模式
# ══════════════════════════════════════════

def main():
    print("=" * 52)
    print("  GoldTrendEA v6.5 — 实时信号监控")
    print("=" * 52)
    print(f"  Bot Token : {'✅ 已设置' if TG_BOT_TOKEN else '❌ 未设置'}")
    print(f"  Chat ID   : {'✅ 已设置' if TG_CHAT_ID   else '❌ 未设置'}")
    print(f"  参数      : TP×{TP_MULTI}  SL×{SL_MULTI}  ATR[{ATR_MIN}-{ATR_MAX}]")
    print(f"  交易时间  : UTC {TRADE_START}:00 ~ {TRADE_END}:00")
    print("=" * 52)

    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("\n❌ 请先设置环境变量:")
        print("   export TG_BOT_TOKEN='xxx'")
        print("   export TG_CHAT_ID='xxx'")
        return

    port = int(os.getenv("PORT", 0))

    if port:
        # ── Web 模式（Render 自动设置 PORT）──
        print(f"  模式      : 🌐 Web Service (PORT={port})")
        print(f"  UptimeRobot: GET /health 每5分钟")
        print("=" * 52)
        tg_send(
            "🚀 <b>GoldTrendEA v6.5 Web模式启动</b>\n"
            f"📌 XAUUSD M5  |  PORT={port}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%m-%d %H:%M UTC')}"
        )
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        # ── 本地轮询模式 ──
        print("  模式      : 🖥️  本地轮询 (每5分钟)")
        print("=" * 52)
        tg_send(
            "🚀 <b>GoldTrendEA v6.5 监控启动</b>\n"
            f"📌 XAUUSD M5\n"
            f"🎯 TP×{TP_MULTI}  SL×{SL_MULTI}  ATR[{ATR_MIN}-{ATR_MAX}]\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%m-%d %H:%M UTC')}"
        )
        while True:
            check_once()
            sleep_to_next_bar()


if __name__ == "__main__":
    main()
