#!/usr/bin/env python3
"""
GoldTrendEA — TradingView → Telegram Webhook 服务器
====================================================

流程:
  TradingView 触发 alert() → POST /webhook → 解析JSON → 发Telegram消息

本地测试:
  1. 设置环境变量:
       export TG_BOT_TOKEN="7xxxxxxxxxx:AAxxxxxxxxxxxxxxxxxxxxxxxxx"
       export TG_CHAT_ID="123456789"

  2. 启动服务器:
       pip3 install flask requests
       python3 telegram_webhook.py

  3. 用 ngrok 暴露端口 (另开终端):
       ngrok http 5000
       → 复制 https://xxxx.ngrok-free.app 填入 TradingView alert webhook URL

部署 (Render.com 免费, 永久在线):
  见文件末尾说明

获取 Telegram Bot Token 和 Chat ID:
  1. 在Telegram搜索 @BotFather → /newbot → 得到 Token
  2. 搜索 @userinfobot → 得到自己的 Chat ID
"""

import json
import os
import hashlib
import hmac
from datetime import datetime, timezone
from flask import Flask, request, jsonify, abort
import requests

app = Flask(__name__)

# ══════════════════════════════════════════
#  配置 (优先读取环境变量)
# ══════════════════════════════════════════
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")   # Telegram Bot Token (必填)
TG_CHAT_ID   = os.getenv("TG_CHAT_ID",   "")   # Telegram Chat ID  (必填)
WH_SECRET    = os.getenv("WH_SECRET",    "")   # Webhook 安全令牌 (可选, 防止他人触发)
PORT         = int(os.getenv("PORT", 5000))

# ══════════════════════════════════════════
#  Telegram 发送
# ══════════════════════════════════════════

def tg_send(text: str, parse_mode: str = "HTML") -> bool:
    """发送 Telegram 消息，返回是否成功"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[ERROR] 未设置 TG_BOT_TOKEN 或 TG_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id":              TG_CHAT_ID,
            "text":                 text,
            "parse_mode":           parse_mode,
            "disable_notification": False,
        }, timeout=10)
        if not resp.ok:
            print(f"[TG ERROR] {resp.status_code}: {resp.text[:200]}")
        return resp.ok
    except Exception as e:
        print(f"[TG EXCEPTION] {e}")
        return False


# ══════════════════════════════════════════
#  消息格式化
# ══════════════════════════════════════════

def format_signal(data: dict) -> str:
    signal = data.get("signal", "").upper()   # BUY / SELL / READY_BUY / READY_SELL
    price  = data.get("price",  "?")
    tp     = data.get("tp",     None)
    sl     = data.get("sl",     None)
    atr    = data.get("atr",    None)
    ticker = data.get("ticker", "XAUUSD")
    tf     = data.get("tf",     "M5")
    now    = datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")

    is_buy   = "BUY"  in signal and "READY" not in signal
    is_sell  = "SELL" in signal and "READY" not in signal
    is_ready = "READY" in signal

    if is_ready:
        direction = "做多 BUY" if "BUY" in signal else "做空 SELL"
        return (
            f"⚠️ <b>GoldTrendEA v6.5 — 准备信号</b>\n"
            f"\n"
            f"📌 {ticker} [{tf}]\n"
            f"💰 当前价: <b>{price}</b>\n"
            f"⏳ 等待 Stoch 金叉/死叉触发 ({direction})\n"
            f"\n"
            f"⏰ {now}"
        )

    if is_buy:
        arrow, action, color_emoji = "🟢", "做多 BUY", "📈"
    elif is_sell:
        arrow, action, color_emoji = "🔴", "做空 SELL", "📉"
    else:
        return f"📡 GoldTrendEA 信号: {signal} @ {price}\n{now}"

    lines = [
        f"{arrow} <b>GoldTrendEA v6.5 — {action} 信号!</b>",
        f"",
        f"📌 品种: <b>{ticker}</b>  [{tf}]",
        f"{color_emoji} 进场价: <b>{price}</b>",
    ]

    if tp is not None:
        tp_dist = abs(float(tp) - float(price)) if price != "?" else 0
        lines.append(f"🎯 止盈 TP: <b>{tp}</b>  (+${tp_dist:.2f})")
    if sl is not None:
        sl_dist = abs(float(price) - float(sl)) if price != "?" else 0
        lines.append(f"🛡️ 止损 SL: <b>{sl}</b>  (-${sl_dist:.2f})")
    if tp is not None and sl is not None and price != "?":
        try:
            rr = abs(float(tp) - float(price)) / abs(float(price) - float(sl))
            lines.append(f"📊 R:R = 1 : {rr:.1f}")
        except ZeroDivisionError:
            pass
    if atr is not None:
        lines.append(f"📉 ATR: {atr}")

    lines += [
        f"",
        f"⚠️ <b>下一根K线开盘进场</b>",
        f"⏰ {now}",
    ]

    return "\n".join(lines)


# ══════════════════════════════════════════
#  Webhook 端点
# ══════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    # ── 安全令牌验证 (可选) ──
    if WH_SECRET:
        token = (request.args.get("token")
                 or request.headers.get("X-Webhook-Token", ""))
        if not hmac.compare_digest(token, WH_SECRET):
            abort(403)

    # ── 解析请求体 ──
    raw = request.data.decode("utf-8", errors="ignore").strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # TradingView 有时发纯文字，直接转发
        print(f"[RAW] {raw[:200]}")
        ok = tg_send(f"📡 <b>GoldTrendEA Alert</b>\n\n{raw}")
        return jsonify({"ok": ok, "mode": "raw"})

    sig = data.get("signal", "UNKNOWN")
    px  = data.get("price",  "?")
    ts  = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] signal={sig}  price={px}  ticker={data.get('ticker','?')}")

    msg = format_signal(data)
    ok  = tg_send(msg)

    return jsonify({"ok": ok, "signal": sig})


# ══════════════════════════════════════════
#  辅助端点
# ══════════════════════════════════════════

@app.route("/", methods=["GET"])
def index():
    bot_ok   = "✅" if TG_BOT_TOKEN else "❌ 未设置 TG_BOT_TOKEN"
    chat_ok  = "✅" if TG_CHAT_ID   else "❌ 未设置 TG_CHAT_ID"
    sec_ok   = "✅ 已开启" if WH_SECRET else "— 未设置"
    return (
        f"<h2>GoldTrendEA Webhook Server ✅</h2>"
        f"<p>Bot Token: {bot_ok}<br>"
        f"Chat ID: {chat_ok}<br>"
        f"安全令牌: {sec_ok}</p>"
        f"<p>Webhook URL: <code>POST /webhook</code><br>"
        f"测试: <a href='/test'>/test</a></p>"
    )


@app.route("/test", methods=["GET"])
def test_send():
    """发一条测试消息到 Telegram，验证配置是否正确"""
    ok = tg_send(format_signal({
        "signal": "BUY",
        "price":  "3120.50",
        "tp":     "3129.96",
        "sl":     "3118.90",
        "atr":    "3.20",
        "ticker": "XAUUSD",
        "tf":     "M5",
    }))
    return jsonify({
        "ok":  ok,
        "msg": "✅ 测试消息已发到 Telegram!" if ok else "❌ 失败，请检查 TG_BOT_TOKEN 和 TG_CHAT_ID",
    })


# ══════════════════════════════════════════
#  启动
# ══════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  GoldTrendEA — TradingView → Telegram Webhook")
    print("=" * 55)
    print(f"  Bot Token : {'✅ 已设置' if TG_BOT_TOKEN else '❌ 未设置 → export TG_BOT_TOKEN=xxx'}")
    print(f"  Chat ID   : {'✅ 已设置' if TG_CHAT_ID   else '❌ 未设置 → export TG_CHAT_ID=xxx'}")
    print(f"  安全令牌  : {'✅ 已开启' if WH_SECRET     else '— 未设置 (可选)'}")
    print(f"  端口      : {PORT}")
    print(f"")
    print(f"  本地测试步骤:")
    print(f"    1. 打开 http://localhost:{PORT}/test  验证Bot")
    print(f"    2. ngrok http {PORT}  得到公网URL")
    print(f"    3. 在TradingView alert填写: https://xxxx.ngrok-free.app/webhook")
    print("=" * 55)
    app.run(host="0.0.0.0", port=PORT, debug=False)


# ══════════════════════════════════════════
#  Render.com 免费部署步骤
# ══════════════════════════════════════════
#
#  1. 把整个 EA 文件夹推到 GitHub 私人仓库
#
#  2. 去 https://render.com → New → Web Service
#     - 连接 GitHub 仓库
#     - Build Command:  pip install -r requirements_webhook.txt
#     - Start Command:  python telegram_webhook.py
#
#  3. 在 Render → Environment 设置环境变量:
#     TG_BOT_TOKEN = 7xxx:AAAxxx
#     TG_CHAT_ID   = 123456789
#     WH_SECRET    = 随便一串字母 (如: goldea2026)
#     PORT         = 10000
#
#  4. Render 会给一个 https://your-app.onrender.com 的永久URL
#
#  5. TradingView Webhook URL:
#     https://your-app.onrender.com/webhook?token=goldea2026
#
# ══════════════════════════════════════════
#  本地 ngrok 快速测试步骤
# ══════════════════════════════════════════
#
#  1. brew install ngrok  (或 https://ngrok.com 下载)
#  2. ngrok config add-authtoken <your_token>
#  3. 另开终端: ngrok http 5000
#  4. 复制 https://xxxx.ngrok-free.app
#  5. TradingView alert → 勾选 Webhook URL →
#     填: https://xxxx.ngrok-free.app/webhook
#
# ══════════════════════════════════════════
