"""
SMC/ICT live scanner: scans all forex, commodities and indices and sends a
WhatsApp (and optional SMS) alert ONLY when price first revisits an unmitigated
order block AND the candle confirms rejection in the bias direction.

Setup:
  pip install yfinance twilio
  export TWILIO_SID=ACxxxx TWILIO_TOKEN=xxxx
  export TWILIO_WA_FROM=whatsapp:+14155238886      # Twilio WhatsApp sandbox/sender
  export TWILIO_SMS_FROM=+1xxxxxxxxxx              # optional, enables SMS too
  export TO_NUMBER=+971559924508
  python smc_live_scanner.py
(Sandbox: first send "join <your-code>" from your WhatsApp to the Twilio sandbox number.)
Free Yahoo data is delayed/approximate; for execution-grade feeds swap fetch() for OANDA/MT5.
"""
import os, time, json, datetime as dt
import yfinance as yf
try:
    from twilio.rest import Client
except ImportError:
    Client = None

TO = os.getenv("TO_NUMBER", "+971559924508")
TF, PERIOD = os.getenv("TF", "1h"), "60d"        # 15m,1h,1d ... (4h: resample yourself)
MIN_SCORE = int(os.getenv("MIN_SCORE", "70"))     # confluence threshold
LOOP_SECONDS = 300
SENT_FILE = "sent_signals.json"

FX = ("EURUSD GBPUSD USDJPY USDCHF AUDUSD USDCAD NZDUSD EURGBP EURJPY GBPJPY AUDJPY EURAUD "
      "EURCHF GBPCHF CADJPY CHFJPY NZDJPY GBPAUD GBPCAD AUDCAD AUDNZD").split()
SYMS = {p: p + "=X" for p in FX}
SYMS.update({"XAUUSD": "GC=F", "XAGUSD": "SI=F", "USOIL": "CL=F", "UKOIL": "BZ=F", "NATGAS": "NG=F",
             "COPPER": "HG=F", "PLATINUM": "PL=F", "US30": "^DJI", "US500": "^GSPC", "NAS100": "^NDX",
             "GER40": "^GDAXI", "UK100": "^FTSE", "JP225": "^N225", "FRA40": "^FCHI", "HK50": "^HSI",
             "AUS200": "^AXJO", "DXY": "DX-Y.NYB"})

import urllib.request, urllib.parse
client = Client(os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"]) if (Client and os.getenv("TWILIO_SID")) else None

def fetch(tk):
    df = yf.download(tk, period=PERIOD, interval=TF, progress=False, auto_adjust=False)
    if df is None or len(df) < 60: return None
    if hasattr(df.columns, "levels"): df.columns = df.columns.get_level_values(0)
    return [dict(o=float(r.Open), h=float(r.High), l=float(r.Low), c=float(r.Close)) for r in df.itertuples()]

def pivots(c, n=3):
    H, L = {}, {}
    for i in range(n, len(c) - n):
        if all(c[i]["h"] > c[i-k]["h"] and c[i]["h"] > c[i+k]["h"] for k in range(1, n+1)): H[i] = c[i]["h"]
        if all(c[i]["l"] < c[i-k]["l"] and c[i]["l"] < c[i+k]["l"] for k in range(1, n+1)): L[i] = c[i]["l"]
    return H, L

def rsi(c, n=14):
    out, g, l = [50.0]*len(c), 0, 0
    for i in range(1, len(c)):
        d = c[i]["c"] - c[i-1]["c"]
        if i <= n: g += max(d, 0)/n; l += max(-d, 0)/n
        else: g = (g*(n-1)+max(d, 0))/n; l = (l*(n-1)+max(-d, 0))/n
        if i >= n: out[i] = 100 if l == 0 else 100 - 100/(1 + g/l)
    return out

def analyze(c, n=3):
    H, L = pivots(c, n); R = rsi(c)
    lH = lL = None; d = 0; obs = []
    for i in range(len(c)):
        if i-n in H: lH = [i-n, H[i-n], 0]
        if i-n in L: lL = [i-n, L[i-n], 0]
        if lH and not lH[2] and c[i]["c"] > lH[1]:
            lH[2] = 1; d = 1
            o = min(range(max(0, lH[0]-15), i+1), key=lambda k: c[k]["l"]); j = o
            while j > max(0, o-5) and not c[j]["c"] < c[j]["o"]: j -= 1
            if c[j]["c"] < c[j]["o"] and not any(x["i"] == j for x in obs):
                obs.append(dict(i=j, t="buy", hi=c[j]["h"], lo=c[j]["l"], bi=i))
        if lL and not lL[2] and c[i]["c"] < lL[1]:
            lL[2] = 1; d = -1
            o = max(range(max(0, lL[0]-15), i+1), key=lambda k: c[k]["h"]); j = o
            while j > max(0, o-5) and not c[j]["c"] > c[j]["o"]: j -= 1
            if c[j]["c"] > c[j]["o"] and not any(x["i"] == j for x in obs):
                obs.append(dict(i=j, t="sell", hi=c[j]["h"], lo=c[j]["l"], bi=i))
    return H, L, R, obs, d

def divergence(c, H, L, R, side):
    n = len(c); ks = sorted(L if side == "buy" else H)
    for a, b in zip(ks, ks[1:]):
        if b < n - 60 or not 5 <= b - a <= 60: continue
        if side == "buy" and L[b] < L[a] and R[b] > R[a] + 1: return True
        if side == "sell" and H[b] > H[a] and R[b] < R[a] - 1: return True
    return False

def signal(c):
    """Evaluate the last CLOSED candle for a first-touch revisit with confirmation."""
    c = c[:-1]                                   # drop forming candle
    H, L, R, obs, d = analyze(c)
    n = len(c) - 1; k = c[n]
    atr = sum(max(x["h"]-x["l"], abs(x["h"]-c[i-1]["c"]), abs(x["l"]-c[i-1]["c"]))
              for i, x in enumerate(c[-14:], len(c)-14)) / 14
    best = None
    for o in obs:
        prior = c[o["bi"]+1:n]
        if any(x["l"] <= o["hi"] and x["h"] >= o["lo"] for x in prior): continue   # already revisited/mitigated
        mid = (o["hi"] + o["lo"]) / 2; buy = o["t"] == "buy"
        touch = k["l"] <= o["hi"] and k["h"] >= o["lo"]
        confirm = (k["c"] > k["o"] and k["c"] > mid) if buy else (k["c"] < k["o"] and k["c"] < mid)
        if not (touch and confirm): continue
        score = 55 + (15 if d == (1 if buy else -1) else 0) + (20 if divergence(c, H, L, R, o["t"]) else 0) \
                   + (10 if (R[n] < 45 if buy else R[n] > 55) else 0)
        ent = k["c"]; sl = o["lo"] - atr*.25 if buy else o["hi"] + atr*.25; r = abs(ent - sl)
        if r <= 0: continue
        s = 1 if buy else -1
        tgt = [v for v in (H if buy else L).values() if (v > ent if buy else v < ent)]
        tp3 = (max(tgt) if buy else min(tgt)) if tgt else ent + s*r*4.5
        tp3 = max(tp3, ent + r*4.5) if buy else min(tp3, ent - r*4.5)
        cand = dict(side=o["t"].upper(), zone=(o["lo"], o["hi"]), ent=ent, sl=sl, tp1=ent+s*r*1.5,
                    tp2=ent+s*r*3, tp3=tp3, score=score, bar=o["bi"], rr=abs(tp3-ent)/r)
        if best is None or score > best["score"]: best = cand
    return best

def post(url, data, headers=None):
    try: urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers or {}), timeout=15)
    except Exception as e: print("send failed:", e)

def send(text):
    # EASIEST: ntfy push. Install the free "ntfy" app, subscribe to your secret topic, set NTFY_TOPIC.
    if os.getenv("NTFY_TOPIC"):
        post(f"https://ntfy.sh/{os.environ['NTFY_TOPIC']}", text.encode(), {"Title": "SMC signal", "Priority": "high"})
    # Telegram bot: set TG_TOKEN (from @BotFather) and TG_CHAT_ID.
    if os.getenv("TG_TOKEN") and os.getenv("TG_CHAT_ID"):
        post(f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
             urllib.parse.urlencode({"chat_id": os.environ["TG_CHAT_ID"], "text": text}).encode())
    if client is None: return
    for addr in [f"whatsapp:{TO}"] + ([TO] if os.getenv("TWILIO_SMS_FROM") else []):
        frm = os.environ["TWILIO_WA_FROM"] if addr.startswith("whatsapp") else os.environ["TWILIO_SMS_FROM"]
        try: client.messages.create(from_=frm, to=addr, body=text)
        except Exception as e: print("send failed:", e)

def main():
    sent = set(json.load(open(SENT_FILE))) if os.path.exists(SENT_FILE) else set()
    while True:
        found = []
        for name, tk in SYMS.items():
            try:
                c = fetch(tk)
                s = c and signal(c)
                if s and s["score"] >= MIN_SCORE: found.append((name, s))
            except Exception as e: print(name, "error:", e)
        for name, s in sorted(found, key=lambda x: -x[1]["score"]):      # best confirmed first, send all
            key = f"{name}|{TF}|{s['side']}|{round(s['zone'][0], 5)}"
            if key in sent: continue
            sent.add(key)
            f = lambda v: f"{v:.5f}" if v < 100 else f"{v:.2f}"
            send(f"INSTITUTIONAL ORDER BLOCK REVISIT\n{name} ({TF}) {s['side']}\n"
                 f"Zone {f(s['zone'][0])} - {f(s['zone'][1])}\nEntry {f(s['ent'])}\nSL {f(s['sl'])}\n"
                 f"TP1 {f(s['tp1'])}\nTP2 {f(s['tp2'])}\nTP3 {f(s['tp3'])} ({s['rr']:.1f}R)\n"
                 f"Confluence {s['score']}%\n{dt.datetime.now():%Y-%m-%d %H:%M}\nNot financial advice.")
        json.dump(sorted(sent), open(SENT_FILE, "w"))
        print(dt.datetime.now(), "scan done:", len(found), "confirmed")
        if os.getenv("RUN_ONCE"): break
        time.sleep(LOOP_SECONDS)

if __name__ == "__main__":
    main()
