#!/usr/bin/env python3
"""ATEX <-> SKYNT bridge operator daemon.

The node operator (Lux) runs this. It performs the off-chain legs:

  1. EXCAL -> ATEX : watch the Excalibur node for EXCAL paid to the operator's
     lock script -> inscribe an ATEX `transfer` to the user's BTC address.
     The user's BTC address comes from the claims registry (see claim.py):
     a lock with no claim is reported and skipped, never guessed.
  2. ATEX -> SKYNT : watch the BRC-20 indexer for ATEX `transfer` inscriptions
     to the operator's BTC address -> call AtexBridge.lockAtex.
  3. SKYNT -> ATEX : watch AtexBridge.SkyntBurned events -> inscribe an ATEX
     `transfer` to the user's BTC address.
  4. ATEX -> EXCAL : ATEX sent to the operator's BTC address with a claim
     naming an Excalibur lock script -> release EXCAL via sendtoaddress.

Secrets: OPERATOR_BTC_WIF_FILE / OPERATOR_ETH_KEY_FILE (0600). Never logged.

State: sqlite (daemon_state.db) — processed ids, watermarks, claims.
Restarts never double-process.

Usage:
  python3 daemon.py [--once] [--dry-run]
"""
import argparse
import logging
import sys
import time
import traceback

from config import Config, ConfigError
from state import State
from btc_wallet import BtcWallet, WalletError
from inscribe import Inscriber, InscribeError
from indexer import make_indexer
from excal import ExcalClient, ExcalError

log = logging.getLogger("atex-operator")

BRD = 10**18  # ATEX/SKYNT base units per whole token


def atex_transfer_json(amount_units: int) -> bytes:
    """BRC-20 transfer inscription body for `amount_units` base units.
    The amt decimal string is built with exact integer math (18dp)."""
    import json
    if amount_units <= 0:
        raise ValueError("amount must be positive")
    whole, frac = divmod(amount_units, BRD)
    amt = str(whole) if frac == 0 else \
        f"{whole}.{str(frac).zfill(18).rstrip('0')}"
    return json.dumps({"p": "brc-20", "op": "transfer", "tick": "ATEX",
                       "amt": amt}).encode()


# ---------------------------------------------------------------- flows
class Operator:
    def __init__(self, cfg: Config, state: State):
        self.cfg = cfg
        self.state = state
        self.btc = None
        self.inscriber = None
        self.eth = None
        self.excal = None
        self.indexer = None

        if cfg.flow_excal_to_atex() or cfg.flow_skynt_to_atex():
            self.btc = BtcWallet(cfg.btc_wif, cfg.btc_api, cfg.confirmations)
            if self.btc.address != cfg.operator_btc_address:
                raise WalletError(
                    f"BTC WIF derives {self.btc.address} != "
                    f"OPERATOR_BTC_ADDRESS {cfg.operator_btc_address} — "
                    "refusing to run")
            self.inscriber = Inscriber(self.btc)
            log.info("BTC wallet ready: %s", self.btc.address)

        if cfg.flow_atex_to_skynt() or cfg.flow_skynt_to_atex():
            from eth_operator import EthOperator  # needs web3
            self.eth = EthOperator(cfg.eth_rpc, cfg.bridge_contract,
                                   cfg.eth_key, cfg.max_fee_gwei)
            log.info("ETH operator ready: %s", self.eth.acct.address)

        if cfg.flow_excal_to_atex() or cfg.flow_atex_to_excal():
            self.excal = ExcalClient(cfg.excal_rpc)
            h = self.excal.height()
            log.info("Excalibur node at height %d", h)

        if cfg.flow_atex_to_skynt() or cfg.flow_atex_to_excal():
            self.indexer = make_indexer(cfg.indexer_url, cfg.indexer_key)

    # -- 1. EXCAL -> ATEX ------------------------------------------------
    def check_excal_locks(self):
        if not self.cfg.flow_excal_to_atex():
            return
        for u in self.excal.utxos_for_lock(self.cfg.operator_excal_lock):
            tid = f"{u['txid']}:{u['vout']}"
            if self.state.seen("excal_lock", tid):
                continue
            if not u["mature"]:
                continue  # wait for coinbase maturity
            claim = self.state.get_claim(f"excal:{u['txid']}")
            if not claim or not claim.get("btc_address"):
                log.warning("EXCAL lock %s (%d swords) has no claim — "
                            "register with claim.py; skipping",
                            tid, u["value"])
                continue
            btc_addr = claim["btc_address"]
            body = atex_transfer_json(u["value"] * (BRD // 10**8))
            if self.cfg.dry_run:
                log.info("DRY-RUN inscribe ATEX transfer of %d units to %s "
                         "for EXCAL lock %s", u["value"], btc_addr, tid)
            else:
                log.info("inscribing ATEX transfer of %d units to %s",
                         u["value"], btc_addr)
                res = self.inscriber.inscribe(
                    btc_addr, body, "application/json",
                    on_commit=lambda c: self.state.set(
                        f"inscribe:{tid}:commit", c["txid"]))
                log.info("inscribed: commit %s reveal %s",
                         res["commit_txid"], res["reveal_txid"])
            self.state.mark("excal_lock", tid,
                            f"{u['value']} swords -> {btc_addr}")

    # -- 2. ATEX -> SKYNT ------------------------------------------------
    def check_btc_atex_transfers(self):
        if not self.cfg.flow_atex_to_skynt():
            return
        for t in self.indexer.transfers_to(self.cfg.operator_btc_address):
            iid = t["inscription_id"]
            if self.state.seen("btc_atex", iid):
                continue
            claim = self.state.get_claim(f"btc:{t['txid']}")
            intent = (claim or {}).get("intent", "skynt")
            if intent == "skynt":
                eth_addr = (claim or {}).get("eth_address")
                if not eth_addr:
                    log.warning("ATEX transfer %s has no eth_address claim — "
                                "skipping", iid)
                    continue
                if self.cfg.dry_run:
                    log.info("DRY-RUN lockAtex txid=%s amt=%d to=%s",
                             t["txid"], t["amount_units"], eth_addr)
                else:
                    log.info("lockAtex txid=%s amt=%d to=%s",
                             t["txid"], t["amount_units"], eth_addr)
                    r = self.eth.lock_atex(t["txid"], t["amount_units"],
                                           eth_addr)
                    log.info("lockAtex mined: %s", r["tx_hash"])
                self.state.mark("btc_atex", iid,
                                f"{t['amount_units']} -> {eth_addr}")
            elif intent == "excal":
                self._release_excal(t, iid)
            else:
                log.warning("ATEX transfer %s has unknown intent %r — "
                            "skipping", iid, intent)

    # -- 4. ATEX -> EXCAL -------------------------------------------------
    def _release_excal(self, t, iid):
        claim = self.state.get_claim(f"btc:{t['txid']}") or {}
        excal_lock = claim.get("excal_lock")
        if not excal_lock:
            log.warning("ATEX transfer %s intent=excal but no excal_lock "
                        "claim — skipping", iid)
            return
        swords = t["amount_units"] // (BRD // 10**8)  # 1:1, both 1e8/1e18
        if self.cfg.dry_run:
            log.info("DRY-RUN sendtoaddress %s %d swords", excal_lock, swords)
        else:
            txid = self.excal.send_to_address(excal_lock, swords)
            log.info("released %d swords to %s: %s", swords, excal_lock, txid)
        self.state.mark("btc_atex", iid, f"{swords} swords -> {excal_lock}")

    # -- 3. SKYNT -> ATEX -------------------------------------------------
    def check_skynt_burns(self):
        if not self.cfg.flow_skynt_to_atex():
            return
        start = self.state.get("eth_from_block")
        if start is None:
            start = self.eth.w3.eth.block_number - 5000
            self.state.set("eth_from_block", start)
        latest = self.eth.w3.eth.block_number
        # bounded ranges: some RPCs cap log ranges
        while int(start) <= latest:
            end = min(int(start) + 2000, latest)
            for ev in self.eth.skynt_burned_logs(int(start), end):
                lid = f"{ev['tx_hash']}:{ev['log_index']}"
                if self.state.seen("skynt_burn", lid):
                    continue
                body = atex_transfer_json(ev["atex_released"])
                if self.cfg.dry_run:
                    log.info("DRY-RUN inscribe ATEX %d to %s (burn %s)",
                             ev["atex_released"], ev["btc_address"], lid)
                else:
                    log.info("inscribing ATEX %d to %s for burn %s",
                             ev["atex_released"], ev["btc_address"], lid)
                    res = self.inscriber.inscribe(
                        ev["btc_address"], body, "application/json",
                        on_commit=lambda c: self.state.set(
                            f"inscribe:{lid}:commit", c["txid"]))
                    log.info("inscribed: commit %s reveal %s",
                             res["commit_txid"], res["reveal_txid"])
                self.state.mark("skynt_burn", lid,
                                f"{ev['atex_released']} -> {ev['btc_address']}")
            start = end + 1
            self.state.set("eth_from_block", start)

    # ------------------------------------------------------------------
    def poll_once(self):
        self.check_excal_locks()
        self.check_btc_atex_transfers()
        self.check_skynt_burns()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = Config()
    except ConfigError as e:
        log.error("config: %s", e)
        return 2
    if args.dry_run:
        cfg.dry_run = True
    log.info("dry_run=%s", cfg.dry_run)
    for name, ok in cfg.report().items():
        log.info("flow %-28s %s", name, "ENABLED" if ok else "disabled")
    try:
        op = Operator(cfg, State(cfg.state_db))
    except (WalletError, ExcalError) as e:
        log.error("startup: %s", e)
        return 1
    log.info("operator daemon running")
    if args.once:
        op.poll_once()
        return 0
    while True:
        try:
            op.poll_once()
        except Exception:
            log.error("poll error:\n%s", traceback.format_exc())
        time.sleep(cfg.poll_secs)


if __name__ == "__main__":
    sys.exit(main())
