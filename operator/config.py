#!/usr/bin/env python3
"""Operator daemon configuration.

Every value comes from the environment. Secrets are never hardcoded and never
logged. Private keys are loaded from files (0600) or, discouraged, env vars.

Required for full operation (each flow self-disables with a warning if its
piece is missing):
  OPERATOR_EXCAL_LOCK   hex lock script of the operator's Excalibur address
                        (from the node's getpkhaddress RPC)
  OPERATOR_BTC_ADDRESS  operator's Bitcoin address (P2TR expected)
  OPERATOR_BTC_WIF_FILE path to a file holding the operator's BTC WIF (0600)
  OPERATOR_ETH_KEY_FILE path to a file holding the operator's ETH hex key (0600)
  BRIDGE_CONTRACT       AtexBridge address on Ethereum L1
  INDEXER_URL           BRC-20 indexer base URL (ATEX transfer watching)

Optional:
  EXCAL_RPC             Excalibur node RPC (default http://127.0.0.1:9432 fork;
                        use :9332 for mainnet)
  ETH_RPC               Ethereum RPC URL (default https://ethereum-rpc.publicnode.com)
  BTC_API_URL           Bitcoin esplora-compatible API
                        (default https://mempool.space/api)
  POLL_SECS             poll interval (default 30)
  MAX_FEE_GWEI          cap on ETH maxFeePerGas (default 2.0)
  CONFIRMATIONS         BTC confirmations before acting (default 1)
  DRY_RUN               1 = log what would happen, change nothing on-chain
"""
import os
import stat


def _get(name, default=""):
    return os.environ.get(name, default).strip()


def _int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        raise ConfigError(f"{name} must be an integer")


def _float(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        raise ConfigError(f"{name} must be a number")


class ConfigError(Exception):
    pass


def _read_secret_file(path, label):
    if not path:
        return ""
    if not os.path.exists(path):
        raise ConfigError(f"{label} file not found: {path}")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise ConfigError(
            f"{label} file {path} has mode {oct(mode)} — must be 0600. "
            "Run: chmod 600 " + path)
    with open(path) as f:
        val = f.read().strip()
    if not val:
        raise ConfigError(f"{label} file {path} is empty")
    return val


class Config:
    def __init__(self):
        self.excal_rpc = _get("EXCAL_RPC", "http://127.0.0.1:9432")
        self.eth_rpc = _get("ETH_RPC", "https://ethereum-rpc.publicnode.com")
        self.btc_api = _get("BTC_API_URL", "https://mempool.space/api").rstrip("/")
        self.indexer_url = _get("INDEXER_URL", "").rstrip("/")
        self.indexer_key = _get("INDEXER_KEY", "")

        self.operator_excal_lock = _get("OPERATOR_EXCAL_LOCK", "").lower()
        self.operator_btc_address = _get("OPERATOR_BTC_ADDRESS", "")
        self.bridge_contract = _get("BRIDGE_CONTRACT",
                                    "0x613241672122187dC75a703F187604aD189A7187")

        self.btc_wif = (_read_secret_file(_get("OPERATOR_BTC_WIF_FILE"),
                                          "OPERATOR_BTC_WIF_FILE")
                        or _get("OPERATOR_BTC_WIF", ""))
        self.eth_key = (_read_secret_file(_get("OPERATOR_ETH_KEY_FILE"),
                                          "OPERATOR_ETH_KEY_FILE")
                        or _get("OPERATOR_ETH_KEY", ""))
        if _get("OPERATOR_BTC_WIF") and not _get("OPERATOR_BTC_WIF_FILE"):
            print("WARNING: OPERATOR_BTC_WIF in env — prefer OPERATOR_BTC_WIF_FILE")
        if _get("OPERATOR_ETH_KEY") and not _get("OPERATOR_ETH_KEY_FILE"):
            print("WARNING: OPERATOR_ETH_KEY in env — prefer OPERATOR_ETH_KEY_FILE")

        self.poll_secs = _int("POLL_SECS", 30)
        self.max_fee_gwei = _float("MAX_FEE_GWEI", 2.0)
        self.confirmations = _int("CONFIRMATIONS", 1)
        self.dry_run = _get("DRY_RUN", "") == "1"
        self.state_db = _get("STATE_DB", os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "daemon_state.db"))

        if self.operator_excal_lock:
            try:
                bytes.fromhex(self.operator_excal_lock)
            except ValueError:
                raise ConfigError("OPERATOR_EXCAL_LOCK must be hex")

    # ---- per-flow readiness -------------------------------------------
    def flow_excal_to_atex(self):
        """EXCAL lock -> inscribe ATEX. Needs Excalibur lock script + BTC wallet."""
        return bool(self.operator_excal_lock and self.btc_wif
                    and self.operator_btc_address)

    def flow_atex_to_skynt(self):
        """ATEX transfer -> lockAtex. Needs indexer + ETH operator key."""
        return bool(self.indexer_url and self.eth_key and self.bridge_contract)

    def flow_skynt_to_atex(self):
        """SkyntBurned -> inscribe ATEX. Needs ETH RPC + BTC wallet."""
        return bool(self.bridge_contract and self.btc_wif
                    and self.operator_btc_address)

    def flow_atex_to_excal(self):
        """ATEX -> release EXCAL. Needs indexer + Excalibur node."""
        return bool(self.indexer_url and self.operator_excal_lock)

    def report(self):
        flows = {
            "EXCAL -> ATEX (inscribe)": self.flow_excal_to_atex(),
            "ATEX -> SKYNT (lockAtex)": self.flow_atex_to_skynt(),
            "SKYNT -> ATEX (inscribe)": self.flow_skynt_to_atex(),
            "ATEX -> EXCAL (release)": self.flow_atex_to_excal(),
        }
        return flows
