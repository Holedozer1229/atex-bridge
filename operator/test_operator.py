#!/usr/bin/env python3
"""Operator tests. No network, no keys, no broadcasts.

Run: ~/workspace/atex-bridge/opvenv/bin/python test_operator.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from embit import ec, script, hashes  # noqa: E402
from embit.networks import NETWORKS  # noqa: E402

import inscribe  # noqa: E402
from inscribe import (  # noqa: E402
    inscription_script, parse_inscription_script, tapleaf_hash, commit_key,
    Inscriber, _even_y_priv)
from state import State  # noqa: E402
from daemon import atex_transfer_json, BRD  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("ok  " if cond else "FAIL") + f" {name} {extra}")


# ---------------------------------------------------------------- envelope
def test_envelope_roundtrip():
    priv = ec.PrivateKey(bytes.fromhex("33" * 32))
    xonly = priv.get_public_key().xonly()
    for content in [b"{}", b"x" * 520, b"y" * 1500, b"\x00\xff" * 300]:
        scr = inscription_script(xonly, content, "application/json")
        x2, ct, c2 = parse_inscription_script(scr)
        check(f"envelope {len(content)}B", x2 == xonly and ct == "application/json"
              and c2 == content)
    # malformed
    try:
        parse_inscription_script(b"\x51\xac\x00")
        check("envelope malformed", False)
    except Exception:
        check("envelope malformed", True)


def test_atex_amount_format():
    cases = [
        (1, "0.000000000000000001"),
        (10**18, "1"),
        (15 * 10**17, "1.5"),
        (1234567890123456789, "1.234567890123456789"),
        (10**10, "0.00000001"),  # 1 sword worth at 1:1
    ]
    for units, want in cases:
        body = json.loads(atex_transfer_json(units))
        check(f"amt {units}", body["amt"] == want, f"got {body['amt']}")
    check("amt tick", json.loads(atex_transfer_json(10**18))["tick"] == "ATEX")


# ---------------------------------------------------------------- taproot
def test_commit_key():
    priv = ec.PrivateKey(bytes.fromhex("44" * 32))
    pub = priv.get_public_key()
    scr = inscription_script(pub.xonly(), b"hello")
    tweaked, control, spk, addr = commit_key(pub, scr)
    # cross-check with embit's own p2tr on the tweaked key material
    expect = pub.taproot_tweak(tapleaf_hash(scr))
    check("tweak matches", tweaked.xonly() == expect.xonly())
    check("addr bc1p", addr.startswith("bc1p"))
    check("control len", len(control) == 33 and control[0] & 0xfe == 0xc0)
    check("control key", control[1:] == pub.xonly())
    check("spk", spk.serialize() == b"\x22\x51\x20" + tweaked.xonly())


class FakeWallet:
    """Same interface as BtcWallet, no network."""

    def __init__(self, seed_byte="55"):
        self.priv = ec.PrivateKey(bytes.fromhex(seed_byte * 32))
        from btc_wallet import privkey_to_p2tr_address, privkey_to_p2tr_script
        self.address = privkey_to_p2tr_address(self.priv)
        self.spk = privkey_to_p2tr_script(self.priv)
        self._utxos = [("ab" * 32, 0, 200_000)]

    def select(self, target):
        total = sum(u[2] for u in self._utxos)
        assert total >= target, "fake funds insufficient"
        return list(self._utxos), total

    def fee_rate(self):
        return 5


def test_commit_reveal_offline():
    w = FakeWallet()
    ins = Inscriber(w)
    content = atex_transfer_json(10**18)
    commit = ins.build_commit(content, "application/json", fee_rate=5)
    check("commit txid", len(commit["txid"]) == 64)
    check("commit addr", commit["commit_address"].startswith("bc1p"))
    # commit spends the fake UTXO: witness present and key-path verifies
    tx = commit["tx"]
    check("commit witness", len(tx.vin[0].witness.items) == 1)
    # reveal
    _tw, _cb, cspk, _a = commit_key(w.priv.get_public_key(),
                                   commit["reveal_script"])
    reveal = ins.build_reveal(commit["txid"], commit["commit_vout"],
                              commit["commit_amount"], cspk,
                              commit["reveal_script"],
                              commit["control_block"],
                              w.address, fee_rate=5)
    rtx = reveal["tx"]
    wit = rtx.vin[0].witness.items
    check("reveal witness items", len(wit) == 3)
    sig, rscript, cblock = wit
    check("reveal script match", bytes(rscript) == commit["reveal_script"])
    check("control match", bytes(cblock) == commit["control_block"])
    # envelope parses out of the witness
    x2, ct, c2 = parse_inscription_script(bytes(rscript))
    check("witness envelope", ct == "application/json" and c2 == content)
    # sighash re-verified independently (belt and suspenders)
    sh = rtx.sighash_taproot(
        0, [cspk], [commit["commit_amount"]],
        script=script.Script(commit["reveal_script"]), leaf_version=0xc0)
    check("reveal sig verifies",
          w.priv.get_public_key().schnorr_verify(
              ec.SchnorrSig.parse(bytes(sig)), sh))
    check("reveal fee sane", 0 < reveal["fee"] < commit["commit_amount"])


def test_odd_key_normalization():
    # find a key with odd-y pubkey
    priv = None
    for i in range(1, 50):
        p = ec.PrivateKey(i.to_bytes(32, "big"))
        if p.get_public_key().sec()[0] == 0x03:
            priv = p
            break
    assert priv is not None
    norm = _even_y_priv(priv)
    check("odd key normalized",
          norm.get_public_key().sec()[0] == 0x02 and
          norm.get_public_key().xonly() == priv.get_public_key().xonly())
    msg = hashes.sha256(b"test")
    sig = norm.schnorr_sign(msg)
    check("odd key sig verifies",
          priv.get_public_key().schnorr_verify(sig, msg))


# ---------------------------------------------------------------- state
def test_state():
    with tempfile.TemporaryDirectory() as d:
        st = State(os.path.join(d, "s.db"))
        check("not seen", not st.seen("k", "1"))
        st.mark("k", "1", "detail")
        check("seen", st.seen("k", "1"))
        st.mark("k", "1")  # idempotent
        check("mark idempotent", st.seen("k", "1"))
        st.set("w", "42")
        check("kv", st.get("w") == "42")
        st.add_claim("excal:abc", {"btc_address": "bc1pXYZ"})
        check("claim", st.get_claim("excal:abc")["btc_address"] == "bc1pXYZ")
        st.close()


# ---------------------------------------------------------------- eth encoding (no RPC)
def test_eth_encoding():
    from web3 import Web3
    art = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "contracts", "out", "AtexBridge.sol",
                       "AtexBridge.json")
    with open(os.path.normpath(art)) as f:
        abi = json.load(f)["abi"]
    w3 = Web3()
    c = w3.eth.contract(address="0x613241672122187dC75a703F187604aD189A7187",
                        abi=abi)
    fn = c.functions.lockAtex(bytes.fromhex("11" * 32), 10**18,
                              "0x000000000000000000000000000000000000dEaD")
    data = fn._encode_transaction_data()
    data_b = bytes.fromhex(data.removeprefix("0x")) if isinstance(data, str) else bytes(data)
    sel = Web3.keccak(text="lockAtex(bytes32,uint256,address)")[:4]
    check("lockAtex selector", data_b[:4] == sel, data_b[:4].hex())
    # SkyntBurned decode from a synthetic log
    sig = Web3.keccak(text="SkyntBurned(address,string,uint256,uint256)")
    from_addr = "0x000000000000000000000000000000000000dEaD"
    topic1 = "0x" + "00" * 12 + from_addr[2:]
    from eth_abi import encode
    enc = encode(["string", "uint256", "uint256"],
                 ["bc1pXYZ", 2 * 10**18, 10**18])
    log = {"address": c.address, "topics": ["0x" + sig.hex(), topic1],
           "data": "0x" + enc.hex(), "blockNumber": 123,
           "blockHash": bytes(32), "transactionHash": bytes(32),
           "transactionIndex": 0, "logIndex": 0, "removed": False}
    ev = c.events.SkyntBurned().process_log(log)["args"]
    check("burn decode",
          ev["from"] == Web3.to_checksum_address(from_addr) and
          ev["btcAddress"] == "bc1pXYZ" and ev["skyntBurned"] == 2 * 10**18 and
          ev["atexReleased"] == 10**18)


def main():
    test_envelope_roundtrip()
    test_atex_amount_format()
    test_commit_key()
    test_commit_reveal_offline()
    test_odd_key_normalization()
    test_state()
    test_eth_encoding()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
