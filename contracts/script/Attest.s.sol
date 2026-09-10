// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {ScoreAttestation} from "../src/ScoreAttestation.sol";

/// @notice Submit a precomputed score hash. Never sends the raw score.
/// Env: ATTESTATION_CONTRACT, SCORE_HASH, TICKER, ATTEST_TIMESTAMP (optional).
/// The attester is always the broadcasting msg.sender. That key must be the
/// contract owner or an address the owner passed to setAttester. There is no
/// attester argument to spoof, and a stranger paying the fee cannot lock a hash.
contract Attest is Script {
    uint256 internal constant BASE_SEPOLIA = 84532;

    function run() external {
        require(block.chainid == BASE_SEPOLIA, "mainnet held: attest on Base Sepolia only");
        address contractAddr = vm.envAddress("ATTESTATION_CONTRACT");
        bytes32 scoreHash = vm.envBytes32("SCORE_HASH");
        string memory ticker = vm.envString("TICKER");
        uint256 timestamp = vm.envOr("ATTEST_TIMESTAMP", block.timestamp);

        ScoreAttestation target = ScoreAttestation(contractAddr);
        uint256 fee = target.attestationFee();

        vm.startBroadcast();
        target.attest{value: fee}(scoreHash, ticker, timestamp);
        vm.stopBroadcast();

        console2.log("attested", ticker);
        console2.logBytes32(scoreHash);
    }
}
