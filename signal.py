#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""狙击策略 v2 —— 修正你上传的那版的所有致命问题

最关键的三个改动：
1. 只用【已收盘】5m K 线判形态，消灭"信号闪一下就永久入库"的幽灵单
2. 用【K 线高低点】回放判定止盈止损，不再靠"我巡逻时刚好看到什么价"
3. TP1 分批（平一半 + 推保本），TP2 留半仓跑，而不是 TP1 一到就整单结案

用法：
    python3 trade_bot_v2.py --once            # 跑一轮（cron 每 5 分钟）
    python3 trade_bot_v2.py --loop            # 常驻
    python3 trade_bot_v2.py --once --dry      # 不发 Telegram
    python3 trade_bot_v2.py --stats           # 只打印统计
    python3 trade_bot_v2.py                    # 自动兼容你原来的 trades.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import statistics
import time
from datetime import datetime, timedelta, timezone

import requests

BJ = timezone(timedelta(hours=8))            # 原版没 import timedelta，get_bj_time 一调用就炸
LOG = logging.getLogger("bot")
BASE = "https://www.okx.com"

CFG = {
    "token": os.getenv("TG_TOKEN", ""),
    "chat_id": os.getenv("TG_CHAT_ID", ""),
    "db": "trades.json",
    "report": "last_report.json",
    "max_open": 3,
    "risk_pct": 0.5,              # 单笔风险%（仅用于记账，不下单）
    "min_risk_pct": 0.35,         # 止损距离下限，低于这个手续费吃掉全部
    "max_risk_pct": 3.0,
    "vol_mult": 1.2,
    "partial_at": 1.5,            # TP1 = 1.5R，平一半并推保本
    "final_at": 2.5,              # TP2 = 2.5R，清仓
    "fee_bps": 5.0,               # 单边 taker 0.05%，双边计 10bps
    "cooldown_min": 60,           # 同币冷却
    "scan_top": 30,
    "min_turnover": 50_000_000,
    "htf_bar": "1H",
    "htf_ma": 20,
    "rate": 6.0,
}


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
class Limiter:
    def __init__(self, per_sec: float):
        self.rate, self.allow, self.last = per_sec, per_sec, time.monotonic()

    def take(self) -> None:
        while True:
            now = time.monotonic()
            self.allow = min(self.rate, self.allow + (now - self.last) * self.rate)
            self.last = now
            if self.allow >= 1:
                self.allow -= 1
                return
            time.sleep((1 - self.allow) / self.rate)


LIM = Limiter(CFG["rate"])
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "sniper/2.0"})


def get_json(path: str, params: dict, retries: int = 3) -> dict:
    for i in range(retries):
        LIM.take()
        try:
            r = SESSION.get(BASE + path, params=params, timeout=10)
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:            # noqa: BLE001
            if i == retries - 1:
                raise RuntimeError(f"{path} 失败: {e}") from e
            time.sleep(0.5 * (i + 1))
    return {}


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:               # noqa: BLE001
        LOG.error("读取 %s 失败(%s)，已备份为 .corrupt", path, e)
        try:
            shutil.copy(path, path + ".corrupt")
        except OSError:
            pass
        return default


def save_json(path: str, data) -> None:
    """原版直接覆盖写，进程被杀就整个库没了。改原子写。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def send_tg(text: str, dry: bool = False) -> bool:
    if dry or not CFG["token"] or not CFG["chat_id"]:
        LOG.info("[DRY] %s", text.replace("\n", " | "))
        return True
    for _ in range(3):
        try:
            r = SESSION.post(
                f"https://api.telegram.org/bot{CFG['token']}/sendMessage",
                json={"chat_id": CFG["chat_id"], "text": text,
                      "disable_web_page_preview": True},
                timeout=10)
            j = r.json()
            if j.get("ok"):
                return True
            wait = (j.get("parameters") or {}).get("retry_after", 2)
            LOG.warning("TG 拒绝: %s，%.0fs 后重试", j.get("description"), wait)
            time.sleep(float(wait))
        except Exception as e:            # noqa: BLE001
            LOG.warning("TG 异常: %s", e)
            time.sleep(1)
    return False


def fmt_price(p: float) -> str:
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}"


def bj_now() -> str:
    return datetime.now(BJ).strftime("%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 行情
# ---------------------------------------------------------------------------
def parse(rows) -> list[dict]:
    out = []
    for c in reversed(list(rows)):        # OKX 是新->旧，转成旧->新
        try:
            out.append({
                "ts": int(c[0]), "o": float(c[1]), "h": float(c[2]),
                "l": float(c[3]), "c": float(c[4]),
                "v": float(c[5]), "vq": float(c[7] or 0),
                "closed": str(c[8]) == "1",
            })
        except (IndexError, TypeError, ValueError):
            continue
    return out


def candles(inst: str, bar: str = "5m", limit: int = 200) -> list[dict]:
    j = get_json("/api/v5/market/candles",
                 {"instId": inst, "bar": bar, "limit": str(limit)})
    if str(j.get("code")) != "0":
        raise RuntimeError(j.get("msg") or "行情异常")
    return parse(j.get("data") or [])


def candles_since(inst: str, since_ms: int, bar: str = "5m") -> list[dict]:
    """从 since_ms 到现在、且已收盘的 K 线。老仓位自动翻历史页。"""
    got: list[dict] = []
    after = None
    for _ in range(12):
        params = {"instId": inst, "bar": bar, "limit": "100"}
        if after:
            params["after"] = str(after)
        path = "/api/v5/market/candles" if after is None else "/api/v5/market/history-candles"
        try:
            j = get_json(path, params)
        except Exception:                 # noqa: BLE001
            break
        rows = parse(j.get("data") or [])
        if not rows:
            break
        got = rows + got
        if rows[0]["ts"] <= since_ms:
            break
        after = rows[0]["ts"]
    return [c for c in got if c["closed"] and c["ts"] + 300_000 > since_ms]


def spot(inst: str) -> float | None:
    try:
        j = get_json("/api/v5/market/ticker", {"instId": inst})
        if str(j.get("code")) == "0" and j.get("data"):
            return float(j["data"][0]["last"])
    except Exception:                     # noqa: BLE001
        pass
    return None


def universe() -> list[str]:
    j = get_json("/api/v5/market/tickers", {"instType": "SWAP"})
    if str(j.get("code")) != "0":
        return []
    rows = []
    for t in j.get("data") or []:
        inst = t.get("instId", "")
        if not inst.endswith("USDT-SWAP"):
            continue
        try:
            turnover = float(t["volCcy24h"]) * float(t["last"])
        except (KeyError, TypeError, ValueError):
            continue
        if turnover >= CFG["min_turnover"]:
            rows.append((inst, turnover))
    rows.sort(key=lambda x: -x[1])
    return [i for i, _ in rows[:CFG["scan_top"]]]


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    g = l = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0.0)
        l += max(-d, 0.0)
    ag, al = g / period, l / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + ag / al)


def atr(cs: list[dict], period: int = 14) -> float | None:
    if len(cs) < period + 1:
        return None
    trs = [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]),
               abs(cs[i]["l"] - cs[i - 1]["c"])) for i in range(1, len(cs))]
    if len(trs) < period:
        return None
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def trend_1h(inst: str, cache: dict) -> str:
    """原版 bug：把当前【未收盘】的 1H K 线也算进 MA20，等于让现价自己拖平均数，
    判多空时被自己抵消一半，永远偏向 NEUTRAL。这里只用已收盘。"""
    hit = cache.get(inst)
    now = time.time()
    if hit and now - hit[0] < 600:
        return hit[1]
    try:
        cs = [c for c in candles(inst, CFG["htf_bar"], 120) if c["closed"]]
    except Exception:                     # noqa: BLE001
        cache[inst] = (now, "NEUTRAL")
        return "NEUTRAL"
    closes = [c["c"] for c in cs]
    if len(closes) < CFG["htf_ma"] + 5:
        cache[inst] = (now, "NEUTRAL")
        return "NEUTRAL"
    n = CFG["htf_ma"]
    ma_now = sum(closes[-n:]) / n
    ma_prev = sum(closes[-n - 5:-5]) / n
    price = closes[-1]
    if price > ma_now and ma_now > ma_prev:
        bias = "UP"
    elif price < ma_now and ma_now < ma_prev:
        bias = "DOWN"
    else:
        bias = "NEUTRAL"
    cache[inst] = (now, bias)
    return bias


# ---------------------------------------------------------------------------
# 形态（补上 body=0 和包住条件）
# ---------------------------------------------------------------------------
def body(c: dict) -> float:
    return abs(c["c"] - c["o"])


def rng(c: dict) -> float:
    return max(c["h"] - c["l"], 1e-12)


def bull_engulf(p: dict, c: dict) -> bool:
    return (p["c"] < p["o"] and c["c"] > c["o"]
            and c["o"] <= p["c"] and c["c"] >= p["o"]     # 原版漏掉这一条
            and body(c) > body(p))


def bear_engulf(p: dict, c: dict) -> bool:
    return (p["c"] > p["o"] and c["c"] < c["o"]
            and c["o"] >= p["c"] and c["c"] <= p["o"]
            and body(c) > body(p))


def hammer(c: dict) -> bool:
    b, r = body(c), rng(c)
    if b / r > 0.40 or b < r * 0.05:                       # body=0 时 b*2=0，原版必真
        return False
    lower = min(c["c"], c["o"]) - c["l"]
    upper = c["h"] - max(c["c"], c["o"])
    return lower >= b * 2 and lower >= r * 0.55 and upper <= b * 0.6


def shooting_star(c: dict) -> bool:
    b, r = body(c), rng(c)
    if b / r > 0.40 or b < r * 0.05:
        return False
    lower = min(c["c"], c["o"]) - c["l"]
    upper = c["h"] - max(c["c"], c["o"])
    return upper >= b * 2 and upper >= r * 0.55 and lower <= b * 0.6


# ---------------------------------------------------------------------------
# 持仓结算：用 K 线高低点回放，而不是"我巡逻那一刻的价"
# ---------------------------------------------------------------------------
def resolve(t: dict) -> dict | None:
    """幂等回放：每次都从 sl0 重新推演全部 K 线。

    为什么要从 sl0 开始？如果沿用"上一轮被推到保本的 sl"再从头回放，
    早于 TP1 的那些 K 线会立刻命中保本价，把还在跑的半仓误判成 BE 关单。
    回放必须是无状态的，结果才可复现。
    """
    opened = t["opened_ts"]
    try:
        cs = candles_since(t["symbol"], opened)
    except Exception as e:                 # noqa: BLE001
        LOG.warning("%s 回放取数失败: %s", t["symbol"], e)
        return None
    if not cs:
        return None

    long_ = t["direction"] == "LONG"
    entry = t["entry"]
    sl0 = float(t.get("sl0", t["sl"]))
    tp1, tp2 = t["tp1"], t["tp2"]
    risk0 = abs(entry - sl0) or 1e-12

    sl = sl0
    remaining = 1.0
    realized = 0.0
    tp1_done = False
    status = "OPEN"

    for c in cs:
        hit_sl = c["l"] <= sl if long_ else c["h"] >= sl
        hit_tp1 = (not tp1_done) and (c["h"] >= tp1 if long_ else c["l"] <= tp1)
        hit_tp2 = tp1_done and (c["h"] >= tp2 if long_ else c["l"] <= tp2)

        if hit_sl:                          # 同根 K 线双杀按止损算，保守
            r = (sl - entry) / risk0 if long_ else (entry - sl) / risk0
            realized += remaining * r
            remaining = 0.0
            status = "BE" if abs(r) < 1e-9 else "LOSS"
            break
        if hit_tp1:
            r1 = (tp1 - entry) / risk0 if long_ else (entry - tp1) / risk0
            realized += 0.5 * r1
            remaining = 0.5
            tp1_done = True
            sl = entry                      # 推保本，后续按保本判
            continue
        if hit_tp2:
            r2 = (tp2 - entry) / risk0 if long_ else (entry - tp2) / risk0
            realized += remaining * r2
            remaining = 0.0
            status = "WIN_TP2"
            break

    if remaining > 1e-9:
        t.update(remaining=remaining, realized_r=realized, sl=sl,
                 tp1_done=tp1_done,
                 status="OPEN_HALF" if tp1_done else "OPEN")
        return t if tp1_done else None

    t.update(remaining=0.0, realized_r=realized, tp1_done=tp1_done, sl=sl,
             status=status, closed_ts=int(time.time() * 1000),
             exit_px=(sl if status in ("LOSS", "BE")
                      else tp2 if status == "WIN_TP2" else tp1))
    return t


def review(trades: list[dict], dry: bool) -> tuple[list[dict], list[dict]]:
    active, closed = [], []
    for t in trades:
        if t.get("status", "OPEN") not in ("OPEN", "OPEN_HALF"):
            closed.append(t)
            continue
        before = t.get("status")
        upd = resolve(t)
        if upd is None:
            active.append(t)
            continue
        if upd["status"] in ("OPEN", "OPEN_HALF"):
            active.append(upd)
            if upd["status"] == "OPEN_HALF" and before != "OPEN_HALF":
                send_tg(f"🟡 TP1 已到 {t['symbol']}\n已平一半并推保本 {fmt_price(t['entry'])}\n"
                        f"剩余半仓目标 {fmt_price(t['tp2'])}", dry)
            continue
        closed.append(upd)
        emoji = {"WIN_TP2": "✅", "WIN_TP1": "🟡", "LOSS": "❌", "BE": "⚪"}[upd["status"]]
        send_tg(f"{emoji} 平仓 {t['symbol']} {t['direction']}\n"
                f"入场 {fmt_price(upd['entry'])} → 出场 {fmt_price(upd['exit_px'])}\n"
                f"本单 {upd['realized_r']:+.2f}R  ({upd['status']})\n{bj_now()}", dry)
    return active, closed


# ---------------------------------------------------------------------------
# 扫描开仓
# ---------------------------------------------------------------------------
def make_trade(inst: str, side: str, c: dict, atr_v: float, r_now: float) -> dict:
    entry = c["c"]
    if side == "LONG":
        sl = min(c["l"], entry - 1.0 * atr_v) * 0.999
        tp1 = entry + CFG["partial_at"] * abs(entry - sl)
        tp2 = entry + CFG["final_at"] * abs(entry - sl)
    else:
        sl = max(c["h"], entry + 1.0 * atr_v) * 1.001
        tp1 = entry - CFG["partial_at"] * abs(entry - sl)
        tp2 = entry - CFG["final_at"] * abs(entry - sl)
    risk = abs(entry - sl)
    return {
        "id": f"{inst}-{c['ts']}",
        "symbol": inst,
        "direction": side,
        "entry": entry,
        "sl0": sl,          # 原始止损，回放永远从它开始
        "sl": sl,           # 当前止损（TP1 后会推到保本）
        "tp1": tp1,
        "tp2": tp2,
        "opened_ts": c["ts"] + 300_000,
        "status": "OPEN",
        "remaining": 1.0,
        "realized_r": 0.0,
        "tp1_done": False,
        "rsi": r_now,
        "atr": atr_v,
        "risk_pct": risk / entry * 100,
    }


def scan(active: list[dict], state: dict, dry: bool) -> list[dict]:
    """原版 bug：alerts_sent 和 len(trades) 双重计数，
    MAX_OPEN=3 实际只能开 2 个。这里用统一的 slots 计数。"""
    slots = CFG["max_open"] - len(active)
    if slots <= 0:
        LOG.info("持仓已满 %d/%d", len(active), CFG["max_open"])
        return []

    open_syms = {t["symbol"] for t in active}
    cooldown_ms = CFG["cooldown_min"] * 60_000
    now_ms = int(time.time() * 1000)
    tcache: dict = state.setdefault("trend_cache", {})
    last_alert = state.setdefault("last_alert", {})

    new: list[dict] = []
    for inst in universe():
        if slots - len(new) <= 0:
            break
        if inst in open_syms:
            continue
        if now_ms - last_alert.get(inst, 0) < cooldown_ms:
            continue

        try:
            raw = candles(inst, "5m", 200)
        except Exception as e:            # noqa: BLE001
            LOG.debug("%s 取 K 线失败: %s", inst, e)
            continue

        cs = [c for c in raw if c["closed"]]        # ★ 核心修复
        if len(cs) < 60:
            continue

        cur, prev = cs[-1], cs[-2]
        closes = [c["c"] for c in cs]
        r_now = rsi(closes, 14)
        atr_v = atr(cs, 14)
        if r_now is None or not atr_v:
            continue

        hist = [c["vq"] for c in cs[-21:-1] if c["vq"] > 0]
        if len(hist) < 10:
            continue
        med = statistics.median(hist)
        vratio = cur["vq"] / med if med > 0 else 0

        if vratio < CFG["vol_mult"]:
            continue

        long_ok = (bull_engulf(prev, cur) or hammer(cur)) and r_now < 40
        short_ok = (bear_engulf(prev, cur) or shooting_star(cur)) and r_now > 60
        if not (long_ok or short_ok):
            continue

        # 形态先过，再查 1H 趋势（原版顺序反了，30 个币每轮白打 30 次请求）
        bias = trend_1h(inst, tcache)
        if long_ok and bias != "UP":
            continue
        if short_ok and bias != "DOWN":
            continue

        side = "LONG" if long_ok else "SHORT"
        t = make_trade(inst, side, cur, atr_v, r_now)
        if not (CFG["min_risk_pct"] <= t["risk_pct"] <= CFG["max_risk_pct"]):
            LOG.debug("%s 止损距离 %.2f%% 不在 [%.2f, %.2f]",
                      inst, t["risk_pct"], CFG["min_risk_pct"], CFG["max_risk_pct"])
            continue

        icon = "🎯 做多" if side == "LONG" else "🎯 做空"
        ok = send_tg(
            f"{icon} {inst}\n1H 趋势: {bias}\nRSI {r_now:.1f}  量比 {vratio:.1f}x\n"
            f"入场 {fmt_price(t['entry'])}\n止损 {fmt_price(t['sl'])} ({t['risk_pct']:.2f}%)\n"
            f"TP1 {fmt_price(t['tp1'])} (平一半+保本)\nTP2 {fmt_price(t['tp2'])}\n{bj_now()}",
            dry)
        if ok:
            new.append(t)
            last_alert[inst] = now_ms
            LOG.info("开仓 %s %s", inst, side)
    return new


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def stats(trades: list[dict]) -> str:
    done = [t for t in trades if t.get("status") not in ("OPEN", "OPEN_HALF")]
    opened = [t for t in trades if t.get("status") in ("OPEN", "OPEN_HALF")]
    if not done:
        return f"📊 累计平仓 0 单，当前持仓 {len(opened)}"

    # 手续费换算成 R：往返成本 2*fee_bps，除以该单的止损百分比
    fee_frac = CFG["fee_bps"] / 10_000 * 2
    net: list[float] = []
    for t in done:
        rp = float(t.get("risk_pct") or 1.0) / 100
        net.append(float(t.get("realized_r", 0.0)) - fee_frac / rp)

    gross_sum, net_sum = sum(float(t.get("realized_r", 0)) for t in done), sum(net)
    wins = [r for r in net if r > 0]
    return (
        f"📊 累计平仓 {len(done)} 单，当前持仓 {len(opened)}\n"
        f"胜率 {len(wins)/len(done)*100:.1f}%  "
        f"(TP2 {len([t for t in done if t['status']=='WIN_TP2'])} / "
        f"TP1 {len([t for t in done if t['status']=='WIN_TP1'])} / "
        f"止损 {len([t for t in done if t['status']=='LOSS'])} / "
        f"保本 {len([t for t in done if t['status']=='BE'])})\n"
        f"总收益 {gross_sum:+.2f}R   扣费后 {net_sum:+.2f}R\n"
        f"期望 {net_sum/len(done):+.3f}R/单"
    )


def report_due(state: dict) -> bool:
    now = int(time.time())
    if now - int(state.get("last_report_ts", 0)) >= 4 * 3600:
        state["last_report_ts"] = now
        return True
    return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def migrate(old: list[dict]) -> list[dict]:
    """兼容你原来的 trades.json。"""
    out = []
    for t in old:
        if "opened_ts" in t:
            out.append(t)
            continue
        try:
            ts = int(datetime.fromisoformat(t["time"]).timestamp() * 1000)
        except Exception:                 # noqa: BLE001
            ts = int(time.time() * 1000)
        t2 = dict(t)
        t2["opened_ts"] = ts
        t2.setdefault("sl0", t.get("sl"))
        t2["remaining"] = 0.0 if t.get("status") not in ("OPEN", "OPEN_HALF") else 1.0
        t2["realized_r"] = 0.0
        t2["tp1_done"] = t.get("status", "").startswith("WIN")
        out.append(t2)
    return out


def run_once(dry: bool) -> None:
    allt = migrate(load_json(CFG["db"], []))
    state = load_json(CFG["report"], {})

    active, closed = review(allt, dry)
    fresh = scan(active, state, dry)
    active.extend(fresh)

    final = closed + active
    # 归档：平仓单超过 500 条时挪走，避免文件无限膨胀
    if len(closed) > 500:
        save_json("trades_archive.json", closed[:-500])
        final = closed[-500:] + active

    save_json(CFG["db"], final)
    save_json(CFG["report"], state)

    LOG.info("持仓 %d / 上限 %d，本轮新开 %d", len(active), CFG["max_open"], len(fresh))
    if report_due(state):
        send_tg(stats(final), dry)
        save_json(CFG["report"], state)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if a.stats:
        print(stats(load_json(CFG["db"], [])))
        return 0

    if not a.loop:
        run_once(a.dry)
        return 0

    while True:
        try:
            run_once(a.dry)
        except KeyboardInterrupt:
            return 0
        except Exception as e:            # noqa: BLE001
            LOG.exception("异常: %s", e)
        time.sleep(max(10, 300 - int(time.time()) % 300) + 5)


if __name__ == "__main__":
    raise SystemExit(main())