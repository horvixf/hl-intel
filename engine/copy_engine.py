"""
HL Copy-Intel Engine (GitHub Actions production build)
Loop of cycles: snapshot tracked wallets -> diff -> events -> paper-trade
-> mark-to-market -> checkpoint -> periodic git commit+push.
Runs LOOP_MINUTES then exits cleanly; the workflow re-dispatches itself.

Paper strategy v2:
  copy side : elite OPEN/FLIP -> mirror; convergence>=2 -> 1.5x margin
  fade side : worst OPEN      -> reverse at half margin
  exits     : source closes | TP +5% px | stop -2.5% px | max hold 48h
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone

import requests

API = "https://api.hyperliquid.xyz/info"
DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STATE = os.path.join(DATA, "state.json")
EVENTS = os.path.join(DATA, "events.jsonl")
TRADES = os.path.join(DATA, "paper_trades.jsonl")
BOARD = os.path.join(DATA, "intel_board.json")

LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "330"))
CYCLE_SEC = int(os.environ.get("CYCLE_SEC", "300"))
COMMIT_EVERY = int(os.environ.get("COMMIT_EVERY", "3"))
GIT_PUSH = os.environ.get("GIT_PUSH", "1") == "1"

START_EQUITY = 100.0
LEV = 3.0
POS_FRAC = 0.30
CONV_BOOST = 1.5
FADE_FRAC = 0.15
MAX_POS = 4
FEE_MAKER = 0.00015
SLIP = 0.0003
TP_PX = 0.05
STOP_PX = 0.025
MAX_HOLD_H = 48
STALE_LIMIT = 5


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}",
          flush=True)


def post(payload, retries=4):
    for a in range(retries):
        try:
            r = requests.post(API, json=payload, timeout=20)
            if r.status_code == 429:
                time.sleep(2 ** a * 2)
                continue
            r.raise_for_status()
            time.sleep(0.12)
            return r.json()
        except Exception:
            if a == retries - 1:
                return None
            time.sleep(2 ** a)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state():
    if os.path.exists(STATE):
        st = json.load(open(STATE))
    else:
        st = {"snapshots": {}, "cycle": 0, "fail_counts": {},
              "paper": {"equity": START_EQUITY, "positions": {},
                        "realized": 0.0, "fees_paid": 0.0,
                        "wins": 0, "losses": 0},
              "equity_curve": []}
    board = json.load(open(BOARD))
    st["elite"] = [s["address"] for s in board["elite_screened"]]
    st["worst"] = [s["address"] for s in board["worst_screened"]]
    return st


def save_state(st):
    os.makedirs(DATA, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def snapshot_wallet(addr):
    st = post({"type": "clearinghouseState", "user": addr})
    if not st:
        return None
    out = {}
    for ap in st.get("assetPositions", []):
        p = ap["position"]
        szi = float(p["szi"])
        if szi == 0:
            continue
        out[p["coin"]] = {"side": "LONG" if szi > 0 else "SHORT",
                          "size": abs(szi),
                          "entry": float(p.get("entryPx") or 0),
                          "notional": abs(float(p.get("positionValue") or 0))}
    return out


def diff(addr, old, new, group, ts):
    events = []
    for coin, np_ in new.items():
        op = old.get(coin)
        if op is None:
            events.append({"t": ts, "addr": addr, "group": group,
                           "event": "OPEN", "coin": coin,
                           "side": np_["side"], "size": np_["size"],
                           "entry": np_["entry"],
                           "notional": np_["notional"]})
        elif op["side"] != np_["side"]:
            events.append({"t": ts, "addr": addr, "group": group,
                           "event": "FLIP", "coin": coin,
                           "side": np_["side"], "size": np_["size"]})
        elif np_["size"] > op["size"] * 1.05:
            events.append({"t": ts, "addr": addr, "group": group,
                           "event": "INCREASE", "coin": coin,
                           "side": np_["side"], "size": np_["size"]})
        elif np_["size"] < op["size"] * 0.95:
            events.append({"t": ts, "addr": addr, "group": group,
                           "event": "DECREASE", "coin": coin,
                           "side": np_["side"], "size": np_["size"]})
    for coin, op in old.items():
        if coin not in new:
            events.append({"t": ts, "addr": addr, "group": group,
                           "event": "CLOSE", "coin": coin,
                           "side": op["side"], "size": op["size"]})
    return events


def open_paper(paper, coin, side, mid, margin, src, tag, conv):
    notional = margin * LEV
    px = mid * (1 + SLIP) if side == "LONG" else mid * (1 - SLIP)
    fee = notional * FEE_MAKER
    paper["fees_paid"] += fee
    paper["equity"] -= fee
    paper["positions"][coin] = {"coin": coin, "side": side, "entry": px,
                                "notional": notional, "margin": margin,
                                "src": src, "opened": now_iso(),
                                "opened_ts": time.time(), "tag": tag,
                                "convergence": conv}
    append_jsonl(TRADES, {"t": now_iso(), "act": "OPEN", "tag": tag,
                          "coin": coin, "side": side, "px": px,
                          "notional": round(notional, 2),
                          "fee": round(fee, 4), "conv": conv,
                          "src": src[:10]})
    log(f"PAPER OPEN {tag} {side} {coin} ${notional:.0f} @ {px}")


def close_paper(paper, key, mids, reason):
    pos = paper["positions"].pop(key)
    mid = float(mids.get(pos["coin"], 0) or 0) or pos["entry"]
    px = mid * (1 - SLIP) if pos["side"] == "LONG" else mid * (1 + SLIP)
    ret = (px / pos["entry"] - 1) * (1 if pos["side"] == "LONG" else -1)
    pnl = pos["notional"] * ret
    fee = pos["notional"] * FEE_MAKER
    paper["equity"] += pnl - fee
    paper["realized"] += pnl
    paper["fees_paid"] += fee
    paper["wins" if pnl > 0 else "losses"] += 1
    append_jsonl(TRADES, {"t": now_iso(), "act": "CLOSE", "tag": pos["tag"],
                          "coin": pos["coin"], "side": pos["side"], "px": px,
                          "pnl": round(pnl, 4), "fee": round(fee, 4),
                          "reason": reason})
    log(f"PAPER CLOSE {pos['side']} {pos['coin']} pnl ${pnl:+.4f} ({reason})")


def paper_on_events(paper, events, mids, elite_positions):
    for ev in events:
        coin, mid = ev["coin"], float(mids.get(ev["coin"], 0) or 0)
        if mid <= 0:
            continue
        if ev["group"] == "elite" and ev["event"] in ("OPEN", "FLIP"):
            if coin in paper["positions"] or len(paper["positions"]) >= MAX_POS:
                continue
            conv = sum(1 for pos in elite_positions.values()
                       if pos.get(coin, {}).get("side") == ev["side"])
            margin = paper["equity"] * POS_FRAC * (CONV_BOOST if conv >= 2 else 1)
            open_paper(paper, coin, ev["side"], mid, margin,
                       ev["addr"], "copy", conv)
        elif ev["group"] == "worst" and ev["event"] == "OPEN":
            if coin in paper["positions"] or len(paper["positions"]) >= MAX_POS:
                continue
            fade_side = "SHORT" if ev["side"] == "LONG" else "LONG"
            margin = paper["equity"] * FADE_FRAC
            open_paper(paper, coin, fade_side, mid, margin,
                       ev["addr"], "fade", 0)
        elif (ev["event"] in ("CLOSE", "FLIP") and coin in paper["positions"]
              and paper["positions"][coin]["src"] == ev["addr"]):
            close_paper(paper, coin, mids, "src_closed")


def manage_exits(paper, mids):
    for key in list(paper["positions"].keys()):
        pos = paper["positions"][key]
        mid = float(mids.get(pos["coin"], 0) or 0)
        if mid <= 0:
            continue
        ret = (mid / pos["entry"] - 1) * (1 if pos["side"] == "LONG" else -1)
        held_h = (time.time() - pos.get("opened_ts", time.time())) / 3600
        if ret >= TP_PX:
            close_paper(paper, key, mids, "take_profit")
        elif ret <= -STOP_PX:
            close_paper(paper, key, mids, "stop_loss")
        elif held_h >= MAX_HOLD_H:
            close_paper(paper, key, mids, "max_hold")


def mark_to_market(paper, mids):
    upnl = 0.0
    for pos in paper["positions"].values():
        mid = float(mids.get(pos["coin"], 0) or 0)
        if mid <= 0:
            continue
        upnl += pos["notional"] * (mid / pos["entry"] - 1) * (
            1 if pos["side"] == "LONG" else -1)
    return paper["equity"] + upnl


def git_commit(msg):
    if not GIT_PUSH:
        return
    try:
        subprocess.run(["git", "add", "data"], check=True)
        r = subprocess.run(["git", "commit", "-m", msg],
                           capture_output=True, text=True)
        if "nothing to commit" in r.stdout + r.stderr:
            return
        subprocess.run(["git", "pull", "--rebase", "-X", "ours"], check=False)
        p = subprocess.run(["git", "push"], capture_output=True, text=True)
        if p.returncode != 0:
            log(f"git push failed: {p.stderr[:200]}")
        else:
            log("data committed + pushed")
    except Exception as e:
        log(f"git error (non-fatal): {e}")


def run_cycle(st):
    st["cycle"] += 1
    ts = now_iso()
    mids = post({"type": "allMids"}) or {}
    all_events, elite_positions = [], {}
    for group, addrs in (("elite", st["elite"]), ("worst", st["worst"])):
        for addr in addrs:
            snap = snapshot_wallet(addr)
            if snap is None:
                st["fail_counts"][addr] = st["fail_counts"].get(addr, 0) + 1
                if st["fail_counts"][addr] >= STALE_LIMIT:
                    log(f"STALE source frozen: {addr[:10]}..")
                continue
            st["fail_counts"][addr] = 0
            if group == "elite":
                elite_positions[addr] = snap
            old = st["snapshots"].get(addr)
            if old is not None:
                evs = diff(addr, old, snap, group, ts)
                for e in evs:
                    append_jsonl(EVENTS, e)
                all_events.extend(evs)
            st["snapshots"][addr] = snap
    if st["cycle"] > 1 and mids:
        paper_on_events(st["paper"], all_events, mids, elite_positions)
        manage_exits(st["paper"], mids)
    mtm = mark_to_market(st["paper"], mids) if mids else st["paper"]["equity"]
    st["equity_curve"].append({"t": ts, "cycle": st["cycle"],
                               "equity_mtm": round(mtm, 4)})
    save_state(st)
    log(f"cycle {st['cycle']} | events {len(all_events)} | "
        f"pos {len(st['paper']['positions'])} | mtm ${mtm:.4f} | "
        f"W/L {st['paper']['wins']}/{st['paper']['losses']}")
    return len(all_events)


def main():
    deadline = time.time() + LOOP_MINUTES * 60
    st = load_state()
    log(f"engine start: {len(st['elite'])} elite + {len(st['worst'])} worst "
        f"tracked, loop {LOOP_MINUTES}m, cycle {CYCLE_SEC}s")
    cycles_since_commit = 0
    while time.time() < deadline:
        try:
            run_cycle(st)
        except Exception as e:
            log(f"cycle error (continuing): {e}")
        cycles_since_commit += 1
        if cycles_since_commit >= COMMIT_EVERY:
            git_commit(f"engine data: cycle {st['cycle']}")
            cycles_since_commit = 0
        remaining = deadline - time.time()
        if remaining < CYCLE_SEC + 60:
            break
        time.sleep(CYCLE_SEC)
    git_commit(f"engine data: end of session, cycle {st['cycle']}")
    log("session complete, exiting for re-dispatch")


if __name__ == "__main__":
    main()
