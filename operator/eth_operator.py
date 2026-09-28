#!/usr/bin/env python3
"""Ethereum leg of the bridge operator.

- lockAtex: build, sign (operator key), broadcast, confirm receipt, verify the
  AtexLocked event. The 'to' field is always asserted (lesson from wire_l1).
- SkyntBurned watcher: eth_getLogs with a persisted from-block watermark.
- Safety: the key's address MUST equal bridge.operator() or we refuse to sign.

Runs on web3 (see ../opvenv).
"""
import json
import os
import time

from web3 import Web3
from eth_account import Account

_ARTIFACT = os.path.join(os.path.dirname(__file__), "..", "contracts",
                         "out", "AtexBridge.sol", "AtexBridge.json")


class EthError(Exception):
    pass


def _load_abi():
    with open(os.path.normpath(_ARTIFACT)) as f:
        return json.load(f)["abi"]


def _to_bytes32(hexstr):
    h = hexstr.lower().removeprefix("0x")
    if len(h) != 64:
        raise EthError(f"btc_txid must be 32 bytes hex, got {len(h)//2}")
    return bytes.fromhex(h)


class EthOperator:
    def __init__(self, eth_rpc, bridge_address, eth_key_hex, max_fee_gwei=2.0):
        key = eth_key_hex.strip().removeprefix("0x")
        if len(key) != 64:
            raise EthError("operator ETH key must be 32 bytes hex")
        try:
            self.acct = Account.from_key(key)
        except Exception as e:
            raise EthError(f"bad operator ETH key: {e}")
        # wipe the local copy of the raw key string ASAP
        del key
        self.w3 = Web3(Web3.HTTPProvider(
            eth_rpc, request_kwargs={"timeout": 60}))
        if not self.w3.is_connected():
            raise EthError(f"cannot reach ETH RPC {eth_rpc}")
        if self.w3.eth.chain_id != 1:
            raise EthError("not Ethereum mainnet — refusing")
        self.bridge = self.w3.eth.contract(
            address=Web3.to_checksum_address(bridge_address), abi=_load_abi())
        onchain = self.bridge.functions.operator().call()
        if onchain.lower() != self.acct.address.lower():
            raise EthError(
                f"key address {self.acct.address} != on-chain operator "
                f"{onchain} — refusing to sign. Rotate with "
                "rotate_operator.py first.")
        self.max_fee_wei = self.w3.to_wei(max_fee_gwei, "gwei")
        self._key_hex = eth_key_hex  # kept only for signing; never logged

    # ---- views ---------------------------------------------------------
    def mint_rate(self):
        return self.bridge.functions.mintRate().call()

    def used_txid(self, btc_txid_hex):
        return self.bridge.functions.usedTxids(
            _to_bytes32(btc_txid_hex)).call()

    # ---- lockAtex ------------------------------------------------------
    def lock_atex(self, btc_txid_hex, atex_amount_wei, to_address,
                  dry_run=False):
        """Attest an ATEX lock and mint SKYNT. Returns the receipt dict."""
        if atex_amount_wei <= 0:
            raise EthError("atex_amount must be positive")
        to = Web3.to_checksum_address(to_address)
        if self.used_txid(btc_txid_hex):
            raise EthError(f"btc_txid {btc_txid_hex} already used on-chain")
        fn = self.bridge.functions.lockAtex(
            _to_bytes32(btc_txid_hex), atex_amount_wei, to)
        return self._send(fn, dry_run=dry_run, tag=f"lockAtex {btc_txid_hex}")

    def _fee_params(self):
        hist = self.w3.eth.fee_history(1, "latest")
        base = hist["baseFeePerGas"][-1]
        prio = min(self.w3.to_wei(0.2, "gwei"), self.max_fee_wei // 10)
        max_fee = min(base * 2 + prio, self.max_fee_wei)
        if base > self.max_fee_wei:
            raise EthError(
                f"base fee {self.w3.from_wei(base,'gwei'):.2f} gwei exceeds cap "
                f"{self.w3.from_wei(self.max_fee_wei,'gwei'):.2f} — waiting")
        return max_fee, prio

    def _send(self, fn, dry_run=False, tag="tx"):
        # local simulation first: any revert surfaces before spending gas
        try:
            fn.call({"from": self.acct.address})
        except Exception as e:
            raise EthError(f"{tag} would revert: {e}")
        data = fn._encode_transaction_data()
        to_addr = self.bridge.address
        assert to_addr, "missing 'to' — refusing (wire_l1 lesson)"
        max_fee, prio = self._fee_params()
        nonce = self.w3.eth.get_transaction_count(self.acct.address)
        gas = fn.estimate_gas({"from": self.acct.address})
        tx = {"from": self.acct.address, "to": to_addr, "nonce": nonce,
              "gas": int(gas * 1.25),
              "maxFeePerGas": int(max_fee),
              "maxPriorityFeePerGas": int(prio),
              "chainId": 1, "data": data}
        if dry_run:
            return {"dry_run": True, "tx": {k: (v.hex() if isinstance(v, bytes)
                                                else v)
                                            for k, v in tx.items()}}
        signed = self.acct.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(h, timeout=600)
        if receipt["status"] != 1:
            raise EthError(f"{tag} reverted on-chain: {h.hex()}")
        return {"tx_hash": h.hex(), "block": receipt["blockNumber"],
                "gas_used": receipt["gasUsed"]}

    # ---- SkyntBurned watcher -------------------------------------------
    def skynt_burned_logs(self, from_block, to_block="latest"):
        sig = Web3.keccak(
            text="SkyntBurned(address,string,uint256,uint256)").hex()
        logs = self.w3.eth.get_logs({
            "address": self.bridge.address,
            "fromBlock": from_block,
            "toBlock": to_block,
            "topics": ["0x" + sig]})
        out = []
        for lg in logs:
            ev = self.bridge.events.SkyntBurned().process_log(lg)
            a = ev["args"]
            out.append({
                "tx_hash": lg["transactionHash"].hex(),
                "log_index": lg["logIndex"],
                "block": lg["blockNumber"],
                "from": a["from"],
                "btc_address": a["btcAddress"],
                "skynt_burned": a["skyntBurned"],
                "atex_released": a["atexReleased"],
            })
        return out
