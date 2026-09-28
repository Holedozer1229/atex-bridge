#!/usr/bin/env python3
"""Finish L1 wiring: setBridge on Skynt (with the missing 'to' field), then verify.
Private key from stdin ONLY. Never prints the key.
Usage: python3 wire_l1.py < key.txt
"""
import sys, json
from web3 import Web3
from eth_account import Account

RPC = "https://ethereum-rpc.publicnode.com"
SKYNT = "0x57d88B34Cbf780C13b56B8d63d36bD86F56fE262"
BRIDGE = "0x613241672122187dC75a703F187604aD189A7187"
MAX_BASE_GWEI = 0.30

def main():
    key = sys.stdin.read().strip()
    assert key, "empty key"
    acct = Account.from_key(key)
    deployer = acct.address
    w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 60}))
    assert w3.eth.chain_id == 1, "not mainnet"
    sk = json.load(open("/home/hatch/workspace/atex-bridge/contracts/out/Skynt.sol/Skynt.json"))
    br = json.load(open("/home/hatch/workspace/atex-bridge/contracts/out/AtexBridge.sol/AtexBridge.json"))
    skynt = w3.eth.contract(address=SKYNT, abi=sk["abi"])
    bridge = w3.eth.contract(address=BRIDGE, abi=br["abi"])

    # pre-checks
    assert skynt.functions.owner().call() == deployer, "not owner"
    assert skynt.functions.bridge().call() == "0x" + "00" * 20, "bridge already set?!"
    print("bridge token():", bridge.functions.token().call(), flush=True)
    print("bridge operator():", bridge.functions.operator().call(), flush=True)
    assert bridge.functions.token().call() == SKYNT, "bridge token mismatch"
    assert bridge.functions.operator().call() == deployer, "bridge operator mismatch"

    base = float(w3.from_wei(w3.eth.fee_history(1, "latest")["baseFeePerGas"][-1], "gwei"))
    print(f"baseFee={base:.4f} gwei", flush=True)
    assert base <= MAX_BASE_GWEI, "base fee too high"

    nonce = w3.eth.get_transaction_count(deployer)
    print(f"nonce={nonce}", flush=True)
    tx = {"from": deployer, "to": SKYNT, "nonce": nonce, "gas": 100_000,
          "maxFeePerGas": w3.to_wei(0.30, "gwei"),
          "maxPriorityFeePerGas": w3.to_wei(0.10, "gwei"),
          "chainId": 1,
          "data": skynt.functions.setBridge(BRIDGE)._encode_transaction_data()}
    h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    print(f"sent {h.hex()}", flush=True)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=600)
    assert r["status"] == 1, f"setBridge failed again: {h.hex()}"
    print(f"setBridge mined: {h.hex()}", flush=True)

    assert skynt.functions.bridge().call() == BRIDGE, "wiring mismatch"
    rate = bridge.functions.mintRate().call()
    assert rate == 10**18, f"genesis rate wrong: {rate}"
    print(f"VERIFY_OK skynt={SKYNT} bridge={BRIDGE} operator={deployer} mintRate=1.0", flush=True)

if __name__ == "__main__":
    main()
