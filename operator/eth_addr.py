#!/usr/bin/env python3
"""Read an Ethereum private key from stdin, print ONLY the derived address.
Never prints the key. Usage: python3 eth_addr.py < key.txt"""
import sys
from eth_account import Account

def main():
    key = sys.stdin.read().strip()
    if not key:
        print("ERROR: empty stdin", file=sys.stderr); sys.exit(1)
    acct = Account.from_key(key)
    print(acct.address)

if __name__ == "__main__":
    main()
