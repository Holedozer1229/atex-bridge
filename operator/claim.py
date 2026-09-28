#!/usr/bin/env python3
"""Manage the operator's claim registry (state db `claims` table).

The daemon never guesses user addresses. Before (or right after) sending
funds to the operator, the user registers where the other side should go:

  EXCAL -> ATEX : claim.py excal <excal_txid> <btc_address>
  ATEX -> SKYNT : claim.py skynt <btc_txid> <eth_address>
  ATEX -> EXCAL : claim.py excal-out <btc_txid> <excal_lock_hex>

List:  claim.py list
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from state import State  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "daemon_state.db")


def _btc_ok(a):
    a = a.strip()
    return (a.startswith("bc1p") or a.startswith("bc1q") or
            a.startswith("1") or a.startswith("3")) and len(a) >= 26


def _eth_ok(a):
    a = a.strip()
    return len(a) == 42 and a.startswith("0x")


def _excal_ok(a):
    a = a.strip().lower().removeprefix("0x")
    return len(a) == 42 and a.startswith("00")


def main():
    st = State(os.environ.get("STATE_DB", DB))
    args = sys.argv[1:]
    if not args or args[0] == "list":
        rows = st.con.execute(
            "SELECT key, value FROM claims ORDER BY key").fetchall()
        for k, v in rows:
            print(f"{k}\t{v}")
        return 0
    if len(args) != 3:
        print(__doc__)
        return 2
    kind, txid, addr = args
    txid = txid.strip().lower().removeprefix("0x")
    if len(txid) != 64:
        print("txid must be 32 bytes hex")
        return 2
    if kind == "excal":
        if not _btc_ok(addr):
            print("bad BTC address")
            return 2
        st.add_claim(f"excal:{txid}", {"btc_address": addr.strip()})
    elif kind == "skynt":
        if not _eth_ok(addr):
            print("bad ETH address")
            return 2
        st.add_claim(f"btc:{txid}",
                     {"intent": "skynt", "eth_address": addr.strip()})
    elif kind == "excal-out":
        if not _excal_ok(addr):
            print("bad Excalibur lock hex ('00'+hash160)")
            return 2
        st.add_claim(f"btc:{txid}",
                     {"intent": "excal",
                      "excal_lock": addr.strip().lower()})
    else:
        print(__doc__)
        return 2
    print("claim recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
