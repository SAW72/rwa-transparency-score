// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {ScoreAttestation} from "../src/ScoreAttestation.sol";

/// @notice Base Sepolia only. Reverts on any other chain, including Base mainnet.
contract DeploySepolia is Script {
    uint256 internal constant BASE_SEPOLIA = 84532;

    function run() external returns (ScoreAttestation deployed) {
        require(block.chainid == BASE_SEPOLIA, "mainnet held: deploy Base Sepolia only");
        uint256 fee = vm.envOr("ATTESTATION_FEE_WEI", uint256(0.001 ether));
        vm.startBroadcast();
        deployed = new ScoreAttestation(fee);
        vm.stopBroadcast();
        console2.log("ScoreAttestation", address(deployed));
        console2.log("chainid", block.chainid);
        console2.log("fee wei", fee);
    }
}
