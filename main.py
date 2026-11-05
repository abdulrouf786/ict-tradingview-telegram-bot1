import os, json, time, urllib.request, urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ICT Scanner prototype: Yahoo Finance market data, Telegram bot polling.
# Simplified liquidity sweep + displacement + FVG detector; not all ICT models.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
PKT = ZoneInfo("Asia/Karachi")
MARKETS = {
    "XAUUSD (GC futures proxy)": "GC=F",
    "BTCUSD": "BTC-USD",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
}
TF = "5m"
RISK_REWARD = 2.0
SCAN_SECONDS = 60
last_update_id = 0
SIGNALS_FILE = "signals_seen.json"

def load_signal_history():
    try:
        with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

last_signal = load_signal_history()
tracked = {}

def now_pkt():
    return datetime.now(PKT).strftime("%Y-%m-%d %I:%M:%S %p PKT")

def http_json(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent":"Mozilla/5.0 ICTScanner/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

def tg(method, payload=None):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    return http_json(url, urllib.parse.urlencode(payload or {}).encode())

def send(text, chat_id=None):
    target = str(chat_id or CHAT_ID).strip()
    if target:
        tg("sendMessage", {"chat_id":target, "text":text})
    else:
        print("Telegram alert (CHAT_ID unset):\n", text)

def candles(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol,safe='')}?range=5d&interval={TF}"
    res = http_json(url)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows=[]
    for i, ts in enumerate(res.get("timestamp", [])):
        try:
            o,h,l,c = q["open"][i],q["high"][i],q["low"][i],q["close"][i]
            if None in (o,h,l,c): continue
            rows.append({"time":datetime.fromtimestamp(ts,timezone.utc).astimezone(PKT),
                         "open":float(o),"high":float(h),"low":float(l),"close":float(c)})
        except (IndexError,TypeError,ValueError): pass
    return rows

def detect(rows):
    """Failed-breakout reversal: sweep range, close back inside, then confirmation."""
    if len(rows) < 35:
        return None
    closed = rows[:-1]  # ignore unfinished candle
    a, b, d = closed[-3], closed[-2], closed[-1]
    prior = closed[-27:-3]
    if len(prior) < 20:
        return None
    range_high = max(x["high"] for x in prior)
    range_low = min(x["low"] for x in prior)
    avg_range = sum(x["high"] - x["low"] for x in prior) / len(prior)
    if avg_range <= 0:
        return None

    # False breakout above range -> bearish reversal confirmation
    if (a["high"] > range_high and a["close"] > range_high
        and b["close"] < range_high and b["close"] < b["open"]
        and d["close"] < d["open"] and d["close"] < b["low"]
        and (a["high"] - range_high) >= avg_range * 0.05):
        entry = d["close"]
        sl = max(a["high"], b["high"], d["high"])
        risk = sl - entry
        if risk > 0 and risk / entry < 0.015:
            return {"side":"SELL","entry":entry,"sl":sl,
                    "tp":entry-RISK_REWARD*risk,"time":d["time"],
                    "setup":"Failed Breakout + Back Inside + Bearish Confirmation",
                    "level":range_high}

    # False breakout below range -> bullish reversal confirmation
    if (a["low"] < range_low and a["close"] < range_low
        and b["close"] > range_low and b["close"] > b["open"]
        and d["close"] > d["open"] and d["close"] > b["high"]
        and (range_low - a["low"]) >= avg_range * 0.05):
        entry = d["close"]
        sl = min(a["low"], b["low"], d["low"])
        risk = entry - sl
        if risk > 0 and risk / entry < 0.015:
            return {"side":"BUY","entry":entry,"sl":sl,
                    "tp":entry+RISK_REWARD*risk,"time":d["time"],
                    "setup":"Failed Breakout + Back Inside + Bullish Confirmation",
                    "level":range_low}
    return None

def price(x): return f"{x:.5f}"

def signal_text(market,s):
    return (f"📡 ICT SCANNER SIGNAL\n\nMarket: {market}\nDirection: {s['side']}\n"
            f"Setup: Failed Breakout + Back Inside + Confirmation\nTimeframe: {TF}\n"
            f"Signal time: {s['time'].strftime('%Y-%m-%d %I:%M:%S %p PKT')}\n"
            f"Alert sent: {now_pkt()}\n\nEntry: {price(s['entry'])}\n"
            f"TP: {price(s['tp'])}\nSL: {price(s['sl'])}\nRR: 1:{RISK_REWARD}\n\n"
            "⚠️ Gold uses GC=F futures as a proxy, not spot XAUUSD. No setup is trap-free; demo-test first.")

def monitor(market, trade, latest):
    if trade["side"]=="BUY":
        if latest["low"]<=trade["sl"]: return "SL"
        if latest["high"]>=trade["tp"]: return "TP"
    else:
        if latest["high"]>=trade["sl"]: return "SL"
        if latest["low"]<=trade["tp"]: return "TP"
    return None

def scan():
    for market,symbol in MARKETS.items():
        try:
            rows=candles(symbol)
            if len(rows)<30: continue
            if market in tracked:
                hit=monitor(market,tracked[market],rows[-1])
                if hit:
                    t=tracked.pop(market)
                    send(f"🔔 TRADE UPDATE — {market}\n{hit} HIT\nSide: {t['side']}\nEntry: {price(t['entry'])}\nTP: {price(t['tp'])}\nSL: {price(t['sl'])}\nChecked: {now_pkt()}")
            s=detect(rows)
            if s:
                key=f"{s['side']}:{s['time'].isoformat()}"
                if last_signal.get(market)==key:
                    continue
                # Persist before sending to avoid repeats after a restart.
                last_signal[market]=key
                try:
                    with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
                        json.dump(last_signal, f)
                except OSError as e:
                    print("Could not save signal history:", e)
                tracked[market]=s
                send(signal_text(market,s))
        except Exception as e:
            print(f"Scan error {market}: {e}")

def poll_commands():
    global last_update_id
    try:
        updates=tg("getUpdates", {"offset":last_update_id+1,"timeout":1})
        for u in updates.get("result",[]):
            last_update_id=u["update_id"]
            m=u.get("message",{}); txt=m.get("text","").strip().lower()
            chat=m.get("chat",{}).get("id")
            if not chat: continue
            if txt in ("/start","/help"):
                send("Welcome to ICT Scanner!\nCommands: /start /help /status /scan\nMarkets: Gold, BTCUSD, EURUSD, GBPUSD\n5m scanner; alerts include time, entry, TP and SL.",chat)
            elif txt=="/status":
                send(f"✅ Scanner online\nMarkets: {len(MARKETS)}\nTimeframe: {TF}\nTrades being monitored: {len(tracked)}\nPakistan time: {now_pkt()}",chat)
            elif txt=="/scan":
                send("🔎 Manual scan started; alerts will follow if a setup is detected.",chat)
                scan()
            else:
                send("Unknown command. Use /help",chat)
    except Exception as e:
        print("Telegram polling error:",e)

def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN environment variable missing. Add it to hosting secrets.")
    print("ICT Scanner started at",now_pkt())
    send("🚀 ICT Scanner online: Gold, BTCUSD, EURUSD, GBPUSD. Use /scan.")
    while True:
        poll_commands()
        scan()
        time.sleep(SCAN_SECONDS)

if __name__=="__main__":
    if os.getenv("GITHUB_ACTIONS", "").lower() == "true":
        # GitHub scheduled run: scan once and exit; Actions invokes this every 10 minutes.
        scan()
    else:
        # Local/hosted long-running bot: keep Telegram command polling active.
        main()
