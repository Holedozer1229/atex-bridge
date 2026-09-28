#!/usr/bin/env python3
"""Taproot commit-reveal inscriptions (ordinals envelope) for ATEX transfers.

Flow:
  1. commit: operator wallet -> P2TR output committing to the reveal script.
  2. reveal: spend the commit output via script-path; the inscription-bearing
     output pays to the recipient's Bitcoin address.

The reveal script is the standard ordinals envelope, prefixed with
<internal_xonly> OP_CHECKSIG so only the operator key can reveal:

    <xonly> OP_CHECKSIG OP_0 OP_IF "ord" OP_1 <content-type> OP_0 <data...> OP_ENDIF

All signing is local (embit / libsecp256k1). Every built transaction is
re-verified (sighash recompute + Schnorr verify) before it is returned.
"""
import hashlib

from embit import ec, hashes, script
from embit.networks import NETWORKS
from embit.transaction import (
    Transaction, TransactionInput, TransactionOutput, SIGHASH, Witness)

NET = NETWORKS["main"]

# opcodes
_OP_0 = 0x00
_OP_1 = 0x51
_OP_IF = 0x63
_OP_ENDIF = 0x68
_OP_CHECKSIG = 0xac

_LEAF_VERSION = 0xc0          # tapscript
_DUST_SATS = 546
_MAX_PUSH = 520


class InscribeError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def _varint(n: int) -> bytes:
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xffffffff:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def _push(data: bytes) -> bytes:
    """Minimal Bitcoin push encoding."""
    if len(data) == 0:
        return bytes([_OP_0])
    if len(data) > _MAX_PUSH:
        raise InscribeError(f"push too large: {len(data)}")
    if len(data) <= 75:
        return bytes([len(data)]) + data
    if len(data) <= 255:
        return b"\x4c" + bytes([len(data)]) + data
    return b"\x4d" + len(data).to_bytes(2, "little") + data


def _chunks(b: bytes, n: int = _MAX_PUSH):
    return [b[i:i + n] for i in range(0, len(b), n)] or [b""]


# ---------------------------------------------------------------- script
def inscription_script(internal_xonly: bytes, content: bytes,
                       content_type: str = "text/plain;charset=utf-8") -> bytes:
    """Raw reveal-script bytes (ordinals envelope)."""
    if len(internal_xonly) != 32:
        raise InscribeError("internal key must be 32 bytes")
    scr = _push(internal_xonly) + bytes([_OP_CHECKSIG])
    scr += bytes([_OP_0, _OP_IF])
    scr += _push(b"ord")
    scr += bytes([_OP_1])
    scr += _push(content_type.encode())
    scr += bytes([_OP_0])
    for c in _chunks(content):
        scr += _push(c)
    scr += bytes([_OP_ENDIF])
    return scr


def parse_inscription_script(scr: bytes):
    """Inverse of inscription_script: returns (xonly, content_type, content).
    Raises InscribeError if the envelope is malformed."""
    p = 0

    def rd_push():
        nonlocal p
        if p >= len(scr):
            raise InscribeError("truncated script")
        op = scr[p]
        p += 1
        if op == _OP_0:
            return b""
        if op <= 75:
            n = op
        elif op == 0x4c:
            n = scr[p]; p += 1
        elif op == 0x4d:
            n = int.from_bytes(scr[p:p + 2], "little"); p += 2
        elif op == 0x4e:
            n = int.from_bytes(scr[p:p + 4], "little"); p += 4
        else:
            raise InscribeError(f"expected push, got opcode {op:#x}")
        d = scr[p:p + n]; p += n
        if len(d) != n:
            raise InscribeError("truncated push")
        return d

    def rd_op(want, what):
        nonlocal p
        if p >= len(scr) or scr[p] != want:
            raise InscribeError(f"expected {what}")
        p += 1

    xonly = rd_push()
    rd_op(_OP_CHECKSIG, "OP_CHECKSIG")
    rd_op(_OP_0, "OP_0")
    rd_op(_OP_IF, "OP_IF")
    if rd_push() != b"ord":
        raise InscribeError("missing 'ord' marker")
    rd_op(_OP_1, "OP_1")
    ctype = rd_push()
    rd_op(_OP_0, "OP_0")
    content = b""
    while p < len(scr) and scr[p] != _OP_ENDIF:
        content += rd_push()
    rd_op(_OP_ENDIF, "OP_ENDIF")
    if p != len(scr):
        raise InscribeError("trailing bytes after OP_ENDIF")
    return xonly, ctype.decode(), content


# ---------------------------------------------------------------- taproot
def tapleaf_hash(reveal_script: bytes) -> bytes:
    return hashes.tagged_hash(
        "TapLeaf",
        bytes([_LEAF_VERSION]) + _varint(len(reveal_script)) + reveal_script)


def commit_key(internal_pub: "ec.PublicKey", reveal_script: bytes):
    """-> (tweaked_pub, control_block, spk, address). Single-leaf tree."""
    leaf = tapleaf_hash(reveal_script)
    tweaked = internal_pub.taproot_tweak(leaf)
    # parity of the tweaked key's y
    sec = tweaked.sec()
    parity = 0 if sec[0] == 0x02 else 1
    control = bytes([_LEAF_VERSION | parity]) + internal_pub.xonly()
    spk = script.Script(b"\x51\x20" + tweaked.xonly())  # OP_1 <32>
    return tweaked, control, spk, spk.address(NET)


# ---------------------------------------------------------------- tx build
def _txid_display(t: Transaction) -> str:
    return t.txid().hex()


def _vsize(t: Transaction) -> int:
    wits = [vin.witness for vin in t.vin]
    for vin in t.vin:
        vin.witness = Witness([])
    base = len(t.serialize())
    for vin, w in zip(t.vin, wits):
        vin.witness = w
    total = len(t.serialize())
    return (base * 3 + total + 3) // 4


def _even_y_priv(priv: ec.PrivateKey) -> ec.PrivateKey:
    """BIP-340: secret whose pubkey has even y (for <key> OP_CHECKSIG)."""
    if priv.get_public_key().sec()[0] == 0x02:
        return priv
    n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    neg = (n - int.from_bytes(priv.secret, "big")) % n
    return ec.PrivateKey(neg.to_bytes(32, "big"))


class Inscriber:
    def __init__(self, wallet):
        self.w = wallet  # BtcWallet
        self.internal = wallet.priv.get_public_key()
        self.internal_xonly = self.internal.xonly()

    # ---- commit ------------------------------------------------------
    def build_commit(self, content: bytes, content_type: str, fee_rate: int):
        reveal_script = inscription_script(self.internal_xonly, content,
                                           content_type)
        _tweaked, control, commit_spk, commit_addr = commit_key(
            self.internal, reveal_script)

        # estimate reveal vsize to size the commit output
        reveal_vsize_est = 200 + len(reveal_script) + len(content) // 4
        reveal_fee = reveal_vsize_est * fee_rate
        commit_amount = reveal_fee + _DUST_SATS + 1000  # margin

        # fund: select UTXOs covering commit_amount + commit fee
        commit_fee_est = 250 * fee_rate
        picked, total = self.w.select(commit_amount + commit_fee_est)

        vin, values, spks = [], [], []
        for txid, vout, sats in picked:
            vin.append(TransactionInput(bytes.fromhex(txid), vout))
            values.append(sats)
            spks.append(self.w.spk)

        # fee with change output; drop change if it would be dust
        vsize_est = 10 + 57 * len(vin) + 43 * 2 + 108 * len(vin) // 4
        fee = vsize_est * fee_rate
        change = total - commit_amount - fee
        vout = [TransactionOutput(commit_amount, commit_spk)]
        if change >= _DUST_SATS:
            vout.append(TransactionOutput(change, self.w.spk))
        else:
            fee = total - commit_amount  # no change; all remainder is fee
            if fee < 0:
                raise InscribeError("selected funds below commit amount")

        tx = Transaction(vin=vin, vout=vout)
        # key-path sign every input (tweaked with empty tree)
        tweaked_priv = self.w.priv.taproot_tweak(b"")
        for i in range(len(vin)):
            sighash = tx.sighash_taproot(
                i, spks, values,
                sighash=SIGHASH.DEFAULT)
            sig = tweaked_priv.schnorr_sign(sighash)
            # verify before attaching
            if not tweaked_priv.get_public_key().schnorr_verify(
                    sig, sighash):
                raise InscribeError("commit sighash self-verify failed")
            vin[i].witness = Witness([sig.serialize()])

        # exact fee check; trim change if we underpaid
        vsize = _vsize(tx)
        fee_paid = total - sum(o.value for o in vout)
        need = vsize * fee_rate
        if fee_paid < need:
            raise InscribeError(
                f"commit fee underpaid: {fee_paid} < {need} sats")
        return {
            "tx": tx,
            "hex": tx.serialize().hex(),
            "txid": _txid_display(tx),
            "commit_vout": 0,
            "commit_amount": commit_amount,
            "commit_address": commit_addr,
            "reveal_script": reveal_script,
            "control_block": control,
            "fee_rate": fee_rate,
        }

    # ---- reveal ------------------------------------------------------
    def build_reveal(self, commit_txid: str, commit_vout: int,
                     commit_amount: int, commit_spk: "script.Script",
                     reveal_script: bytes, control_block: bytes,
                     recipient_address: str, fee_rate: int):
        try:
            recip_spk = script.address_to_scriptpubkey(recipient_address)
        except Exception as e:
            raise InscribeError(f"bad recipient address: {e}")

        vin = [TransactionInput(bytes.fromhex(commit_txid), commit_vout)]
        # placeholder output; amount fixed after sizing
        tx = Transaction(vin=vin,
                         vout=[TransactionOutput(_DUST_SATS, recip_spk)])

        sighash = tx.sighash_taproot(
            0, [commit_spk], [commit_amount],
            sighash=SIGHASH.DEFAULT,
            script=script.Script(reveal_script),
            leaf_version=_LEAF_VERSION)
        signer = _even_y_priv(self.w.priv)
        # the CHECKSIG key is the internal key; signer must match its xonly
        if signer.get_public_key().xonly() != self.internal_xonly:
            raise InscribeError("internal key mismatch")
        sig = signer.schnorr_sign(sighash)
        if not self.internal.schnorr_verify(sig, sighash):
            raise InscribeError("reveal sighash self-verify failed")
        vin[0].witness = Witness(
            [sig.serialize(), reveal_script, control_block])

        vsize = _vsize(tx)
        fee = vsize * fee_rate
        out_amount = commit_amount - fee
        if out_amount < _DUST_SATS:
            raise InscribeError(
                f"reveal output would be dust: commit {commit_amount}, "
                f"fee {fee}")
        tx.vout[0].value = out_amount
        # amount changed -> sighash commits to values? No: BIP-341 script-path
        # sighash commits to spent amounts, not outputs' ... it commits to
        # hash_outputs, which changed. Re-sign.
        # amount changed -> hash_outputs changed -> re-sign.
        sighash = tx.sighash_taproot(
            0, [commit_spk], [commit_amount],
            sighash=SIGHASH.DEFAULT,
            script=script.Script(reveal_script),
            leaf_version=_LEAF_VERSION)
        sig = signer.schnorr_sign(sighash)
        if not self.internal.schnorr_verify(sig, sighash):
            raise InscribeError("reveal re-sign self-verify failed")
        vin[0].witness = Witness(
            [sig.serialize(), reveal_script, control_block])

        return {
            "tx": tx,
            "hex": tx.serialize().hex(),
            "txid": _txid_display(tx),
            "fee": fee,
            "vsize": vsize,
        }

    # ---- full flow ----------------------------------------------------
    def inscribe(self, recipient_address: str, content: bytes,
                 content_type: str = "text/plain;charset=utf-8",
                 fee_rate: int = None, dry_run: bool = False,
                 on_commit=None):
        """Commit-reveal inscribe. Returns dict with commit/reveal txids+hex.
        on_commit(commit_dict) is called after broadcast so callers can
        persist the commit for crash recovery."""
        fee_rate = fee_rate or self.w.fee_rate()
        commit = self.build_commit(content, content_type, fee_rate)
        _tweaked, _control, commit_spk, _addr = commit_key(
            self.internal, commit["reveal_script"])

        if dry_run:
            reveal = self.build_reveal(
                commit["txid"], commit["commit_vout"],
                commit["commit_amount"], commit_spk,
                commit["reveal_script"], commit["control_block"],
                recipient_address, fee_rate)
            return {"dry_run": True, "commit": commit, "reveal": reveal}

        commit_txid = self.w.broadcast(commit["hex"])
        if on_commit:
            on_commit(commit)
        if not self.w.wait_confirm(commit_txid):
            raise InscribeError(
                f"commit {commit_txid} did not confirm — reveal withheld. "
                "Resume with the stored commit info.")
        reveal = self.build_reveal(
            commit_txid, commit["commit_vout"], commit["commit_amount"],
            commit_spk, commit["reveal_script"], commit["control_block"],
            recipient_address, fee_rate)
        reveal_txid = self.w.broadcast(reveal["hex"])
        return {"commit_txid": commit_txid, "reveal_txid": reveal_txid,
                "commit": commit, "reveal": reveal}
