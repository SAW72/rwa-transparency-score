// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {ScoreAttestation} from "../src/ScoreAttestation.sol";

/// @notice Deploy ScoreAttestation on Base Sepolia (chain id 84532) only.
/// Reverts on every other chain, including Base mainnet (8453).
///
/// The broadcaster is the keystore account (`forge script --account`) or the
/// `--sender` address. Optional env `DEPLOYER` overrides that address. This
/// script never reads a private key.
///
/// Env defaults match this repo (no attester address is committed):
/// - `ATTESTATION_FEE_WEI` defaults to 0.001 ether (`ScoreAttestation.DEFAULT_FEE`,
///   also the commented default in contracts/README.md).
/// - `ATTESTER_ADDRESS`, `ATTESTER_ADDRESS_2`, and `ATTESTER_ADDRESS_3` default
///   to `0x0000000000000000000000000000000000000000` (skip), same as
///   `DeploySepolia`'s `ATTESTER_ADDRESS`.
/// - `ATTESTERS` is an optional comma-separated allowlist. Unset means none.
/// - `FINAL_OWNER` defaults to unset (`address(0)`), which leaves the deployer
///   as owner. If it is set and differs from the deployer, the script calls
///   `transferOwnership` and the new owner must `acceptOwnership`.
contract DeployScoreAttestation is Script {
    uint256 internal constant BASE_SEPOLIA = 84532;
    /// @dev Checksummed zero. Repo config has no production attester to default to.
    address internal constant NO_ATTESTER = 0x0000000000000000000000000000000000000000;

    function run() external returns (ScoreAttestation deployed) {
        if (block.chainid != BASE_SEPOLIA) {
            revert("mainnet held: deploy Base Sepolia only");
        }

        uint256 fee = vm.envOr("ATTESTATION_FEE_WEI", uint256(0.001 ether));
        address finalOwner = vm.envOr("FINAL_OWNER", NO_ATTESTER);
        address[8] memory allowlist;
        uint256 n = _loadAllowlist(allowlist);

        if (vm.envExists("DEPLOYER")) {
            vm.startBroadcast(vm.envAddress("DEPLOYER"));
        } else {
            // `--sender`, else the single `--account` signer, else Foundry's default sender.
            vm.startBroadcast();
        }

        deployed = new ScoreAttestation(fee);
        address deployer = deployed.owner();

        for (uint256 i = 0; i < n; i++) {
            address who = allowlist[i];
            if (who != deployer) {
                deployed.setAttester(who, true);
            }
        }
        if (finalOwner != NO_ATTESTER && finalOwner != deployer) {
            deployed.transferOwnership(finalOwner);
        }
        vm.stopBroadcast();

        console2.log("chainid", block.chainid);
        console2.log("ScoreAttestation", address(deployed));
        console2.log("deployer", deployer);
        console2.log("owner", deployed.owner());
        console2.log("pendingOwner", deployed.pendingOwner());
        console2.log("fee wei", deployed.attestationFee());
        console2.log("deployer isAttester", deployed.isAttester(deployer));
        for (uint256 i = 0; i < n; i++) {
            console2.log("attester", allowlist[i], deployed.isAttester(allowlist[i]));
        }
    }

    function _loadAllowlist(address[8] memory allowlist) internal view returns (uint256 n) {
        n = _push(allowlist, n, vm.envOr("ATTESTER_ADDRESS", NO_ATTESTER));
        n = _push(allowlist, n, vm.envOr("ATTESTER_ADDRESS_2", NO_ATTESTER));
        n = _push(allowlist, n, vm.envOr("ATTESTER_ADDRESS_3", NO_ATTESTER));
        if (vm.envExists("ATTESTERS")) {
            string memory raw = vm.envString("ATTESTERS");
            if (bytes(raw).length > 0) {
                address[] memory listed = vm.envAddress("ATTESTERS", ",");
                for (uint256 i = 0; i < listed.length && n < allowlist.length; i++) {
                    n = _push(allowlist, n, listed[i]);
                }
            }
        }
    }

    function _push(address[8] memory allowlist, uint256 n, address who) internal pure returns (uint256) {
        if (who == NO_ATTESTER || n >= allowlist.length) return n;
        for (uint256 i = 0; i < n; i++) {
            if (allowlist[i] == who) return n;
        }
        allowlist[n] = who;
        return n + 1;
    }
}
