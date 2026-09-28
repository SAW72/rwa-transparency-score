// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title RAT Score attestation
/// @notice Stores a hash of a score payload plus a trusted attestation time.
///         NEVER stores the raw score, band, or pillar breakdown. Anyone can
///         verify a cited score was not quietly edited after the fact.
/// @dev Deploy on Base Sepolia (84532) only until an explicit mainnet go.
/// @dev `attest` is NOT permissionless. Only the owner or an allowlisted
///      attester (relayer / API-held key) may lock a hash. Strangers who
///      pay `attestationFee` cannot occupy a digest or brick an official one.
/// @dev Trusted time is `block.timestamp` (`attestedAt`). The `timestamp`
///      argument is kept for callers and is stored and emitted only as
///      `claimedAt`. It is not the trusted time.
/// @dev Ownership moves in two steps: `transferOwnership`, then
///      `acceptOwnership`. The current owner keeps admin control until accept.
contract ScoreAttestation {
    uint256 public constant DEFAULT_FEE = 0.001 ether;
    /// @notice Ceiling for `attestationFee`. One hundred times `DEFAULT_FEE`
    ///         (0.1 ether). A fee at the cap is still a deliberate testnet price;
    ///         anything higher would let the owner key brick `attest` or demand
    ///         multiple ether per hash from the attester key.
    uint256 public constant MAX_FEE = 0.1 ether;

    address public owner;
    address public pendingOwner;
    uint256 public attestationFee;

    struct Record {
        bytes32 scoreHash;
        string ticker;
        uint256 attestedAt;
        address attester;
        uint256 claimedAt;
    }

    mapping(bytes32 => Record) private _records;
    mapping(bytes32 => bool) public attested;
    mapping(bytes32 => bytes32[]) private _tickerHashes;
    mapping(address => bool) public isAttester;

    /// @notice `attestedAt` is `block.timestamp`. `claimedAt` is the attester input.
    event ScoreAttested(string ticker, bytes32 scoreHash, uint256 attestedAt, uint256 claimedAt, address attester);
    event AttesterUpdated(address indexed attester, bool allowed);
    event OwnershipTransferStarted(address indexed previousOwner, address indexed newOwner);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event FeeUpdated(uint256 oldFee, uint256 newFee);
    event Withdrawn(address indexed to, uint256 amount);

    error InsufficientFee();
    error EmptyHash();
    error EmptyTicker();
    error ZeroAttester();
    /// @notice `transferOwnership` was given the zero address. Not a renounce.
    error ZeroOwner();
    error AlreadyAttested();
    error NotOwner();
    error NotPendingOwner();
    error NotAttester();
    error WithdrawFailed();
    error FeeTooHigh(uint256 fee, uint256 max);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(uint256 fee_) {
        owner = msg.sender;
        uint256 fee = fee_ == 0 ? DEFAULT_FEE : fee_;
        if (fee > MAX_FEE) revert FeeTooHigh(fee, MAX_FEE);
        attestationFee = fee;
        isAttester[msg.sender] = true;
        emit AttesterUpdated(msg.sender, true);
    }

    /// @notice Owner is always authorized, even if later removed from the map.
    function authorized(address who) public view returns (bool) {
        return who == owner || isAttester[who];
    }

    /// @notice Start a two-step owner rotation. `newOwner` must call `acceptOwnership`.
    ///         The previous owner stays an attester until `setAttester` revokes them.
    ///         The current owner keeps control until accept.
    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroOwner();
        pendingOwner = newOwner;
        emit OwnershipTransferStarted(owner, newOwner);
    }

    /// @notice Finish a two-step rotation. Only the pending owner can accept.
    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert NotPendingOwner();
        address previous = owner;
        owner = msg.sender;
        pendingOwner = address(0);
        emit OwnershipTransferred(previous, owner);
    }

    /// @notice Allowlist or revoke a relayer / API-held key. Owner only.
    function setAttester(address attester, bool allowed) external onlyOwner {
        if (attester == address(0)) revert ZeroAttester();
        isAttester[attester] = allowed;
        emit AttesterUpdated(attester, allowed);
    }

    /// @notice Lock `scoreHash` for `ticker`.
    /// @param timestamp Attester-supplied time. Stored and emitted as `claimedAt` only.
    ///        The trusted time written to the record is `block.timestamp`.
    function attest(bytes32 scoreHash, string calldata ticker, uint256 timestamp) external payable {
        if (!authorized(msg.sender)) revert NotAttester();
        if (msg.value < attestationFee) revert InsufficientFee();
        if (scoreHash == bytes32(0)) revert EmptyHash();
        if (bytes(ticker).length == 0) revert EmptyTicker();
        if (attested[scoreHash]) revert AlreadyAttested();

        address attester = msg.sender;
        uint256 attestedAt = block.timestamp;
        attested[scoreHash] = true;
        _records[scoreHash] = Record({
            scoreHash: scoreHash, ticker: ticker, attestedAt: attestedAt, attester: attester, claimedAt: timestamp
        });
        _tickerHashes[keccak256(bytes(ticker))].push(scoreHash);

        emit ScoreAttested(ticker, scoreHash, attestedAt, timestamp, attester);
    }

    function getAttestation(bytes32 scoreHash) external view returns (Record memory) {
        return _records[scoreHash];
    }

    /// @notice Static-return helper for clients that do not decode a string.
    /// @return ok True when the hash is attested and the ticker matches.
    /// @return timestamp Trusted time: block.timestamp at attest, not claimedAt.
    /// @return attester Account that locked the hash.
    function verify(bytes32 scoreHash, string calldata ticker)
        external
        view
        returns (bool ok, uint256 timestamp, address attester)
    {
        if (!attested[scoreHash]) {
            return (false, 0, address(0));
        }
        Record storage rec = _records[scoreHash];
        if (keccak256(bytes(rec.ticker)) != keccak256(bytes(ticker))) {
            return (false, 0, address(0));
        }
        return (true, rec.attestedAt, rec.attester);
    }

    function hashesForTicker(string calldata ticker) external view returns (bytes32[] memory) {
        return _tickerHashes[keccak256(bytes(ticker))];
    }

    function setFee(uint256 fee_) external onlyOwner {
        if (fee_ > MAX_FEE) revert FeeTooHigh(fee_, MAX_FEE);
        uint256 oldFee = attestationFee;
        attestationFee = fee_;
        emit FeeUpdated(oldFee, fee_);
    }

    /// @notice Send the full balance to `to`. Uses all remaining gas, not the 2300 stipend.
    function withdraw(address payable to) external onlyOwner {
        if (to == address(0)) revert ZeroAttester();
        uint256 amount = address(this).balance;
        bool ok;
        assembly {
            // out-size 0: do not copy a returndata bomb into memory.
            ok := call(gas(), to, amount, 0, 0, 0, 0)
        }
        if (!ok) revert WithdrawFailed();
        emit Withdrawn(to, amount);
    }
}
