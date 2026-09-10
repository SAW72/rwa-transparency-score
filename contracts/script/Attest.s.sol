// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {ScoreAttestation} from "../src/ScoreAttestation.sol";

/// @notice Submit a precomputed score hash. Never sends the raw score.
/// Env: ATTESTATION_CONTRACT, SCORE_HASH, TICKER, ATTEST_TIMESTAMP (optional),
///      ATTESTER (optional; defaults to the broadcaster).
contract Attest is Script {
    uint256 internal constant BASE_SEPOLIA = 84532;

    function run() external {
        require(block.chainid == BASE_SEPOLIA, "mainnet held: attest on Base Sepolia only");
        address contractAddr = vm.envAddress("ATTESTATION_CONTRACT");
        bytes32 scoreHash = vm.envBytes32("SCORE_HASH");
        string memory ticker = vm.envString("TICKER");
        uint256 timestamp = vm.envOr("ATTEST_TIMESTAMP", block.timestamp);
        address attester = vm.envOr("ATTESTER", msg.sender);

        ScoreAttestation target = ScoreAttestation(contractAddr);
        uint256 fee = target.attestationFee();

        vm.startBroadcast();
        target.attest{value: fee}(scoreHash, ticker, timestamp, attester);
        vm.stopBroadcast();

        console2.log("attested", ticker);
        console2.logBytes32(scoreHash);
    }
}
