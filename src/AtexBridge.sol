// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface ISkynt {
    function mint(address to, uint256 amount) external;
    function burnFrom(address from, uint256 amount) external;
    function totalSupply() external view returns (uint256);
}

/// @title AtexBridge — ATEX (BRC-20, Bitcoin) <-> SKYNT (ERC-20) bridge.
/// @notice Conversion rate is a logarithmic bonding curve on SKYNT supply,
///         priced at the margin:
///             rate(S) = R0 / (1 + ALPHA * ln(1 + S / S_REF))   [SKYNT per ATEX]
///         S = SKYNT totalSupply. Constants are ossified at deploy; the curve
///         can never be changed. All fixed point is 1e18 (ONE).
/// @dev ATEX -> SKYNT: operator attests a Bitcoin lock via lockAtex().
///      SKYNT -> ATEX: anyone burns via burnForAtex(); the operator's daemon
///      watches SkyntBurned and releases ATEX on Bitcoin.
contract AtexBridge {
    uint256 public constant R0 = 1e18; // genesis rate: 1 SKYNT per ATEX
    uint256 public constant ALPHA = 1e18; // curve steepness
    uint256 public constant S_REF = 1e24; // 1M SKYNT in wei (curve scale)

    uint256 internal constant ONE = 1e18;
    uint256 internal constant LN2 = 693147180559945309; // ln(2) * 1e18
    uint256 internal constant TWO_E18 = 2e18;

    ISkynt public immutable token;
    address public owner;
    address public operator;

    /// @notice Bitcoin txids already attested. Replay protection for lockAtex.
    mapping(bytes32 => bool) public usedTxids;

    event AtexLocked(
        bytes32 indexed btcTxid, address indexed to, uint256 atexAmount, uint256 skyntMinted
    );
    event SkyntBurned(
        address indexed from, string btcAddress, uint256 skyntBurned, uint256 atexReleased
    );
    event OperatorSet(address indexed operator);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    error OnlyOwner();
    error OnlyOperator();
    error ZeroAddress();
    error ZeroAmount();
    error ZeroTxid();
    error EmptyBtcAddress();
    error TxidAlreadyUsed();
    error LnOutOfDomain();

    modifier onlyOwner() {
        if (msg.sender != owner) revert OnlyOwner();
        _;
    }

    modifier onlyOperator() {
        if (msg.sender != operator) revert OnlyOperator();
        _;
    }

    constructor(address token_, address operator_) {
        if (token_ == address(0) || operator_ == address(0)) revert ZeroAddress();
        token = ISkynt(token_);
        owner = msg.sender;
        operator = operator_;
        emit OwnershipTransferred(address(0), msg.sender);
        emit OperatorSet(operator_);
    }

    /// @notice Rotate the operator (Lux). Cannot be set to address(0).
    function setOperator(address operator_) external onlyOwner {
        if (operator_ == address(0)) revert ZeroAddress();
        operator = operator_;
        emit OperatorSet(operator_);
    }

    function renounceOwnership() external onlyOwner {
        emit OwnershipTransferred(owner, address(0));
        owner = address(0);
    }

    /// @notice Natural logarithm. Input/output are 1e18 fixed-point; requires x >= 1e18.
    /// @dev log2 by bit-length scan + 60 rounds of fractional refinement
    ///      (repeated squaring), then multiply by ln(2). Dependency-free.
    function ln(uint256 x) public pure returns (uint256) {
        if (x < ONE) revert LnOutOfDomain();
        unchecked {
            // Integer part of log2(x): bit-length scan. bl = floor(log2(x)).
            uint256 bl = 0;
            uint256 t = x;
            if (t >= 1 << 128) {
                t >>= 128;
                bl += 128;
            }
            if (t >= 1 << 64) {
                t >>= 64;
                bl += 64;
            }
            if (t >= 1 << 32) {
                t >>= 32;
                bl += 32;
            }
            if (t >= 1 << 16) {
                t >>= 16;
                bl += 16;
            }
            if (t >= 1 << 8) {
                t >>= 8;
                bl += 8;
            }
            if (t >= 1 << 4) {
                t >>= 4;
                bl += 4;
            }
            if (t >= 1 << 2) {
                t >>= 2;
                bl += 2;
            }
            if (t >= 1 << 1) {
                bl += 1;
            }
            // Normalize m = x / 2^(bl-60) into [2^60, 2^61); log2(x) = k + log2(m/1e18).
            // x >= 1e18 > 2^59, so bl >= 59 and the shifts below are safe.
            uint256 m;
            int256 k;
            if (bl >= 60) {
                m = x >> (bl - 60);
                k = int256(bl) - 60;
            } else {
                m = x << (60 - bl);
                k = int256(bl) - 60;
            }
            if (m >= TWO_E18) {
                m >>= 1;
                k += 1;
            }
            // Fractional part of log2(m/1e18) by repeated squaring; m in [1.15e18, 2e18).
            uint256 frac = 0; // 1e18 fixed point
            uint256 bit = 5e17; // 0.5 in 1e18
            for (uint256 i = 0; i < 60; ++i) {
                m = (m * m) / ONE; // m in [1,2) => m^2 in [1,4); stays in fixed point
                if (m >= TWO_E18) {
                    m >>= 1;
                    frac += bit;
                }
                bit >>= 1;
            }
            uint256 log2x = uint256(k * int256(ONE) + int256(frac));
            return (log2x * LN2) / ONE;
        }
    }

    /// @notice Current SKYNT-per-ATEX mint rate at the live SKYNT supply.
    /// @dev rate = R0 * 1e18 / (1e18 + ALPHA * ln(1 + S/S_REF) / 1e18).
    ///      S/S_REF = S / 1e6 exactly (S_REF / 1e18 = 1e6), avoiding overflow.
    function mintRate() public view returns (uint256) {
        uint256 s = token.totalSupply();
        uint256 x = ONE + s / 1e6; // 1 + S / S_REF in 1e18 fixed point; >= 1e18
        uint256 denom = ONE + (ALPHA * ln(x)) / ONE;
        return (R0 * ONE) / denom;
    }

    /// @notice Attest an ATEX lock on Bitcoin and mint SKYNT to `to`.
    /// @dev Only the operator calls this, after verifying the Bitcoin transfer.
    ///      Each btcTxid is accepted once (usedTxids). Rate is read at the
    ///      pre-mint supply.
    function lockAtex(bytes32 btcTxid, uint256 atexAmount, address to) external onlyOperator {
        if (btcTxid == bytes32(0)) revert ZeroTxid();
        if (atexAmount == 0) revert ZeroAmount();
        if (to == address(0)) revert ZeroAddress();
        if (usedTxids[btcTxid]) revert TxidAlreadyUsed();
        usedTxids[btcTxid] = true;

        uint256 skyntOut = (atexAmount * mintRate()) / ONE;
        token.mint(to, skyntOut);
        emit AtexLocked(btcTxid, to, atexAmount, skyntOut);
    }

    /// @notice Burn the caller's SKYNT and request the ATEX equivalent on Bitcoin.
    /// @dev Caller must approve() the bridge first. atexReleased is quoted at the
    ///      pre-burn supply: skyntAmount * 1e18 / rate. The operator's daemon
    ///      watches SkyntBurned and inscribes the ATEX transfer to btcAddress.
    function burnForAtex(uint256 skyntAmount, string calldata btcAddress) external {
        if (skyntAmount == 0) revert ZeroAmount();
        if (bytes(btcAddress).length == 0) revert EmptyBtcAddress();

        uint256 atexOut = (skyntAmount * ONE) / mintRate(); // pre-burn supply
        token.burnFrom(msg.sender, skyntAmount);
        emit SkyntBurned(msg.sender, btcAddress, skyntAmount, atexOut);
    }
}
