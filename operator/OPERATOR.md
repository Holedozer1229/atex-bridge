# ATEX ↔ SKYNT Bridge — Operator Runbook

The operator daemon performs the off-chain legs of the bridge. Contracts are
on Ethereum L1 (immutable); the daemon is the only trusted component.

- SKYNT: `0x57d88B34Cbf780C13b56B8d63d36bD86F56fE262`
- AtexBridge: `0x613241672122187dC75a703F187604aD189A7187`

## What the daemon does

| Flow | Trigger | Action |
|---|---|---|
| EXCAL → ATEX | EXCAL paid to operator lock script (mature) | Inscribe ATEX `transfer` to the user's BTC address |
| ATEX → SKYNT | ATEX `transfer` inscription to operator BTC address | Call `AtexBridge.lockAtex` |
| SKYNT → ATEX | `SkyntBurned` event on L1 | Inscribe ATEX `transfer` to the user's BTC address |
| ATEX → EXCAL | ATEX `transfer` + claim naming an Excalibur lock | `sendtoaddress` on the Excalibur node |

Users register the cross-chain mapping **before** sending funds:

```
python3 claim.py excal <excal_txid> <btc_address>      # EXCAL -> ATEX
python3 claim.py skynt <btc_txid> <eth_address>        # ATEX -> SKYNT
python3 claim.py excal-out <btc_txid> <excal_lock>     # ATEX -> EXCAL
```

A lock/transfer with no claim is logged and **skipped** — the daemon never
guesses addresses.

## Setup

```bash
cd ~/workspace/atex-bridge/operator
~/workspace/atex-bridge/opvenv/bin/python daemon.py --once --dry-run   # config check, no chain writes
~/workspace/atex-bridge/opvenv/bin/python daemon.py                    # full run (POLL_SECS loop)
```

Environment (see `config.py` for the full list):

```
OPERATOR_EXCAL_LOCK     # hex of operator's Excalibur PKH lock (node getpkhaddress)
OPERATOR_BTC_ADDRESS    # operator's Bitcoin P2TR address
OPERATOR_BTC_WIF_FILE   # file holding BTC WIF, mode 0600
OPERATOR_ETH_KEY_FILE   # file holding ETH hex key, mode 0600
BRIDGE_CONTRACT         # AtexBridge (defaults to the L1 deployment above)
INDEXER_URL             # BRC-20 indexer base URL (ATEX-receiving flows)
EXCAL_RPC               # default http://127.0.0.1:9432 (fork) / :9332 mainnet
ETH_RPC / BTC_API_URL   # defaults: publicnode / mempool.space
POLL_SECS=30  MAX_FEE_GWEI=2.0  CONFIRMATIONS=1  DRY_RUN=1
```

Key files: create with `umask 077`, never commit, never log. The daemon
refuses to start if a key file is group/world-readable, if the BTC WIF does
not derive `OPERATOR_BTC_ADDRESS`, or if the ETH key is not the on-chain
`operator()`.

## Before accepting deposits (Lux's checklist)

1. **Rotate the operator key.** The deployer key was exposed in chat.
   Generate a FRESH key offline, then:
   `python3 rotate_operator.py <fresh_address> < owner_key.txt`
   (`--renounce` also ossifies ownership afterwards.)
2. **Fund the operator BTC wallet.** Inscriptions need confirmed BTC for
   commit + reveal fees. The daemon selects UTXOs itself via mempool.space.
3. **Verify the BRC-20 indexer mapping** (`indexer.py` is marked UNVERIFIED):
   set `INDEXER_URL`, run `python3 indexer.py <addr>`, confirm against the
   indexer's explorer before enabling ATEX-receiving flows.
4. **Recheck the ATEX ticker** on a live indexer immediately before the
   deploy/mint inscriptions (first-come-first-served).
5. **Dry-run first**: `DRY_RUN=1 ... daemon.py --once`, inspect the log,
   then go live.

## Safety properties

- Idempotent: every processed event is recorded in `daemon_state.db`
  (`done` table); restarts never double-inscribe or double-mint.
- Every built Bitcoin transaction is sighash-recomputed and Schnorr-verified
  locally before broadcast; the ETH `lockAtex` call is simulated (`eth_call`)
  before signing, and `usedTxids` is checked to avoid reverts.
- Crash between commit and reveal: the commit txid is persisted
  (`inscribe:<id>:commit`); re-running completes the reveal — funds are never
  stranded in an unspendable commit the operator can't finish.
- Fee caps: `MAX_FEE_GWEI` bounds L1 gas; Bitcoin fees use the recommended
  fastest rate; commit outputs are sized from measured vsize + margin.

## Tests

`~/workspace/atex-bridge/opvenv/bin/python test_operator.py` — 34 checks: inscription envelope
round-trip (incl. >520B chunking), exact 18dp BRC-20 amounts, taproot tweak /
control block / address derivation, full offline commit→reveal with
independent sighash verification, odd-y key normalization, state/claim
idempotency, `lockAtex` calldata selector, `SkyntBurned` log decoding.
