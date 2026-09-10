// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title RAT Score attestation
/// @notice Stores a hash of a score payload + timestamp. NEVER stores the raw
///         score, band, or pillar breakdown. Anyone can verify a cited score
///         was not quietly edited after the fact.
/// @dev Deploy on Base Sepolia (84532) only until an explicit mainnet go.
/// @dev `attest` is NOT permissionless. Only the owner or an allowlisted
///      attester (relayer / API-held key) may lock a hash. Strangers who
///      pay `attestationFee` cannot occupy a digest or brick an official one.
contract ScoreAttestation {
    uint256 public constant DEFAULT_FEE = 0.001 ether;

    address public owner;
    uint256 public attestationFee;

    struct Record {
        bytes32 scoreHash;
        string ticker;
        uint256 timestamp;
        address attester;
    }

    mapping(bytes32 => Record) private _records;
    mapping(bytes32 => bool) public attested;
    mapping(bytes32 => bytes32[]) private _tickerHashes;
    mapping(address => bool) public isAttester;

    event ScoreAttested(string ticker, bytes32 scoreHash, uint256 timestamp, address attester);
    event AttesterUpdated(address indexed attester, bool allowed);

    error InsufficientFee();
    error EmptyHash();
    error EmptyTicker();
    error ZeroAttester();
    error AlreadyAttested();
    error NotOwner();
    error NotAttester();

    constructor(uint256 fee_) {
        owner = msg.sender;
        attestationFee = fee_ == 0 ? DEFAULT_FEE : fee_;
        isAttester[msg.sender] = true;
        emit AttesterUpdated(msg.sender, true);
    }

    /// @notice Owner is always authorized, even if later removed from the map.
    function authorized(address who) public view returns (bool) {
        return who == owner || isAttester[who];
    }

    /// @notice Allowlist or revoke a relayer / API-held key. Owner only.
    function setAttester(address attester, bool allowed) external {
        if (msg.sender != owner) revert NotOwner();
        if (attester == address(0)) revert ZeroAttester();
        isAttester[attester] = allowed;
        emit AttesterUpdated(attester, allowed);
    }

    function attest(
        bytes32 scoreHash,
        string calldata ticker,
        uint256 timestamp
    ) external payable {
        if (!authorized(msg.sender)) revert NotAttester();
        if (msg.value < attestationFee) revert InsufficientFee();
        if (scoreHash == bytes32(0)) revert EmptyHash();
        if (bytes(ticker).length == 0) revert EmptyTicker();
        if (attested[scoreHash]) revert AlreadyAttested();

        address attester = msg.sender;
        attested[scoreHash] = true;
        _records[scoreHash] = Record({
            scoreHash: scoreHash,
            ticker: ticker,
            timestamp: timestamp,
            attester: attester
        });
        _tickerHashes[keccak256(bytes(ticker))].push(scoreHash);

        emit ScoreAttested(ticker, scoreHash, timestamp, attester);
    }

    function getAttestation(bytes32 scoreHash) external view returns (Record memory) {
        return _records[scoreHash];
    }

    /// @notice Static-return helper for clients that do not decode a string.
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
        return (true, rec.timestamp, rec.attester);
    }

    function hashesForTicker(string calldata ticker) external view returns (bytes32[] memory) {
        return _tickerHashes[keccak256(bytes(ticker))];
    }

    function setFee(uint256 fee_) external {
        if (msg.sender != owner) revert NotOwner();
        attestationFee = fee_;
    }

    function withdraw(address payable to) external {
        if (msg.sender != owner) revert NotOwner();
        if (to == address(0)) revert ZeroAttester();
        to.transfer(address(this).balance);
    }
}
