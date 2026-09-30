"""
Pump.fun paper-trading bot  (v1)
================================
Reads live pump.fun launches and trades (via PumpPortal's free data feed),
filters out bundled / dev-heavy / concentrated launches, scores the rest on
traction + socials + narrative, and "trades" with FAKE SOL.

It never touches a wallet or private key. It cannot spend real money.

Run:
    python bot.py            live data, paper money
    python bot.py --sim      offline synthetic market, to check everything works

Logs go to ./logs:
    trades.csv      every paper buy / sell with P&L
    candidates.csv  every token the bot finished judging, with all its numbers
                    (this is the file you tune the filters from)
    summary.json    running scorecard
"""

import argparse
import asyncio
import base64
import hashlib
import struct
import csv
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

WS_URL = "wss://pumpportal.fun/api/data"           # new launches (free)
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TRADE_EVENT = hashlib.sha256(b"event:TradeEvent").digest()[:8]
DEFAULT_WSS = "wss://api.mainnet-beta.solana.com"  # free public Solana node
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(b) - len(b.lstrip(b"\0"))
    return "1" * pad + out


def decode_trade(data: bytes) -> Optional[dict]:
    """Decode a pump.fun TradeEvent (from 'Program data:' logs) into the same
    shape the rest of the bot uses. Only the stable leading fields are read."""
    if len(data) < 8 + 32 + 8 + 8 + 1 + 32 + 8 + 8 + 8 or data[:8] != TRADE_EVENT:
        return None
    o = 8
    mint = b58encode(data[o:o + 32]); o += 32
    sol, tok = struct.unpack_from("<QQ", data, o); o += 16
    is_buy = data[o] == 1; o += 1
    user = b58encode(data[o:o + 32]); o += 32
    o += 8  # timestamp
    vsol, vtok = struct.unpack_from("<QQ", data, o)
    vsol_f, vtok_f = vsol / 1e9, vtok / 1e6
    return {"txType": "buy" if is_buy else "sell", "mint": mint,
            "traderPublicKey": user, "solAmount": sol / 1e9,
            "tokenAmount": tok / 1e6, "vSolInBondingCurve": vsol_f,
            "vTokensInBondingCurve": vtok_f,
            "marketCapSol": (vsol_f / vtok_f * TOTAL_SUPPLY) if vtok_f else 0.0,
            "pool": "pump"}
TOTAL_SUPPLY = 1_000_000_000  # every pump.fun token has 1B supply
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_narratives(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip().lower()
            if line and not line.startswith("#") and line not in out:
                out.append(line)
    return out


# --------------------------------------------------------------------------
# Data holders
# --------------------------------------------------------------------------

@dataclass
class Token:
    mint: str
    name: str
    symbol: str
    creator: str
    uri: str
    created: float
    last_price: float          # SOL per token
    mcap_sol: float
    vsol: float
    vtok: float
    create_vsol: float
    last_trade: float
    prev_mcap_sol: float
    peak_mcap_sol: float
    balances: Dict[str, float] = field(default_factory=dict)
    early_buyers: Set[str] = field(default_factory=set)
    buyers: Set[str] = field(default_factory=set)
    sellers: Set[str] = field(default_factory=set)
    buys: int = 0
    sells: int = 0
    sol_in: float = 0.0
    sol_out: float = 0.0
    dev_tokens: float = 0.0
    dev_initial: float = 0.0
    dev_sold: bool = False
    unseen_sol: float = 0.0
    seen_first_trade: bool = False
    migrated: bool = False
    meta: Optional[dict] = None
    create_slot: int = 0            # Solana slot of the launch
    dev_buy_matched: bool = False   # dev's launch buy already counted from the create msg
    slot_buyers: Set[str] = field(default_factory=set)   # bought in the launch slot(s)
    early_sizes: List[float] = field(default_factory=list)
    twitter_handle: str = ""
    twitter_kind: str = ""          # none | profile | tweet | community
    fresh_top5: int = -1            # fresh wallets among top-5 holders (-1 = not checked)
    cp_idx: int = 0
    status: str = "watching"   # watching | bought | done


@dataclass
class Position:
    mint: str
    symbol: str
    entry_time: float
    entry_price: float
    tokens: float
    cost_sol: float
    score: float
    peak_price: float
    tp_hit: bool = False
    realized_sol: float = 0.0


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------

class Bot:
    def __init__(self, cfg: dict, narratives: List[str], sim: bool = False):
        self.cfg = cfg
        self.narratives = narratives
        self.sim = sim
        self.tokens: Dict[str, Token] = {}
        self.positions: Dict[str, Position] = {}
        self.sol_usd: float = float(cfg["fallback_sol_usd"])
        self.sol_price_live = False   # True once a real price was fetched
        self.balance_sol = 0.0
        self.start_balance_sol = 0.0
        self.closed: List[dict] = []
        self.ws = None
        self.session = None
        self.clock = 0.0            # virtual time in sim mode
        self.run_minutes = 0.0
        self.buy_cutoff = 0.0       # no new buys after this time (timed shifts)
        self.last_dash = 0.0
        self.helius_key = os.environ.get("HELIUS_API_KEY", "").strip()
        self.pp_key = os.environ.get("PUMPPORTAL_API_KEY", "").strip()
        self.rpc_wss = os.environ.get("SOLANA_WSS", "").strip() or DEFAULT_WSS
        self.rpc_http = os.environ.get("SOLANA_RPC", "").strip() or DEFAULT_RPC
        # trades for mints we haven't seen created yet (launch-block bundles
        # often arrive before the create message) - replayed on create
        self.pending: Dict[str, List[tuple]] = {}
        self.chain_msgs = 0
        self.chain_trades = 0
        # twitter handle -> mints that linked it (copycat / recycled-account detection)
        self.handles: Dict[str, List[str]] = {}
        self.stats = {"seen": 0, "rejected": 0, "dropped": 0, "bought": 0}
        self.reject_reasons: Dict[str, int] = {}
        os.makedirs(LOG_DIR, exist_ok=True)
        suffix = "_sim" if sim else ""
        self.trades_path = os.path.join(LOG_DIR, f"trades{suffix}.csv")
        self.cands_path = os.path.join(LOG_DIR, f"candidates{suffix}.csv")
        self.summary_path = os.path.join(LOG_DIR, f"summary{suffix}.json")
        self.state_path = os.path.join(LOG_DIR, f"state{suffix}.json")

    # ---------------- time / io helpers ----------------

    def now(self) -> float:
        return self.clock if self.sim else time.time()

    def init_balance(self) -> None:
        # Resume the paper account from earlier runs (live mode only)
        if not self.sim and os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    st = json.load(f)
                self.start_balance_sol = float(st["start_balance_sol"])
                self.balance_sol = float(st["balance_sol"])
                self.closed = st.get("closed", [])
                self.stats.update(st.get("stats", {}))
                self.reject_reasons = st.get("reject_reasons", {})
                self.handles = st.get("handles", {})
                if not self.sol_price_live and st.get("sol_usd"):
                    # All price sources down: last known price beats the stale config fallback
                    self.sol_usd = float(st["sol_usd"])
                    log(f"Using last known SOL price ${self.sol_usd:.2f}")
                log(f"Resumed paper account: {self.balance_sol:.3f} SOL, "
                    f"{len(self.closed)} past trades")
                return
            except Exception as e:
                log(f"Could not resume state ({e}); starting fresh")
        self.start_balance_sol = float(self.cfg["start_balance_usd"]) / self.sol_usd
        self.balance_sol = self.start_balance_sol
        log(f"Paper balance: {self.balance_sol:.3f} SOL (~${self.cfg['start_balance_usd']}) "
            f"at SOL=${self.sol_usd:.2f}")

    async def send(self, obj: dict) -> None:
        if self.sim or self.ws is None:
            return
        try:
            await self.ws.send(json.dumps(obj))
        except Exception:
            pass

    def fire(self, obj: dict) -> None:
        if not self.sim and self.ws is not None:
            asyncio.create_task(self.send(obj))

    def append_csv(self, path: str, row: dict) -> None:
        if os.path.exists(path):
            with open(path, "r", newline="", encoding="utf-8") as f:
                header = next(csv.reader(f), [])
            missing = [k for k in row if k not in header]
            if missing:
                # new columns: rewrite the file once with the wider header
                with open(path, "r", newline="", encoding="utf-8") as f:
                    old = list(csv.DictReader(f))
                header = header + missing
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=header)
                    w.writeheader()
                    w.writerows(old)
            with open(path, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=header, extrasaction="ignore").writerow(row)
        else:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(row.keys()))
                w.writeheader()
                w.writerow(row)

    def stamp(self) -> str:
        t = self.now()
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))

    # ---------------- network extras (live only) ----------------

    # Free SOL/USD sources, tried in order. CoinGecko often returns 403 to
    # GitHub Actions IPs, so there are exchange-ticker backups.
    SOL_PRICE_SOURCES = [
        ("coingecko", "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
         lambda d: d["solana"]["usd"]),
        ("coinbase", "https://api.coinbase.com/v2/prices/SOL-USD/spot",
         lambda d: d["data"]["amount"]),
        ("kraken", "https://api.kraken.com/0/public/Ticker?pair=SOLUSD",
         lambda d: next(iter(d["result"].values()))["c"][0]),
        ("binance", "https://api.binance.us/api/v3/ticker/price?symbol=SOLUSDT",
         lambda d: d["price"]),
    ]

    async def refresh_sol_price(self) -> None:
        if self.sim or self.session is None:
            return
        errors = []
        for name, url, pick in self.SOL_PRICE_SOURCES:
            try:
                async with self.session.get(url, timeout=10) as r:
                    data = await r.json(content_type=None)
                    px = float(pick(data))
                if px > 0:
                    self.sol_usd = px
                    self.sol_price_live = True
                    return
            except Exception as e:
                errors.append(f"{name}: {e}")
        log(f"SOL price fetch failed ({'; '.join(errors)}); using ${self.sol_usd:.2f}")

    async def price_updater(self) -> None:
        while True:
            await asyncio.sleep(600)
            await self.refresh_sol_price()

    async def fetch_meta(self, t: Token) -> None:
        if not t.uri or self.session is None:
            t.meta = {}
            return
        try:
            async with self.session.get(t.uri, timeout=8) as r:
                t.meta = await r.json(content_type=None)
        except Exception:
            t.meta = {}
        self.note_twitter(t)

    FAMOUS = {"elonmusk", "realdonaldtrump", "potus", "whitehouse", "cz_binance", "binance",
              "coinbase", "pumpdotfun", "solana", "vitalikbuterin", "saylor", "aeyakovenko",
              "rajgokal", "jupiterexchange", "phantom", "dexscreener", "kanyewest", "ye",
              "barackobama", "nasa", "openai", "sama", "tesla", "spacex", "x", "twitter"}
    NOT_HANDLES = {"i", "intent", "home", "search", "hashtag", "share", "explore"}

    def note_twitter(self, t: Token) -> None:
        """Read the token's X/Twitter link: what kind it is, and whether the
        same account has been attached to other launches (a recycled or copied
        account is a common rug sign)."""
        tw = str((t.meta or {}).get("twitter") or "").strip()
        if not tw:
            t.twitter_kind = "none"
            return
        m = re.search(r"(?:twitter|x)\.com/i/communities/(\d+)", tw, re.I)
        if m:
            t.twitter_kind, t.twitter_handle = "community", "community:" + m.group(1)
        else:
            m = re.search(r"(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})(/status/\d+)?", tw, re.I)
            if not m or m.group(1).lower() in self.NOT_HANDLES:
                t.twitter_kind = "other"
                return
            t.twitter_handle = m.group(1).lower()
            t.twitter_kind = "tweet" if m.group(2) else "profile"
        mints = self.handles.setdefault(t.twitter_handle, [])
        if t.mint not in mints:
            mints.append(t.mint)
            del mints[:-20]

    async def fresh_wallet_check(self, t: Token) -> None:
        """How many of the 5 biggest non-dev holders are brand-new wallets.
        Bundlers usually fund fresh wallets right before launch."""
        if self.sim or self.session is None:
            return
        top = sorted(((b, w) for w, b in t.balances.items() if w != t.creator and b > 0),
                     reverse=True)[:5]
        limit = int(self.cfg.get("fresh_wallet_tx_limit", 20))
        fresh = 0
        for _, w in top:
            try:
                payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                           "params": [w, {"limit": limit}]}
                async with self.session.post(self.rpc_http, json=payload, timeout=8) as r:
                    data = await r.json()
                if len(data.get("result") or []) < limit:
                    fresh += 1
            except Exception:
                return          # don't guess on RPC trouble
        t.fresh_top5 = fresh

    async def holder_check(self, t: Token):
        """Optional: real top-holder concentration from chain (needs HELIUS_API_KEY).
        Catches bundle wallets the live feed missed."""
        if self.sim or self.session is None:
            return True, ""
        try:
            url = (f"https://mainnet.helius-rpc.com/?api-key={self.helius_key}"
                   if self.helius_key else self.rpc_http)
            payload = {"jsonrpc": "2.0", "id": 1,
                       "method": "getTokenLargestAccounts", "params": [t.mint]}
            async with self.session.post(url, json=payload, timeout=8) as r:
                data = await r.json()
            vals = sorted((float(v.get("uiAmount") or 0) for v in data["result"]["value"]),
                          reverse=True)
            # largest account is the bonding curve itself - skip it
            top10 = sum(vals[1:11]) / TOTAL_SUPPLY * 100
            if top10 > self.cfg["max_top10_pct"]:
                return False, f"on-chain top10 holders {top10:.0f}%"
        except Exception as e:
            log(f"holder check failed for {t.symbol}: {e} (not blocking)")
        return True, ""

    # ---------------- feed handling ----------------

    async def handle(self, data: dict) -> None:
        tx = data.get("txType")
        if tx == "create":
            await self.on_create(data)
        elif tx in ("buy", "sell"):
            self.on_trade(data)

    async def on_create(self, d: dict) -> None:
        if len(self.tokens) >= self.cfg["max_watching"]:
            return
        mint = d.get("mint")
        if not mint or mint in self.tokens:
            return
        vsol = float(d.get("vSolInBondingCurve") or 0)
        vtok = float(d.get("vTokensInBondingCurve") or 0)
        mcap = float(d.get("marketCapSol") or 0)
        now = self.now()
        t = Token(
            mint=mint,
            name=str(d.get("name", ""))[:40],
            symbol=str(d.get("symbol", ""))[:20],
            creator=d.get("traderPublicKey", ""),
            uri=d.get("uri", "") or "",
            created=now,
            last_price=(vsol / vtok) if vtok else 0.0,
            mcap_sol=mcap,
            vsol=vsol,
            vtok=vtok,
            create_vsol=vsol,
            last_trade=now,
            prev_mcap_sol=mcap,
            peak_mcap_sol=mcap,
        )
        dev_buy = float(d.get("initialBuy") or 0)
        t.dev_initial = t.dev_tokens = dev_buy
        if dev_buy > 0:
            t.balances[t.creator] = dev_buy
        if "meta" in d:              # sim mode injects metadata directly
            t.meta = d["meta"]
            self.note_twitter(t)
        self.tokens[mint] = t
        self.stats["seen"] += 1
        if self.pp_key:
            await self.send({"method": "subscribeTokenTrade", "keys": [mint]})
        for _, ev in self.pending.pop(mint, []):
            self.on_trade(ev)
        if t.meta is None:
            asyncio.create_task(self.fetch_meta(t))

    def on_trade(self, d: dict) -> None:
        t = self.tokens.get(d.get("mint"))
        if t is None:
            return
        now = self.now()
        tx = d["txType"]
        trader = d.get("traderPublicKey", "")
        tok = float(d.get("tokenAmount") or 0)
        sol = float(d.get("solAmount") or 0)
        vsol = float(d.get("vSolInBondingCurve") or 0)
        vtok = float(d.get("vTokensInBondingCurve") or 0)

        # Bundle proxy: SOL that entered the curve in trades we never saw
        # (same-block buys land before our subscription goes live).
        if not t.seen_first_trade:
            t.seen_first_trade = True
            pre = vsol - sol if tx == "buy" else vsol + sol
            if now - t.created <= 10:
                t.unseen_sol = max(0.0, pre - t.create_vsol)

        if vtok > 0:
            t.last_price = vsol / vtok
            t.vsol, t.vtok = vsol, vtok
        mc = d.get("marketCapSol")
        if mc:
            t.mcap_sol = float(mc)
        t.peak_mcap_sol = max(t.peak_mcap_sol, t.mcap_sol)
        t.last_trade = now

        slot = int(d.get("slot") or 0)
        # The dev's launch buy shows up twice (in the create message and as an
        # on-chain trade). Count it once, and use it to learn the launch slot.
        if (tx == "buy" and trader == t.creator and not t.dev_buy_matched
                and t.dev_initial > 0 and abs(tok - t.dev_initial) <= 0.02 * t.dev_initial):
            t.dev_buy_matched = True
            if slot:
                t.create_slot = slot
            return
        if slot and not t.create_slot:
            t.create_slot = slot
        if (tx == "buy" and trader != t.creator and slot and t.create_slot
                and slot <= t.create_slot + int(self.cfg.get("bundle_slot_window", 1))):
            t.slot_buyers.add(trader)
            t.early_sizes.append(sol)

        if tx == "buy":
            t.buys += 1
            t.sol_in += sol
            t.buyers.add(trader)
            if trader != t.creator and now - t.created <= self.cfg["bundle_window_seconds"]:
                t.early_buyers.add(trader)
        else:
            t.sells += 1
            t.sol_out += sol
            t.sellers.add(trader)
            if trader == t.creator:
                t.dev_sold = True

        nb = d.get("newTokenBalance")
        if nb is not None:
            t.balances[trader] = float(nb)
        else:
            delta = tok if tx == "buy" else -tok
            t.balances[trader] = max(0.0, t.balances.get(trader, 0.0) + delta)
        if trader == t.creator:
            t.dev_tokens = t.balances.get(trader, 0.0)

        pool = d.get("pool")
        if pool and pool != "pump":
            t.migrated = True

        if t.mint in self.positions:
            self.check_exit(t)

    # ---------------- judging ----------------

    def features(self, t: Token) -> dict:
        now = self.now()
        socials = t.meta or {}
        tw = str(socials.get("twitter") or "")
        text = f"{t.name} {t.symbol} {socials.get('description') or ''}".lower()
        words = re.findall(r"[a-z0-9]+", text)
        hits = [k for k in self.narratives
                if (" " in k and k in text) or any(w.startswith(k) for w in words)]
        top10 = sum(sorted(t.balances.values(), reverse=True)[:10]) / TOTAL_SUPPLY * 100
        early_pct = sum(t.balances.get(w, 0.0) for w in t.early_buyers) / TOTAL_SUPPLY * 100
        growth = (t.mcap_sol / t.prev_mcap_sol - 1) * 100 if t.prev_mcap_sol else 0.0
        slot_pct = sum(t.balances.get(w, 0.0) for w in t.slot_buyers) / TOTAL_SUPPLY * 100
        sizes = sorted(t.early_sizes)
        twins = sum(1 for i, x in enumerate(sizes)
                    if any(abs(x - y) <= 0.01 * max(x, 1e-9) for j, y in enumerate(sizes) if j != i))
        reuse = len([m for m in self.handles.get(t.twitter_handle, []) if m != t.mint]) \
            if t.twitter_handle else 0
        return {
            "age_s": round(now - t.created),
            "mcap_usd": round(t.mcap_sol * self.sol_usd),
            "growth_pct": round(growth, 1),
            "unique_buyers": len(t.buyers),
            "buys": t.buys,
            "sells": t.sells,
            "net_sol": round(t.sol_in - t.sol_out, 3),
            "dev_hold_pct": round(t.dev_tokens / TOTAL_SUPPLY * 100, 2),
            "dev_sold": t.dev_sold,
            "early_wallets": len(t.early_buyers),
            "early_pct": round(early_pct, 2),
            "top10_pct": round(top10, 2),
            "unseen_sol": round(t.unseen_sol, 3),
            "twitter": bool(tw),
            "tweet_link": "/status/" in tw,
            "telegram": bool(socials.get("telegram")),
            "website": bool(socials.get("website")),
            "narrative": "|".join(hits),
            "same_slot_buyers": len(t.slot_buyers),
            "same_slot_pct": round(slot_pct, 2),
            "twin_buys": twins,
            "fresh_top5": t.fresh_top5,
            "twitter_kind": t.twitter_kind or ("none" if t.meta is not None else ""),
            "twitter_handle": t.twitter_handle,
            "handle_reuse": reuse,
            "twitter_famous": t.twitter_handle in self.FAMOUS,
        }

    def score(self, f: dict) -> float:
        s = 0.0
        s += min(25.0, f["unique_buyers"] * 1.0)                             # traction
        ratio = f["buys"] / max(1, f["sells"])
        s += min(20.0, max(0.0, (ratio - 1) * 10))                           # buy pressure
        s += min(15.0, max(0.0, f["net_sol"] * 3))                           # money in
        s += min(15.0, max(0.0, f["growth_pct"] / 4))                        # momentum
        s += (7 if f["twitter"] else 0) + (3 if f["telegram"] else 0) + (5 if f["website"] else 0)
        s += 10 if f["narrative"] else 0                                     # narrative
        return round(s, 1)

    def decide(self, t: Token, f: dict):
        c = self.cfg
        if f["dev_sold"] and c["reject_if_dev_sells"]:
            return "reject", 0, "dev sold"
        if f["dev_hold_pct"] > c["max_dev_hold_pct"]:
            return "reject", 0, f"dev holds {f['dev_hold_pct']:.1f}%"
        if f["early_wallets"] >= c["bundle_max_early_wallets"] and f["early_pct"] >= c["bundle_max_early_pct"]:
            return "reject", 0, f"bundled: {f['early_wallets']} launch wallets own {f['early_pct']:.0f}%"
        if f["unseen_sol"] >= c["bundle_max_unseen_sol"]:
            return "reject", 0, f"bundled: {f['unseen_sol']:.1f} SOL bought in launch block"
        if f["top10_pct"] > c["max_top10_pct"]:
            return "reject", 0, f"top10 hold {f['top10_pct']:.0f}%"
        if f["mcap_usd"] > c["max_mcap_usd"]:
            return "reject", 0, "mcap too high (late)"
        if f["age_s"] >= 60 and f["unique_buyers"] < 3:
            return "reject", 0, "no traction"
        if c["require_socials"]:
            if t.meta is None:
                return "wait", 0, "metadata loading"
            if not (f["twitter"] or f["telegram"] or f["website"]):
                return "reject", 0, "no socials"
        s = self.score(f)
        for rule, why in self.extra_rules(f):
            if c.get("enforce_" + rule, False):
                return "reject", 0, why
        if f["mcap_usd"] < c["min_mcap_usd"]:
            return "wait", s, "mcap below min"
        if f["unique_buyers"] < c["min_unique_buyers"]:
            return "wait", s, "too few buyers"
        if s >= c["buy_score"]:
            if f["age_s"] < c.get("min_buy_age_seconds", 0):
                return "wait", s, "too early (launch spike)"
            if f["age_s"] > c.get("max_buy_age_seconds", 10**9):
                return "wait", s, "past buy window (late mover)"
            return "buy", s, "passed"
        return "wait", s, "score below threshold"

    def extra_rules(self, f: dict):
        """Newer anti-bundle / Twitter checks. Each one only rejects when its
        enforce_<name> switch is true in config.json; otherwise it is recorded
        in the shadow_flags column so the nightly review can see whether it
        would have avoided losing trades before it gets switched on."""
        c = self.cfg
        out = []
        if (f["same_slot_buyers"] >= c.get("max_same_slot_buyers", 3)
                or f["same_slot_pct"] >= c.get("max_same_slot_pct", 10)):
            out.append(("same_slot", f"bundled: {f['same_slot_buyers']} wallets in launch block "
                                     f"own {f['same_slot_pct']:.0f}%"))
        if f["twin_buys"] >= c.get("max_twin_buys", 3):
            out.append(("twin_buys", f"bundled: {f['twin_buys']} identical-size launch buys"))
        if f["fresh_top5"] >= c.get("max_fresh_top5", 3):
            out.append(("fresh_wallets", f"bundled: {f['fresh_top5']} of top 5 holders are fresh wallets"))
        if f["handle_reuse"] >= c.get("max_handle_reuse", 1):
            out.append(("twitter_reuse", f"twitter account reused by {f['handle_reuse']} other launches"))
        if f["twitter_famous"]:
            out.append(("twitter_famous", "twitter link points at a famous account (fake association)"))
        return out

    async def evaluate(self, t: Token, final: bool) -> None:
        f = self.features(t)
        decision, s, reason = self.decide(t, f)
        if decision == "buy":
            ok, why = await self.holder_check(t)
            if not ok:
                decision, reason = "reject", why
        if decision == "buy":
            await self.fresh_wallet_check(t)
            f = self.features(t)
            decision, s, reason = self.decide(t, f)
        if decision == "buy":
            if self.buy_cutoff and self.now() >= self.buy_cutoff:
                decision, reason = "wait", "shift ending"
            elif len(self.positions) >= self.cfg["max_open_positions"]:
                decision, reason = "wait", "max positions open"
            elif self.balance_sol * self.cfg["position_pct"] < self.cfg["min_trade_sol"]:
                decision, reason = "wait", "balance too low"
            else:
                self.open_position(t, s)
        if decision == "wait" and final:
            decision = "drop"
        t.prev_mcap_sol = t.mcap_sol
        if decision != "wait":
            row = {"time": self.stamp(), "mint": t.mint, "symbol": t.symbol,
                   "decision": decision, "score": s, "reason": reason}
            row.update(f)
            row["shadow_flags"] = "|".join(r for r, _ in self.extra_rules(f))
            row["link"] = f"https://pump.fun/coin/{t.mint}"
            self.append_csv(self.cands_path, row)
        if decision == "reject":
            self.stats["rejected"] += 1
            key = reason.split(":")[0].split(" ")[0] if reason.startswith(("bundled", "dev", "top10")) else reason
            self.reject_reasons[key] = self.reject_reasons.get(key, 0) + 1
            self.forget(t)
        elif decision == "drop":
            self.stats["dropped"] += 1
            self.forget(t)

    def forget(self, t: Token) -> None:
        t.status = "done"
        self.tokens.pop(t.mint, None)
        if self.pp_key:
            self.fire({"method": "unsubscribeTokenTrade", "keys": [t.mint]})

    def on_chain_trade(self, ev: dict) -> None:
        mint = ev["mint"]
        if mint in self.tokens:
            self.on_trade(ev)
        elif len(self.pending) < 20000:
            self.pending.setdefault(mint, []).append((self.now(), ev))

    def prune_pending(self) -> None:
        cutoff = self.now() - 20
        for m in list(self.pending):
            if self.pending[m][-1][0] < cutoff:
                del self.pending[m]

    # ---------------- paper execution ----------------
    # Fills use the real bonding-curve math, so a big order pays real price
    # impact. slippage_pct is an extra haircut for latency (price moving
    # before your transaction lands).

    def curve_buy(self, t: Token, sol_net: float) -> float:
        if t.vsol <= 0 or t.vtok <= 0:
            return sol_net / t.last_price if t.last_price else 0.0
        k = t.vsol * t.vtok
        return t.vtok - k / (t.vsol + sol_net)

    def curve_sell(self, t: Token, tokens: float) -> float:
        if t.vsol <= 0 or t.vtok <= 0:
            return tokens * t.last_price
        k = t.vsol * t.vtok
        return t.vsol - k / (t.vtok + tokens)

    def open_position(self, t: Token, s: float) -> None:
        c = self.cfg
        size = min(self.balance_sol * c["position_pct"], c["max_position_sol"])
        fee = size * c["fee_pct"]
        net = size - fee - c["priority_fee_sol"]
        if net <= 0 or t.last_price <= 0:
            return
        tokens = self.curve_buy(t, net) * (1 - c["slippage_pct"])
        entry = size / tokens
        self.balance_sol -= size
        self.positions[t.mint] = Position(t.mint, t.symbol, self.now(), entry, tokens, size, s, t.last_price)
        t.status = "bought"
        self.stats["bought"] += 1
        mcap_usd = t.mcap_sol * self.sol_usd
        log(f"BUY  {t.symbol:<10} score {s:>5}  mcap ${mcap_usd:,.0f}  size {size:.3f} SOL")
        self.append_csv(self.trades_path, {
            "time": self.stamp(), "mint": t.mint, "symbol": t.symbol, "action": "BUY",
            "reason": f"score {s}", "price": f"{entry:.12f}", "sol": round(size, 5),
            "pnl_sol": "", "pnl_pct": "", "mcap_usd": round(mcap_usd),
            "balance_sol": round(self.balance_sol, 5)})

    def sell(self, t: Token, p: Position, fraction: float, reason: str) -> None:
        c = self.cfg
        qty = p.tokens * fraction
        gross = self.curve_sell(t, qty) * (1 - c["slippage_pct"])
        px = gross / qty if qty else 0.0
        proceeds = max(0.0, gross - gross * c["fee_pct"] - c["priority_fee_sol"])
        p.tokens -= qty
        p.realized_sol += proceeds
        self.balance_sol += proceeds
        closing = fraction >= 0.999 or p.tokens <= 0
        pnl = p.realized_sol - p.cost_sol if closing else ""
        pnl_pct = round((p.realized_sol / p.cost_sol - 1) * 100, 1) if closing else ""
        log(f"SELL {p.symbol:<10} {int(fraction*100):>3}%  {reason:<22}"
            + (f" P&L {pnl:+.4f} SOL ({pnl_pct:+}%)" if closing else ""))
        self.append_csv(self.trades_path, {
            "time": self.stamp(), "mint": p.mint, "symbol": p.symbol,
            "action": "SELL" if closing else "SELL_PART", "reason": reason,
            "price": f"{px:.12f}", "sol": round(proceeds, 5),
            "pnl_sol": round(pnl, 5) if closing else "", "pnl_pct": pnl_pct,
            "mcap_usd": round(t.mcap_sol * self.sol_usd),
            "balance_sol": round(self.balance_sol, 5)})
        if closing:
            self.closed.append({"symbol": p.symbol, "pnl_sol": pnl, "pnl_pct": pnl_pct,
                                "reason": reason, "held_s": round(self.now() - p.entry_time)})
            self.positions.pop(p.mint, None)
            self.forget(t)

    def check_exit(self, t: Token) -> None:
        p = self.positions.get(t.mint)
        if p is None:
            return
        c = self.cfg
        now = self.now()
        price = t.last_price
        p.peak_price = max(p.peak_price, price)
        # value if we sold everything right now, vs what we paid
        exit_val = self.curve_sell(t, p.tokens) * (1 - c["slippage_pct"]) + p.realized_sol
        change = (exit_val / p.cost_sol - 1) * 100 if not p.tp_hit else (price / p.entry_price - 1) * 100
        if t.dev_sold and c["exit_if_dev_sells"]:
            return self.sell(t, p, 1.0, "dev sold")
        if t.migrated and c["exit_on_migration"]:
            return self.sell(t, p, 1.0, "migrated off curve")
        if not p.tp_hit and change >= c["take_profit_pct"]:
            p.tp_hit = True
            self.sell(t, p, c["take_profit_sell_fraction"], f"take profit +{change:.0f}%")
            if t.mint not in self.positions:
                return
        if not p.tp_hit and change <= -c["stop_loss_pct"]:
            return self.sell(t, p, 1.0, f"stop loss {change:.0f}%")
        if p.tp_hit and price <= p.peak_price * (1 - c["trailing_stop_pct"] / 100):
            return self.sell(t, p, 1.0, "trailing stop")
        if now - p.entry_time >= c["max_hold_seconds"]:
            return self.sell(t, p, 1.0, "time limit")
        if now - t.last_trade >= c["stale_seconds"]:
            return self.sell(t, p, 1.0, "no trading (stale)")

    # ---------------- loop ----------------

    async def tick(self) -> None:
        now = self.now()
        cps = self.cfg["checkpoints_seconds"]
        for t in list(self.tokens.values()):
            if t.status == "watching":
                if t.cp_idx < len(cps) and now - t.created >= cps[t.cp_idx]:
                    t.cp_idx += 1
                    await self.evaluate(t, final=(t.cp_idx == len(cps)))
            elif t.status == "bought":
                self.check_exit(t)
        every = 1800 if self.sim else self.cfg["dashboard_seconds"]
        if not self.sim:
            self.prune_pending()
        if now - self.last_dash >= every:
            self.last_dash = now
            self.dashboard()

    def equity(self) -> float:
        held = 0.0
        for p in self.positions.values():
            t = self.tokens.get(p.mint)
            if t:
                held += self.curve_sell(t, p.tokens) * (1 - self.cfg["slippage_pct"])
        return self.balance_sol + held

    def dashboard(self) -> None:
        eq = self.equity()
        wins = [x for x in self.closed if x["pnl_sol"] > 0]
        n = len(self.closed)
        wr = (len(wins) / n * 100) if n else 0.0
        ret = (eq / self.start_balance_sol - 1) * 100 if self.start_balance_sol else 0.0
        log(f"--- equity {eq:.3f} SOL (${eq*self.sol_usd:,.2f}, {ret:+.1f}%) | "
            f"open {len(self.positions)} | closed {n} | win rate {wr:.0f}% | "
            f"watching {len(self.tokens)} | seen {self.stats['seen']} "
            f"rejected {self.stats['rejected']} bought {self.stats['bought']}"
            + ("" if self.sim else f" | chain trades {self.chain_trades}"))
        summary = {
            "updated": self.stamp(), "sol_usd": self.sol_usd,
            "start_balance_sol": round(self.start_balance_sol, 5),
            "equity_sol": round(eq, 5), "equity_usd": round(eq * self.sol_usd, 2),
            "return_pct": round(ret, 2), "closed_trades": n, "wins": len(wins),
            "win_rate_pct": round(wr, 1),
            "best_trade_pct": max((x["pnl_pct"] for x in self.closed), default=None),
            "worst_trade_pct": min((x["pnl_pct"] for x in self.closed), default=None),
            "tokens_seen": self.stats["seen"], "rejected": self.stats["rejected"],
            "dropped": self.stats["dropped"], "bought": self.stats["bought"],
            "reject_reasons": dict(sorted(self.reject_reasons.items(), key=lambda kv: -kv[1])),
        }
        with open(self.summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        self.save_state()

    def save_state(self) -> None:
        if self.sim:
            return
        st = {"start_balance_sol": self.start_balance_sol, "balance_sol": self.balance_sol,
              "closed": self.closed, "stats": self.stats,
              "reject_reasons": self.reject_reasons, "sol_usd": self.sol_usd,
              "handles": dict(list(self.handles.items())[-3000:])}
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def log_equity(self) -> None:
        """One row per finished shift - feeds the phone dashboard's chart."""
        if self.sim:
            return
        eq = self.equity()
        n = len(self.closed)
        wins = sum(1 for x in self.closed if x["pnl_sol"] > 0)
        self.append_csv(os.path.join(LOG_DIR, "equity.csv"), {
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "equity_sol": round(eq, 5), "equity_usd": round(eq * self.sol_usd, 2),
            "return_pct": round((eq / self.start_balance_sol - 1) * 100, 2),
            "closed_trades": n, "win_rate_pct": round(wins / n * 100, 1) if n else 0,
            "tokens_seen": self.stats["seen"]})

    def close_all(self, reason: str) -> None:
        for p in list(self.positions.values()):
            t = self.tokens.get(p.mint)
            if t:
                self.sell(t, p, 1.0, reason)

    async def ticker(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(1)

    async def ws_loop(self) -> None:
        import websockets
        backoff = 1
        while True:
            try:
                url = WS_URL + (f"?api-key={self.pp_key}" if self.pp_key else "")
                async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                              max_size=None) as ws:
                    self.ws = ws
                    backoff = 1
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    keys = list(self.tokens.keys())
                    if keys:
                        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": keys}))
                    log("Connected to live pump.fun feed")
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        if isinstance(data, dict):
                            await self.handle(data)
            except Exception as e:
                self.ws = None
                log(f"Feed disconnected ({e}); reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def chain_loop(self) -> None:
        """Every pump.fun trade, read straight from Solana program logs."""
        import websockets
        backoff = 1
        sub = {"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
               "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "processed"}]}
        while True:
            try:
                async with websockets.connect(self.rpc_wss, ping_interval=20, ping_timeout=30,
                                              max_size=None) as ws:
                    await ws.send(json.dumps(sub))
                    backoff = 1
                    log("Connected to Solana trade stream")
                    async for raw in ws:
                        self.chain_msgs += 1
                        try:
                            msg = json.loads(raw)
                            val = msg["params"]["result"]["value"]
                            slot = int(msg["params"]["result"].get("context", {}).get("slot") or 0)
                        except Exception:
                            if "error" in str(raw)[:200]:
                                log(f"Solana node said: {str(raw)[:200]}")
                            continue
                        if val.get("err"):
                            continue
                        for line in val.get("logs") or []:
                            if not line.startswith("Program data: "):
                                continue
                            try:
                                ev = decode_trade(base64.b64decode(line[14:]))
                            except Exception:
                                ev = None
                            if ev:
                                ev["slot"] = slot
                                self.chain_trades += 1
                                self.on_chain_trade(ev)
            except Exception as e:
                log(f"Solana stream disconnected ({e}); reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def run_live(self) -> None:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            self.session = session
            await self.refresh_sol_price()
            self.init_balance()
            if self.pp_key:
                log("Trade data: PumpPortal (paid key)")
            else:
                log(f"Trade data: Solana logs via {self.rpc_wss.split('?')[0]}")
            log("Holder check: " + ("Helius" if self.helius_key else self.rpc_http.split('?')[0]))
            log(f"Narrative keywords loaded: {len(self.narratives)}")
            loops = [self.ws_loop(), self.ticker(), self.price_updater()]
            if not self.pp_key:
                loops.append(self.chain_loop())
            tasks = [asyncio.create_task(x) for x in loops]
            if self.run_minutes:
                log(f"Running for {self.run_minutes} minutes")
                self.buy_cutoff = time.time() + max(0.0, self.run_minutes - 3) * 60
                await asyncio.sleep(self.run_minutes * 60)
                self.close_all("shift ended")
                self.dashboard()
                self.log_equity()
                log("Shift over. Open positions closed at market (paper).")
                for x in tasks:
                    x.cancel()
            else:
                await asyncio.gather(*tasks)


# --------------------------------------------------------------------------
# Offline simulator: fake market built on the real bonding-curve math.
# It exists to test the plumbing. Its results say NOTHING about real edge.
# --------------------------------------------------------------------------

class SimMarket:
    NAMES = ["cat", "dog", "frog", "pepe", "ai agent", "moon", "trump", "chad", "goblin",
             "rocket", "bonk", "quant", "wizard", "based", "sigma", "hamster"]

    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.coins = []
        self.n = 0

    def wallet(self) -> str:
        return "W" + "".join(self.rng.choice("abcdef0123456789") for _ in range(10))

    @staticmethod
    def buy(c, sol):
        k = c["vsol"] * c["vtok"]
        out = c["vtok"] - k / (c["vsol"] + sol)
        c["vsol"] += sol
        c["vtok"] -= out
        return out

    @staticmethod
    def sell(c, tokens):
        k = c["vsol"] * c["vtok"]
        out = c["vsol"] - k / (c["vtok"] + tokens)
        c["vtok"] += tokens
        c["vsol"] -= out
        return out

    def ev(self, c, tx, wallet, tok, sol):
        return {"txType": tx, "mint": c["mint"], "traderPublicKey": wallet,
                "tokenAmount": tok, "solAmount": sol,
                "newTokenBalance": c["bal"].get(wallet, 0.0),
                "vSolInBondingCurve": c["vsol"], "vTokensInBondingCurve": c["vtok"],
                "marketCapSol": c["vsol"] / c["vtok"] * TOTAL_SUPPLY, "pool": "pump"}

    def new_coin(self, now):
        r = self.rng.random()
        # Rough, pessimistic mix. "fakeout" = looks like a runner (wash buys,
        # socials) then insiders dump - the most common trap on pump.fun.
        kind = ("dead" if r < 0.55 else "bundle" if r < 0.68 else "devdump" if r < 0.78
                else "fakeout" if r < 0.90 else "runner" if r < 0.95 else "chop")
        self.n += 1
        name = self.rng.choice(self.NAMES)
        c = {"mint": f"SIM{self.n:06d}", "kind": kind, "born": now, "vsol": 30.0,
             "vtok": 1_073_000_191.0, "bal": {}, "dev": self.wallet(),
             "life": self.rng.uniform(90, 900), "holders": []}
        dev_sol = self.rng.uniform(0.2, 1.5) if kind != "devdump" else self.rng.uniform(1, 3)
        dev_tok = self.buy(c, dev_sol)
        c["bal"][c["dev"]] = dev_tok
        socials_p = {"runner": 0.75, "chop": 0.5, "devdump": 0.6, "bundle": 0.5,
                     "dead": 0.3, "fakeout": 0.8}[kind]
        meta = {}
        if self.rng.random() < socials_p:
            meta = {"twitter": "https://x.com/example", "website": "https://example.com"
                    if self.rng.random() < 0.5 else "", "telegram": "https://t.me/x"
                    if self.rng.random() < 0.5 else ""}
        create = {"txType": "create", "mint": c["mint"], "traderPublicKey": c["dev"],
                  "initialBuy": dev_tok, "solAmount": dev_sol, "name": name.title(),
                  "symbol": name.upper().replace(" ", "")[:6], "uri": "", "meta": meta,
                  "vSolInBondingCurve": c["vsol"], "vTokensInBondingCurve": c["vtok"],
                  "marketCapSol": c["vsol"] / c["vtok"] * TOTAL_SUPPLY}
        events = [create]
        if kind == "bundle":
            # half the time the bundle lands in the launch block (unseen),
            # half the time we see it as a burst of early buys
            hidden = self.rng.random() < 0.5
            for _ in range(self.rng.randint(4, 9)):
                w = self.wallet()
                sol = self.rng.uniform(0.5, 2.0)
                tok = self.buy(c, sol)
                c["bal"][w] = tok
                c["holders"].append(w)
                if not hidden:
                    events.append(self.ev(c, "buy", w, tok, sol))
        self.coins.append(c)
        return events

    def step(self, now):
        events = []
        if self.rng.random() < 0.3:
            events += self.new_coin(now)
        alive = []
        for c in self.coins:
            age = now - c["born"]
            if age > c["life"] + 300:
                continue
            alive.append(c)
            k, life = c["kind"], c["life"]
            p_buy, p_sell, bmax = 0.0, 0.0, 0.5
            if k == "dead":
                p_buy, p_sell = (0.05, 0.03) if age < 40 else (0.0, 0.0)
            elif k == "runner":
                p_buy, p_sell, bmax = (0.7, 0.35, 1.5) if age < life else (0.15, 0.8, 1.0)
            elif k == "fakeout":
                peak = min(life, self.rng.uniform(60, 200)) if "peak" not in c else c["peak"]
                c["peak"] = peak
                if age < peak:
                    p_buy, p_sell, bmax = (0.75, 0.2, 1.2)
                else:
                    p_buy, p_sell, bmax = (0.05, 0.9, 0.3)
                    if c["holders"] and self.rng.random() < 0.5:
                        w = self.rng.choice(c["holders"])
                        tok = c["bal"].get(w, 0.0)
                        if tok > 0:
                            sol = self.sell(c, tok)
                            c["bal"][w] = 0.0
                            events.append(self.ev(c, "sell", w, tok, sol))
            elif k == "devdump":
                p_buy, p_sell, bmax = (0.7, 0.25, 1.2) if age < life * 0.4 else (0.05, 0.4, 0.5)
                if age >= life * 0.4 and c["bal"].get(c["dev"], 0) > 0:
                    tok = c["bal"][c["dev"]]
                    sol = self.sell(c, tok)
                    c["bal"][c["dev"]] = 0.0
                    events.append(self.ev(c, "sell", c["dev"], tok, sol))
            elif k == "bundle":
                p_buy, p_sell, bmax = (0.5, 0.3, 0.8) if age < 120 else (0.05, 0.3, 0.3)
                if age > 60 and c["holders"] and self.rng.random() < 0.3:
                    w = c["holders"].pop()
                    tok = c["bal"].get(w, 0)
                    if tok > 0:
                        sol = self.sell(c, tok)
                        c["bal"][w] = 0.0
                        events.append(self.ev(c, "sell", w, tok, sol))
            elif k == "chop":
                p_buy, p_sell, bmax = (0.5, 0.5, 0.8) if age < life else (0.0, 0.0, 0.5)
            if self.rng.random() < p_buy:
                w = self.wallet() if self.rng.random() < 0.8 or not c["holders"] else self.rng.choice(c["holders"])
                sol = self.rng.uniform(0.05, bmax)
                tok = self.buy(c, sol)
                c["bal"][w] = c["bal"].get(w, 0.0) + tok
                if w not in c["holders"]:
                    c["holders"].append(w)
                events.append(self.ev(c, "buy", w, tok, sol))
            if self.rng.random() < p_sell and c["holders"]:
                w = self.rng.choice(c["holders"])
                held = c["bal"].get(w, 0.0)
                if held > 0:
                    tok = held * self.rng.uniform(0.3, 1.0)
                    sol = self.sell(c, tok)
                    c["bal"][w] = held - tok
                    events.append(self.ev(c, "sell", w, tok, sol))
        self.coins = alive
        return events


async def run_sim(bot: Bot, hours: float) -> None:
    bot.clock = time.time()
    bot.init_balance()
    market = SimMarket()
    end = bot.clock + hours * 3600
    log(f"Simulating {hours} hours of a FAKE market...")
    while bot.clock < end:
        for e in market.step(bot.clock):
            await bot.handle(e)
        await bot.tick()
        bot.clock += 1.0
    bot.dashboard()
    log("Simulation done. Check logs/trades_sim.csv and logs/candidates_sim.csv")


def main() -> None:
    ap = argparse.ArgumentParser(description="pump.fun paper-trading bot")
    ap.add_argument("--sim", action="store_true", help="run offline on a fake market")
    ap.add_argument("--hours", type=float, default=3.0, help="sim length in simulated hours")
    ap.add_argument("--minutes", type=float, default=0,
                    help="live mode: stop after this many minutes (0 = run forever)")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    args = ap.parse_args()
    cfg = load_config(args.config)
    narratives = load_narratives(os.path.join(HERE, "narratives.txt"))
    bot = Bot(cfg, narratives, sim=args.sim)
    bot.run_minutes = args.minutes
    try:
        if args.sim:
            asyncio.run(run_sim(bot, args.hours))
        else:
            asyncio.run(bot.run_live())
    except (KeyboardInterrupt, asyncio.CancelledError):
        bot.close_all("stopped")
        bot.dashboard()
        log("Stopped.")


if __name__ == "__main__":
    main()
