"""
Hyperliquid Universe Intel Suite v2
Stage 1: pull global leaderboard (~40k wallets), screen locally with
         consistency-first scoring (fixes v1 lucky-trade flaw).
Stage 2: deep-scan elite + worst wallets: live positions, resting orders,
         TP/SL levels, entry prices.
Stage 3: aggregate into a smart-money vs dumb-money board per coin.
"""

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

API = "https://api.hyperliquid.xyz/info"
LB_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
OUT = "data_tmp"
import pathlib
pathlib.Path("data").mkdir(exist_ok=True)
LB_CACHE = os.path.join(OUT, "leaderboard_raw.json")

TOP_N_DEEP = 12          # elite wallets to deep-scan
BOT_N_DEEP = 8           # consistent losers to deep-scan
MIN_EQ, MAX_EQ = 10_000, 10_000_000
MIN_MONTH_VLM = 1_000_000   # real activity, not idle luck


def post(payload, retries=4):
    for a in range(retries):
        try:
            r = requests.post(API, json=payload, timeout=20)
            if r.status_code == 429:
                time.sleep(2 ** a * 2)
                continue
            r.raise_for_status()
            time.sleep(0.15)
            return r.json()
        except Exception:
            if a == retries - 1:
                return None
            time.sleep(2 ** a)


def get_leaderboard():
    if os.path.exists(LB_CACHE):
        print("Using cached leaderboard")
        return json.load(open(LB_CACHE))["leaderboardRows"]
    r = requests.get(LB_URL, timeout=120)
    r.raise_for_status()
    d = r.json()
    os.makedirs(OUT, exist_ok=True)
    with open(LB_CACHE + ".tmp", "w") as f:
        json.dump(d, f)
    os.replace(LB_CACHE + ".tmp", LB_CACHE)
    return d["leaderboardRows"]


def screen(rows):
    """Consistency-first score. Windows: day/week/month/allTime."""
    scored = []
    for row in rows:
        try:
            eq = float(row["accountValue"])
            if not (MIN_EQ <= eq <= MAX_EQ):
                continue
            w = {k: {"pnl": float(v["pnl"]), "roi": float(v["roi"]),
                     "vlm": float(v["vlm"])}
                 for k, v in row["windowPerformances"]}
            if w["month"]["vlm"] < MIN_MONTH_VLM:
                continue
            d_roi, wk_roi, m_roi = (w["day"]["roi"], w["week"]["roi"],
                                    w["month"]["roi"])
            at_pnl = w["allTime"]["pnl"]
            # consistency: how many windows positive (incl. allTime)
            pos_windows = sum(x > 0 for x in
                              [d_roi, wk_roi, m_roi, at_pnl])
            # weekly roi should not dwarf month (single-spike detector)
            spike = abs(wk_roi) > 0.85 * abs(m_roi) and abs(m_roi) > 0.10
            score = (
                100 * min(m_roi, 2.0)
                + 60 * min(wk_roi, 1.0)
                + 10 * pos_windows
                - (25 if spike else 0)
            )
            scored.append({
                "address": row["ethAddress"], "equity": round(eq),
                "roi_day": round(d_roi * 100, 2),
                "roi_week": round(wk_roi * 100, 2),
                "roi_month": round(m_roi * 100, 2),
                "pnl_month": round(w["month"]["pnl"]),
                "pnl_alltime": round(at_pnl),
                "vlm_month": round(w["month"]["vlm"]),
                "pos_windows": pos_windows,
                "spike_flag": spike,
                "score": round(score, 1),
            })
        except Exception:
            continue
    scored.sort(key=lambda r: -r["score"])
    return scored


def deep_scan(addr):
    """Live positions + resting orders (targets/stops) for one wallet."""
    st = post({"type": "clearinghouseState", "user": addr})
    oo = post({"type": "frontendOpenOrders", "user": addr}) or []
    out = {"address": addr, "positions": [], "orders": []}
    if st:
        out["equity"] = round(float(st["marginSummary"]["accountValue"]))
        for ap in st.get("assetPositions", []):
            p = ap["position"]
            szi = float(p["szi"])
            if szi == 0:
                continue
            out["positions"].append({
                "coin": p["coin"],
                "side": "LONG" if szi > 0 else "SHORT",
                "size": abs(szi),
                "entry": float(p.get("entryPx") or 0),
                "upnl": round(float(p.get("unrealizedPnl") or 0)),
                "lev": p.get("leverage", {}).get("value"),
                "liq_px": float(p["liquidationPx"]) if p.get("liquidationPx") else None,
                "notional": round(abs(float(p.get("positionValue") or 0))),
            })
    for o in oo:
        out["orders"].append({
            "coin": o["coin"], "side": o["side"],
            "px": float(o["limitPx"]) if o.get("limitPx") else None,
            "trigger_px": float(o["triggerPx"]) if o.get("triggerPx") and o["triggerPx"] != "0.0" else None,
            "type": o.get("orderType"), "tpsl": o.get("isPositionTpsl"),
            "reduce_only": o.get("reduceOnly"), "sz": o.get("sz"),
        })
    return out


def aggregate_board(scans, label):
    """Net smart/dumb-money exposure per coin."""
    agg = defaultdict(lambda: {"long_notional": 0, "short_notional": 0,
                               "holders_long": 0, "holders_short": 0})
    for s in scans:
        for p in s.get("positions", []):
            a = agg[p["coin"]]
            if p["side"] == "LONG":
                a["long_notional"] += p["notional"]
                a["holders_long"] += 1
            else:
                a["short_notional"] += p["notional"]
                a["holders_short"] += 1
    board = []
    for coin, a in agg.items():
        net = a["long_notional"] - a["short_notional"]
        board.append({"coin": coin, "group": label,
                      "net_notional": net,
                      "long_notional": a["long_notional"],
                      "short_notional": a["short_notional"],
                      "n_long": a["holders_long"],
                      "n_short": a["holders_short"]})
    board.sort(key=lambda b: -abs(b["net_notional"]))
    return board


def main():
    os.makedirs(OUT, exist_ok=True)
    rows = get_leaderboard()
    print(f"Universe: {len(rows)} wallets")
    scored = screen(rows)
    print(f"Passed screen (eq {MIN_EQ}-{MAX_EQ}, vlm>{MIN_MONTH_VLM/1e6:.0f}M): "
          f"{len(scored)}")

    elite = [s for s in scored if s["pos_windows"] == 4
             and not s["spike_flag"]][:TOP_N_DEEP]
    worst = [s for s in scored
             if s["roi_day"] < 0 and s["roi_week"] < 0
             and s["roi_month"] < 0][-BOT_N_DEEP:]

    with open("data/screened_top500.json", "w") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(),
                   "screened": scored[:500]}, f, indent=1)

    print("\nDeep-scanning", len(elite), "elite +", len(worst), "worst wallets")
    elite_scans = [deep_scan(s["address"]) for s in elite]
    worst_scans = [deep_scan(s["address"]) for s in worst]

    smart = aggregate_board(elite_scans, "smart")
    dumb = aggregate_board(worst_scans, "dumb")

    result = {"generated": datetime.now(timezone.utc).isoformat(),
              "elite_screened": elite, "worst_screened": worst,
              "elite_scans": elite_scans, "worst_scans": worst_scans,
              "smart_board": smart, "dumb_board": dumb}
    with open(os.path.join("data/intel_board.json"), "w") as f:
        json.dump(result, f, indent=1)
    print("Saved:", os.path.join("data/intel_board.json"))


if __name__ == "__main__":
    main()
