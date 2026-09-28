#!/usr/bin/env python3
"""Rotate the AtexBridge operator to a fresh address.

Safe sequence (the current operator key was exposed in chat):
  1. Lux generates a FRESH key OFFLINE. Only the address is needed here.
  2. This script calls setOperator(new) signed by the current OWNER key
     (read from stdin — never stored, never logged).
  3. Optionally renounceOwnership afterwards to ossify.

Usage: python3 rotate_operator.py <new_operator_address> [--renounce] < owner_key.txt
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eth_operator import EthOperator, EthError  # noqa: E402
from web3 import Web3  # noqa: E402

BRIDGE = "0x613241672122187dC75a703F187604aD189A7187"
RPC = os.environ.get("ETH_RPC", "https://ethereum-rpc.publicnode.com")


def main():
    args = sys.argv[1:]
    renounce = "--renounce" in args
    args = [a for a in args if a != "--renounce"]
    if len(args) != 1:
        print(__doc__)
        return 2
    new_op = Web3.to_checksum_address(args[0])
    owner_key = sys.stdin.read().strip()
    if not owner_key:
        print("empty owner key on stdin")
        return 1
    try:
        op = EthOperator.__new__(EthOperator)
        # minimal init: we need w3/bridge/acct but NOT the operator check
        # (the owner key is not the operator key)
        from eth_account import Account
        op.acct = Account.from_key(owner_key.strip().removeprefix("0x"))
        owner_key = "x" * 64  # drop the secret
        op.w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 60}))
        assert op.w3.eth.chain_id == 1, "not mainnet"
        import json
        art = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "contracts", "out", "AtexBridge.sol",
                           "AtexBridge.json")
        with open(os.path.normpath(art)) as f:
            abi = json.load(f)["abi"]
        op.bridge = op.w3.eth.contract(
            address=Web3.to_checksum_address(BRIDGE), abi=abi)
        op.max_fee_wei = op.w3.to_wei(
            float(os.environ.get("MAX_FEE_GWEI", "2.0")), "gwei")
        owner = op.bridge.functions.owner().call()
        assert owner.lower() == op.acct.address.lower(), \
            f"key {op.acct.address} is not owner {owner}"
        cur = op.bridge.functions.operator().call()
        print(f"current operator: {cur}")
        print(f"new operator:     {new_op}")

        r = op._send(op.bridge.functions.setOperator(new_op),
                     tag="setOperator")
        print(f"setOperator mined: {r['tx_hash']}")
        assert op.bridge.functions.operator().call() == new_op
        print("operator rotated OK")
        if renounce:
            r = op._send(op.bridge.functions.renounceOwnership(),
                         tag="renounceOwnership")
            print(f"renounced: {r['tx_hash']}")
    except (EthError, AssertionError) as e:
        print(f"FAILED: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
