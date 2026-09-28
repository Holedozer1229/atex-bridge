# SKYNT / AtexBridge contracts

Foundry project. Build: `forge build` · Test: `forge test` · solc 0.8.24, Cancun.

## Files

- `src/Skynt.sol` — ERC-20 "Skynt"/"SKYNT", 18 decimals. Hand-rolled (no OZ
  dependency, per the self-contained requirement). `mint()` is callable only by
  the wired bridge; `burn()` is permissionless; `burnFrom()` (allowance-based)
  exists so the bridge can burn the caller's tokens in `burnForAtex`.
- `src/AtexBridge.sol` — bridge with the ossified logarithmic bonding curve.
- `test/AtexBridge.t.sol` — 19 tests (forge-std is a dev-only dependency).

## Wiring decision (documented per spec)

The bridge address is **set once by the owner** (`Skynt.setBridge`), not an
immutable constructor arg — otherwise Skynt and AtexBridge could not reference
each other without CREATE2 precomputation. Deploy order:

1. Deploy `Skynt` (owner = deployer).
2. Deploy `AtexBridge(token=Skynt, operator=Lux)`.
3. `Skynt.setBridge(AtexBridge)` — reverts if called twice or by non-owner.
4. (Recommended) `Skynt.renounceOwnership()` and `AtexBridge.renounceOwnership()`
   to ossify the wiring and operator rotation permanently.

Before step 3, `mint()` reverts for everyone (`bridge == address(0)`).

## Curve

`rate(S) = R0 / (1 + ALPHA * ln(1 + S / S_REF))`, all 1e18 fixed-point.
R0=1e18 (genesis 1:1), ALPHA=1e18, S_REF=1e24 (1M SKYNT). `ln` is implemented
dependency-free: log2 by bit-length scan + 60 rounds of fractional refinement
(repeated squaring), times ln(2). Verified: ln(2) exact to the constant's
precision; ln(e), ln(10), ln(100) within 1e-6 relative.

Sample rates: S=0 → 1.0 · S=1M → 0.5906 · S≈6.4M → 0.3332 · S=147M → 0.167.

## Notes for auditors

- `lockAtex` sets `usedTxids` before the external `mint` (replay-safe, CEI).
- `burnForAtex` quotes `atexReleased` at pre-burn supply, then burns via
  allowance — the user must `approve(bridge, amount)` first.
- The forge lint `reentrancy-events` advisory on the event-after-call ordering is
  deliberate: our state effects precede the external call (standard CEI); the
  callee is our own non-reentrant token.
- `missing-events-access-control` on `renounceOwnership` is a false positive:
  `OwnershipTransferred` is emitted in the same call.

## Operator daemon (`operator/`)

Off-chain bridge operator: watches the Excalibur chain, Bitcoin (BRC-20
indexer), and the `SkyntBurned` event; inscribes ATEX transfers (Taproot
commit-reveal, ordinals envelope) and calls `lockAtex`. All secrets via
environment/files; idempotent via sqlite state; 34-test suite
(`test_operator.py`). Runbook: `operator/OPERATOR.md`.
