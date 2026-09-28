#!/usr/bin/env python3
"""Derive candidate addresses from a BIP-39 mnemonic read on stdin.
Prints ADDRESSES ONLY. The mnemonic is never printed, logged, or stored.
Usage: python3 derive.py < mnemonic.txt
"""
import sys
from embit import bip39, bip32, script
from embit.networks import NETWORKS

def main():
    mnemonic = sys.stdin.read().strip()
    if not mnemonic:
        print("ERROR: empty stdin", file=sys.stderr); sys.exit(1)
    if not bip39.mnemonic_is_valid(mnemonic):
        print("ERROR: mnemonic checksum INVALID", file=sys.stderr); sys.exit(2)
    seed = bip39.mnemonic_to_seed(mnemonic)
    root = bip32.HDKey.from_seed(seed, version=NETWORKS["main"]["xprv"])
    paths = {
        "bip86_taproot": "m/86'/0'/0'/0",
        "bip84_segwit":  "m/84'/0'/0'/0",
        "bip44_legacy":  "m/44'/0'/0'/0",
    }
    for name, path in paths.items():
        acct = root.derive(path)
        for i in range(5):
            child = acct.derive(f"{i}")
            pub = child.get_public_key()
            net = NETWORKS["main"]
            if "86" in path:
                addr = script.p2tr(pub).address(net)
            elif "84" in path:
                addr = script.p2wpkh(pub).address(net)
            else:
                addr = script.p2pkh(pub).address(net)
            print(f"{name}/{i}\t{addr}")
    print("DERIVE_OK")

if __name__ == "__main__":
    main()
