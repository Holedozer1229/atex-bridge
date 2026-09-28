#!/usr/bin/env python3
"""Minimal Excalibur node RPC client for the bridge operator.

Uses only stdlib. The node's address format is the hex of the PKH lock
itself ('00' + hash160), as returned by getpkhaddress.
"""
import json
import urllib.request
import urllib.error


class ExcalError(Exception):
    pass


class ExcalClient:
    def __init__(self, rpc_url, timeout=30):
        self.url = rpc_url.rstrip("/")
        self.timeout = timeout
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        body = json.dumps({"method": method, "params": params or [],
                           "id": self._id}).encode()
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.load(r)
        except urllib.error.URLError as e:
            raise ExcalError(f"node unreachable: {e}")
        if resp.get("error"):
            raise ExcalError(f"{method}: {resp['error']}")
        return resp.get("result")

    def height(self):
        return int(self.call("getblockcount"))

    def utxos_for_lock(self, lock_hex):
        """UTXOs paying lock_hex: [{txid, vout, value, mature, coinbase}]."""
        return self.call("getutxos", [lock_hex])

    def send_to_address(self, address, amount_swords, fee_rate=1):
        """Release EXCAL. address = lock hex ('00'+hash160)."""
        return self.call("sendtoaddress",
                         [address, int(amount_swords), int(fee_rate)])
