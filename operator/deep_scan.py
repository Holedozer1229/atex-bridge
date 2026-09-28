#!/usr/bin/env python3
"""Deep scan: derive indices 0-19 (taproot/segwit) and 0-9 (legacy), check balances.
Mnemonic from stdin. Prints only addresses with activity or errors. Never prints mnemonic.
"""
import sys, json, time, urllib.request
from embit import bip39, bip32, script
from embit.networks import NETWORKS

def bal(addr):
    url = f"https://mempool.space/api/address/{addr}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                d = json.load(r)
            cs, ms = d["chain_stats"], d["mempool_stats"]
            return (cs["funded_txo_sum"] - cs["spent_txo_sum"]
                    + ms["funded_txo_sum"] - ms["spent_txo_sum"], cs["tx_count"])
        except Exception:
            time.sleep(2)
    return None

def main():
    mnemonic = sys.stdin.read().strip()
    assert bip39.mnemonic_is_valid(mnemonic), "bad mnemonic"
    root = bip32.HDKey.from_seed(bip39.mnemonic_to_seed(mnemonic), version=NETWORKS["main"]["xprv"])
    net = NETWORKS["main"]
    jobs = [("m/86'/0'/0'/0", 20, lambda pub: script.p2tr(pub).address(net)),
            ("m/84'/0'/0'/0", 20, lambda pub: script.p2wpkh(pub).address(net)),
            ("m/44'/0'/0'/0", 10, lambda pub: script.p2pkh(pub).address(net))]
    found = 0
    for path, n, mk in jobs:
        acct = root.derive(path)
        for i in range(n):
            addr = mk(acct.derive(f"{i}").get_public_key())
            r = bal(addr)
            if r is None:
                print(f"QUERY_FAIL {path}/{i} {addr}")
            elif r[0] != 0 or r[1] != 0:
                print(f"ACTIVE {path}/{i} {addr} balance_sats={r[0]} txs={r[1]}")
                found += 1
            time.sleep(0.15)
    print(f"SCAN_DONE active={found}")

if __name__ == "__main__":
    main()
