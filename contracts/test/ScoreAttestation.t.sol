// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {ScoreAttestation} from "../src/ScoreAttestation.sol";
import {DeploySepolia} from "../script/DeploySepolia.s.sol";

contract ScoreAttestationTest is Test {
    ScoreAttestation internal attestor;
    address internal attester = address(0xA11CE);
    bytes32 internal sampleHash = keccak256("payload");

    function setUp() public {
        attestor = new ScoreAttestation(0.001 ether);
        vm.deal(address(this), 1 ether);
        vm.deal(attester, 1 ether);
    }

    function test_attestStoresHashNotScore() public {
        vm.expectEmit(true, true, true, true);
        emit ScoreAttestation.ScoreAttested("NVDA", sampleHash, 1_700_000_000, attester);
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1_700_000_000, attester);

        assertTrue(attestor.attested(sampleHash));
        ScoreAttestation.Record memory rec = attestor.getAttestation(sampleHash);
        assertEq(rec.scoreHash, sampleHash);
        assertEq(rec.ticker, "NVDA");
        assertEq(rec.timestamp, 1_700_000_000);
        assertEq(rec.attester, attester);

        // Storage holds the digest only — no score / band / pillar fields exist.
        (bool ok, uint256 ts, address who) = attestor.verify(sampleHash, "NVDA");
        assertTrue(ok);
        assertEq(ts, 1_700_000_000);
        assertEq(who, attester);
    }

    function test_verifyRejectsWrongTicker() public {
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 99, attester);
        (bool ok,,) = attestor.verify(sampleHash, "TSLA");
        assertFalse(ok);
    }

    function test_verifyUnknownHash() public view {
        (bool ok, uint256 ts, address who) = attestor.verify(sampleHash, "NVDA");
        assertFalse(ok);
        assertEq(ts, 0);
        assertEq(who, address(0));
    }

    function test_rejectEmptyHash() public {
        vm.expectRevert(ScoreAttestation.EmptyHash.selector);
        attestor.attest{value: 0.001 ether}(bytes32(0), "NVDA", 1, attester);
    }

    function test_rejectEmptyTicker() public {
        vm.expectRevert(ScoreAttestation.EmptyTicker.selector);
        attestor.attest{value: 0.001 ether}(sampleHash, "", 1, attester);
    }

    function test_rejectZeroAttester() public {
        vm.expectRevert(ScoreAttestation.ZeroAttester.selector);
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1, address(0));
    }

    function test_rejectLowFee() public {
        vm.expectRevert(ScoreAttestation.InsufficientFee.selector);
        attestor.attest{value: 0.0009 ether}(sampleHash, "NVDA", 1, attester);
    }

    function test_rejectDuplicateHash() public {
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1, attester);
        vm.expectRevert(ScoreAttestation.AlreadyAttested.selector);
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 2, attester);
    }

    function test_hashesForTicker() public {
        bytes32 other = keccak256("other");
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1, attester);
        attestor.attest{value: 0.001 ether}(other, "NVDA", 2, attester);
        bytes32[] memory hashes = attestor.hashesForTicker("NVDA");
        assertEq(hashes.length, 2);
        assertEq(hashes[0], sampleHash);
        assertEq(hashes[1], other);
    }

    function test_ownerCanSetFeeAndWithdraw() public {
        attestor.setFee(0);
        attestor.attest(sampleHash, "NVDA", 1, attester);
        attestor.setFee(0.002 ether);

        address payable sink = payable(address(0xBEEF));
        attestor.attest{value: 0.002 ether}(keccak256("two"), "TSLA", 2, attester);
        uint256 before = sink.balance;
        attestor.withdraw(sink);
        assertEq(sink.balance - before, 0.002 ether);
    }

    function test_nonOwnerCannotSetFee() public {
        vm.prank(attester);
        vm.expectRevert(ScoreAttestation.NotOwner.selector);
        attestor.setFee(0);
    }

    function test_deployScriptRevertsOnBaseMainnet() public {
        vm.chainId(8453);
        DeploySepolia script = new DeploySepolia();
        vm.expectRevert(bytes("mainnet held: deploy Base Sepolia only"));
        script.run();
    }

    function test_deployScriptAllowsBaseSepolia() public {
        vm.chainId(84532);
        DeploySepolia script = new DeploySepolia();
        ScoreAttestation deployed = script.run();
        assertTrue(address(deployed).code.length > 0);
        assertEq(deployed.attestationFee(), 0.001 ether);
        assertTrue(deployed.owner() != address(0));
    }

    function test_defaultFeeWhenConstructorZero() public {
        ScoreAttestation zero = new ScoreAttestation(0);
        assertEq(zero.attestationFee(), 0.001 ether);
    }
}
