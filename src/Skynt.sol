// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Skynt (SKYNT) — ERC-20 side of the ATEX <-> SKYNT bridge.
/// @notice Minting is restricted to a single bridge address, wired exactly once
///         by the owner after deployment via setBridge(). Anyone may burn their
///         own tokens via burn(), or via burnFrom() with an allowance (used by
///         the bridge for burnForAtex). No pause, no proxy, no admin mint.
/// @dev Deploy order: 1) Skynt  2) AtexBridge(token)  3) Skynt.setBridge(bridge)
///      4) (recommended) Skynt.renounceOwnership() to ossify the wiring.
contract Skynt {
    string public constant name = "Skynt";
    string public constant symbol = "SKYNT";
    uint8 public constant decimals = 18;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    address public owner;
    address public bridge; // set exactly once via setBridge()

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner_, address indexed spender, uint256 value);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event BridgeSet(address indexed bridge);

    error OnlyOwner();
    error OnlyBridge();
    error BridgeAlreadySet();
    error ZeroAddress();
    error InsufficientBalance();
    error InsufficientAllowance();

    modifier onlyOwner() {
        if (msg.sender != owner) revert OnlyOwner();
        _;
    }

    constructor() {
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    /// @notice Wire the bridge exactly once. Follow with renounceOwnership()
    ///         to make the mint path permanently immutable.
    function setBridge(address bridge_) external onlyOwner {
        if (bridge != address(0)) revert BridgeAlreadySet();
        if (bridge_ == address(0)) revert ZeroAddress();
        bridge = bridge_;
        emit BridgeSet(bridge_);
    }

    function renounceOwnership() external onlyOwner {
        emit OwnershipTransferred(owner, address(0));
        owner = address(0);
    }

    /// @notice Only the wired bridge can mint. Reverts for everyone else,
    ///         including before setBridge() is called.
    function mint(address to, uint256 amount) external {
        if (msg.sender != bridge) revert OnlyBridge();
        if (to == address(0)) revert ZeroAddress();
        totalSupply += amount;
        unchecked {
            balanceOf[to] += amount;
        }
        emit Transfer(address(0), to, amount);
    }

    /// @notice Permissionless: any holder may burn their own tokens.
    function burn(uint256 amount) external {
        _burn(msg.sender, amount);
    }

    /// @notice Burn `amount` of `from`'s tokens, requiring an allowance.
    /// @dev Used by AtexBridge.burnForAtex after the user approves the bridge.
    function burnFrom(address from, uint256 amount) external {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed < amount) revert InsufficientAllowance();
        if (allowed != type(uint256).max) {
            unchecked {
                allowance[from][msg.sender] = allowed - amount;
            }
        }
        _burn(from, amount);
    }

    function _burn(address from, uint256 amount) internal {
        if (balanceOf[from] < amount) revert InsufficientBalance();
        unchecked {
            balanceOf[from] -= amount;
        }
        totalSupply -= amount;
        emit Transfer(from, address(0), amount);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed < amount) revert InsufficientAllowance();
        if (allowed != type(uint256).max) {
            unchecked {
                allowance[from][msg.sender] = allowed - amount;
            }
        }
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal {
        if (to == address(0)) revert ZeroAddress();
        if (balanceOf[from] < amount) revert InsufficientBalance();
        unchecked {
            balanceOf[from] -= amount;
            balanceOf[to] += amount;
        }
        emit Transfer(from, to, amount);
    }
}
