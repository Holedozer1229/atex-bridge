#!/usr/bin/env python3
"""Operator Bitcoin wallet.

- Key: WIF from config (file 0600 preferred). Never logged.
- Address: P2TR (BIP-86 style single key) derived from the WIF.
- Chain data: esplora-compatible REST API (mempool.space default).
- Broadcast: POST /tx.

This module never invents keys: without a configured WIF every signing
operation raises WalletError.
"""
import json
import time
import urllib.request
import urllib.error

from embit import ec, script
from embit.networks import NETWORKS

NET = NETWORKS["main"]
SAT = 1


class WalletError(Exception):
    pass


def _api_get(base, path, timeout=20):
    url = base + path
    req = urllib.request.Request(url, headers={"User-Agent": "atex-operator/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise WalletError(f"BTC API {e.code} on {path}")


def _api_post_text(base, path, data, timeout=30):
    url = base + path
    req = urllib.request.Request(url, data=data.encode(),
                                 headers={"Content-Type": "text/plain",
                                          "User-Agent": "atex-operator/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode().strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        raise WalletError(f"BTC broadcast failed {e.code}: {body}")


def wif_to_privkey(wif):
    """Decode WIF -> embit PrivateKey. Raises WalletError on garbage."""
    try:
        return ec.PrivateKey.from_wif(wif)
    except Exception as e:
        raise WalletError(f"bad OPERATOR_BTC_WIF: {e}")


def privkey_to_p2tr_address(priv):
    return script.p2tr(priv.get_public_key()).address(NET)


def privkey_to_p2tr_script(priv):
    return script.p2tr(priv.get_public_key())


class BtcWallet:
    def __init__(self, wif, api_base, min_confirmations=1):
        self.priv = wif_to_privkey(wif)
        self.api = api_base.rstrip("/")
        self.min_conf = min_confirmations
        self.address = privkey_to_p2tr_address(self.priv)
        self.spk = privkey_to_p2tr_script(self.priv)

    # ---- chain reads --------------------------------------------------
    def utxos(self):
        """Confirmed UTXOs for the operator address: [(txid, vout, sats)]."""
        data = _api_get(self.api, f"/address/{self.address}/utxo")
        out = []
        for u in data:
            if u.get("status", {}).get("confirmed"):
                out.append((u["txid"], u["vout"],
                            u["value"]))
        # largest first for simple selection
        out.sort(key=lambda x: -x[2])
        return out

    def fee_rate(self):
        """ sats/vbyte, fastestFee from mempool.space-style /v1/fees/recommended."""
        try:
            d = _api_get(self.api, "/v1/fees/recommended")
            return max(1, int(d.get("fastestFee", 5)))
        except WalletError:
            return 5  # conservative fallback

    def broadcast(self, raw_hex):
        return _api_post_text(self.api, "/tx", raw_hex)

    def tx_status(self, txid):
        d = _api_get(self.api, f"/tx/{txid}/status")
        return d.get("confirmed", False)

    def wait_confirm(self, txid, timeout=1800, poll=30):
        """Wait until txid confirms (or timeout). Returns True/False."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.tx_status(txid):
                    return True
            except WalletError:
                pass
            time.sleep(poll)
        return False

    # ---- selection ----------------------------------------------------
    def select(self, target_sats):
        """Pick UTXOs covering target_sats. Returns (picked, total)."""
        picked, total = [], 0
        for u in self.utxos():
            picked.append(u)
            total += u[2]
            if total >= target_sats:
                return picked, total
        raise WalletError(
            f"insufficient confirmed BTC: have {total} sats, need {target_sats}")
