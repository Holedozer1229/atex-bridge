#!/usr/bin/env python3
"""BRC-20 indexer access for the operator.

No indexer was reachable from this environment at build time (Hiro's API is
deprecated; ordiscan is Cloudflare-blocked), so the HTTP mapping below is
marked UNVERIFIED and the ATEX-receiving flows stay disabled until
INDEXER_URL is set AND the mapping is confirmed against the live indexer.

To bring a flow online:
  1. Pick an indexer (e.g. Unisat) and set INDEXER_URL (+ INDEXER_KEY).
  2. Run: python3 indexer.py <operator_btc_address>
     and confirm the printed transfers match the indexer's own explorer.
  3. Only then enable the ATEX -> SKYNT / ATEX -> EXCAL flows.

Transfer record shape (all adapters must return this):
  {"inscription_id": str, "txid": str, "vout": int,
   "amount_units": int,          # base units (1 ATEX = 1e18)
   "sender": str}                # best-effort sender address
"""
import json
import urllib.request
import urllib.error


class IndexerError(Exception):
    pass


class Brc20Indexer:
    """Adapter interface. Subclass per indexer."""

    def transfers_to(self, address, tick="ATEX"):
        """Confirmed BRC-20 `transfer` inscriptions paying `address`."""
        raise NotImplementedError


def _get(url, api_key="", timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": "atex-operator/1.0",
        **({"Authorization": f"Bearer {api_key}"} if api_key else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise IndexerError(f"indexer {e.code} on {url[:120]}")


class UnisatIndexer(Brc20Indexer):
    """UNVERIFIED mapping — confirm against the live API before use."""

    def __init__(self, base_url, api_key=""):
        self.base = base_url.rstrip("/")
        self.key = api_key

    def transfers_to(self, address, tick="ATEX"):
        # UNVERIFIED endpoint shape; adjust to the indexer's real API.
        data = _get(f"{self.base}/v1/indexer/address/{address}/brc20/{tick}/transfer-history",
                    self.key)
        out = []
        for t in data.get("data", {}).get("history", []):
            try:
                out.append({
                    "inscription_id": t["inscriptionId"],
                    "txid": t["txid"],
                    "vout": int(t.get("vout", 0)),
                    "amount_units": int(float(t["amount"]) * 10**18),
                    "sender": t.get("from", ""),
                })
            except (KeyError, ValueError):
                continue
        return out


def make_indexer(url, api_key=""):
    if not url:
        return None
    return UnisatIndexer(url, api_key)


if __name__ == "__main__":
    import sys
    addr = sys.argv[1]
    idx = make_indexer("https://open-api.unisat.io")
    print(json.dumps(idx.transfers_to(addr), indent=2)[:2000])
