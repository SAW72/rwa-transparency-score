// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {ScoreAttestation} from "../src/ScoreAttestation.sol";
import {DeploySepolia} from "../script/DeploySepolia.s.sol";
import {DeployScoreAttestation} from "../script/DeployScoreAttestation.s.sol";

contract ScoreAttestationTest is Test {
    ScoreAttestation internal attestor;
    address internal attester = address(0xA11CE);
    address internal stranger = address(0xB0B);
    bytes32 internal sampleHash = keccak256("payload");

    function setUp() public {
        vm.warp(1_800_000_000);
        attestor = new ScoreAttestation(0.001 ether);
        vm.deal(address(this), 1 ether);
        vm.deal(attester, 1 ether);
        vm.deal(stranger, 1 ether);
    }

    function test_deployerIsOwnerAndInitialAttester() public view {
        assertEq(attestor.owner(), address(this));
        assertTrue(attestor.isAttester(address(this)));
        assertTrue(attestor.authorized(address(this)));
        assertFalse(attestor.authorized(stranger));
    }

    function test_attestStoresHashNotScore() public {
        uint256 trusted = 1_700_000_000;
        vm.warp(trusted);
        vm.expectEmit(true, true, true, true);
        emit ScoreAttestation.ScoreAttested("NVDA", sampleHash, trusted, trusted, address(this));
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", trusted);

        assertTrue(attestor.attested(sampleHash));
        ScoreAttestation.Record memory rec = attestor.getAttestation(sampleHash);
        assertEq(rec.scoreHash, sampleHash);
        assertEq(rec.ticker, "NVDA");
        assertEq(rec.attestedAt, block.timestamp);
        assertEq(rec.claimedAt, trusted);
        assertEq(rec.attester, address(this));

        // Storage holds the digest only — no score / band / pillar fields exist.
        // verify's uint256 is the trusted chain time, not a caller-chosen clock.
        (bool ok, uint256 ts, address who) = attestor.verify(sampleHash, "NVDA");
        assertTrue(ok);
        assertEq(ts, block.timestamp);
        assertEq(who, address(this));
    }

    function test_backdatedClaimedTimestampIsIgnored() public {
        uint256 claimed = 1_700_000_000;
        uint256 trusted = 1_800_000_000;
        vm.warp(trusted);

        vm.expectEmit(true, true, true, true);
        emit ScoreAttestation.ScoreAttested("NVDA", sampleHash, trusted, claimed, address(this));
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", claimed);

        ScoreAttestation.Record memory rec = attestor.getAttestation(sampleHash);
        assertEq(rec.attestedAt, block.timestamp);
        assertEq(rec.attestedAt, trusted);
        assertEq(rec.claimedAt, claimed);
        assertTrue(rec.claimedAt < rec.attestedAt);

        (bool ok, uint256 ts,) = attestor.verify(sampleHash, "NVDA");
        assertTrue(ok);
        assertEq(ts, block.timestamp);
    }

    function test_attestRecordsMsgSenderNotCalldata() public {
        attestor.setAttester(attester, true);
        vm.prank(attester);
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1_700_000_000);
        ScoreAttestation.Record memory rec = attestor.getAttestation(sampleHash);
        assertEq(rec.attester, attester);
        assertTrue(rec.attester != address(this));
    }

    function test_strangerCannotAttestEvenWithFee() public {
        vm.prank(stranger);
        vm.expectRevert(ScoreAttestation.NotAttester.selector);
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1_700_000_000);
        assertFalse(attestor.attested(sampleHash));
    }

    function test_strangerCannotFrontRunOfficialHash() public {
        vm.prank(stranger);
        vm.expectRevert(ScoreAttestation.NotAttester.selector);
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1);
        // Official attester can still lock the same digest.
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1_700_000_000);
        assertTrue(attestor.attested(sampleHash));
        ScoreAttestation.Record memory rec = attestor.getAttestation(sampleHash);
        assertEq(rec.attester, address(this));
    }

    function test_allowlistedAttesterCanAttest() public {
        attestor.setAttester(attester, true);
        assertTrue(attestor.authorized(attester));
        vm.prank(attester);
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 99);
        assertTrue(attestor.attested(sampleHash));
    }

    function test_revokedAttesterCannotAttest() public {
        attestor.setAttester(attester, true);
        attestor.setAttester(attester, false);
        assertFalse(attestor.authorized(attester));
        vm.prank(attester);
        vm.expectRevert(ScoreAttestation.NotAttester.selector);
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1);
    }

    function test_ownerRemainsAuthorizedIfRemovedFromMap() public {
        attestor.setAttester(address(this), false);
        assertFalse(attestor.isAttester(address(this)));
        assertTrue(attestor.authorized(address(this)));
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1);
        assertTrue(attestor.attested(sampleHash));
    }

    function test_nonOwnerCannotSetAttester() public {
        vm.prank(stranger);
        vm.expectRevert(ScoreAttestation.NotOwner.selector);
        attestor.setAttester(stranger, true);
    }

    function test_setAttesterRejectsZero() public {
        vm.expectRevert(ScoreAttestation.ZeroAttester.selector);
        attestor.setAttester(address(0), true);
    }

    function test_verifyRejectsWrongTicker() public {
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 99);
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
        attestor.attest{value: 0.001 ether}(bytes32(0), "NVDA", 1);
    }

    function test_rejectEmptyTicker() public {
        vm.expectRevert(ScoreAttestation.EmptyTicker.selector);
        attestor.attest{value: 0.001 ether}(sampleHash, "", 1);
    }

    function test_rejectLowFee() public {
        vm.expectRevert(ScoreAttestation.InsufficientFee.selector);
        attestor.attest{value: 0.0009 ether}(sampleHash, "NVDA", 1);
    }

    function test_rejectDuplicateHash() public {
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1);
        vm.expectRevert(ScoreAttestation.AlreadyAttested.selector);
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 2);
    }

    function test_hashesForTicker() public {
        bytes32 other = keccak256("other");
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1);
        attestor.attest{value: 0.001 ether}(other, "NVDA", 2);
        bytes32[] memory hashes = attestor.hashesForTicker("NVDA");
        assertEq(hashes.length, 2);
        assertEq(hashes[0], sampleHash);
        assertEq(hashes[1], other);
    }

    function test_ownerCanSetFeeAndWithdraw() public {
        attestor.setFee(0);
        attestor.attest(sampleHash, "NVDA", 1);
        attestor.setFee(0.002 ether);

        address payable sink = payable(address(0xBEEF));
        attestor.attest{value: 0.002 ether}(keccak256("two"), "TSLA", 2);
        uint256 before = sink.balance;
        attestor.withdraw(sink);
        assertEq(sink.balance - before, 0.002 ether);
    }

    function test_withdrawRejectsZeroAddress() public {
        vm.expectRevert(ScoreAttestation.ZeroAttester.selector);
        attestor.withdraw(payable(address(0)));
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
        assertTrue(deployed.authorized(deployed.owner()));
        assertTrue(deployed.isAttester(deployed.owner()));
    }

    function test_deployScriptAllowlistsExtraAttester() public {
        vm.chainId(84532);
        vm.setEnv("ATTESTER_ADDRESS", vm.toString(attester));
        DeploySepolia script = new DeploySepolia();
        ScoreAttestation deployed = script.run();
        assertTrue(deployed.authorized(attester));
        assertTrue(deployed.isAttester(attester));
        vm.setEnv("ATTESTER_ADDRESS", vm.toString(address(0)));
    }

    function test_futureClaimedTimestampIsNotTrusted() public {
        uint256 claimed = block.timestamp + 1;
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", claimed);
        ScoreAttestation.Record memory rec = attestor.getAttestation(sampleHash);
        assertEq(rec.attestedAt, block.timestamp);
        assertEq(rec.claimedAt, claimed);
        assertTrue(attestor.attested(sampleHash));
    }

    function test_zeroClaimedTimestampIsNotTrusted() public {
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 0);
        ScoreAttestation.Record memory rec = attestor.getAttestation(sampleHash);
        assertEq(rec.attestedAt, block.timestamp);
        assertEq(rec.claimedAt, 0);
    }

    function test_acceptsTimestampEqualToBlock() public {
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", block.timestamp);
        (, uint256 ts,) = attestor.verify(sampleHash, "NVDA");
        assertEq(ts, block.timestamp);
    }

    function test_twoStepOwnership() public {
        attestor.transferOwnership(attester);
        assertEq(attestor.pendingOwner(), attester);
        assertEq(attestor.owner(), address(this));

        vm.prank(stranger);
        vm.expectRevert(ScoreAttestation.NotPendingOwner.selector);
        attestor.acceptOwnership();

        vm.prank(attester);
        attestor.acceptOwnership();
        assertEq(attestor.owner(), attester);
        assertEq(attestor.pendingOwner(), address(0));

        vm.expectRevert(ScoreAttestation.NotOwner.selector);
        attestor.setFee(0);

        vm.prank(attester);
        attestor.setFee(0);
    }

    function test_transferOwnershipRejectsZero() public {
        vm.expectRevert(ScoreAttestation.ZeroAttester.selector);
        attestor.transferOwnership(address(0));
    }

    function test_nonOwnerCannotTransferOwnership() public {
        vm.prank(stranger);
        vm.expectRevert(ScoreAttestation.NotOwner.selector);
        attestor.transferOwnership(stranger);
    }

    function test_withdrawPaysContractThatWritesStorage() public {
        GasHungry sink = new GasHungry();
        attestor.attest{value: 0.001 ether}(sampleHash, "NVDA", 1);
        attestor.withdraw(payable(address(sink)));
        assertEq(address(sink).balance, 0.001 ether);
        assertEq(sink.hits(), 1);
    }

    function test_defaultFeeWhenConstructorZero() public {
        ScoreAttestation zero = new ScoreAttestation(0);
        assertEq(zero.attestationFee(), 0.001 ether);
        assertTrue(zero.authorized(address(this)));
    }

    function test_transferOwnershipThenAccept() public {
        address next = address(0x0A1E);
        assertEq(attestor.pendingOwner(), address(0));

        vm.expectEmit(true, true, false, true);
        emit ScoreAttestation.OwnershipTransferStarted(address(this), next);
        attestor.transferOwnership(next);

        assertEq(attestor.owner(), address(this));
        assertEq(attestor.pendingOwner(), next);

        vm.expectEmit(true, true, false, true);
        emit ScoreAttestation.OwnershipTransferred(address(this), next);
        vm.prank(next);
        attestor.acceptOwnership();

        assertEq(attestor.owner(), next);
        assertEq(attestor.pendingOwner(), address(0));
        assertTrue(attestor.authorized(next));
    }

    function test_acceptOwnershipRevertsForNonPending() public {
        address next = address(0x0A1E);
        attestor.transferOwnership(next);

        vm.prank(stranger);
        vm.expectRevert(ScoreAttestation.NotPendingOwner.selector);
        attestor.acceptOwnership();

        vm.expectRevert(ScoreAttestation.NotPendingOwner.selector);
        attestor.acceptOwnership();

        assertEq(attestor.owner(), address(this));
        assertEq(attestor.pendingOwner(), next);
    }

    function test_oldOwnerKeepsControlUntilAccept() public {
        address next = address(0x0A1E);
        attestor.transferOwnership(next);

        attestor.setFee(0.004 ether);
        assertEq(attestor.attestationFee(), 0.004 ether);
        attestor.setAttester(attester, true);
        assertTrue(attestor.isAttester(attester));

        vm.prank(next);
        vm.expectRevert(ScoreAttestation.NotOwner.selector);
        attestor.setFee(0);

        vm.prank(stranger);
        vm.expectRevert(ScoreAttestation.NotOwner.selector);
        attestor.transferOwnership(stranger);

        assertEq(attestor.owner(), address(this));
        assertEq(attestor.pendingOwner(), next);

        vm.prank(next);
        attestor.acceptOwnership();

        vm.expectRevert(ScoreAttestation.NotOwner.selector);
        attestor.setFee(0.001 ether);

        vm.prank(next);
        attestor.setFee(0.005 ether);
        assertEq(attestor.attestationFee(), 0.005 ether);
        assertEq(attestor.owner(), next);
    }

    function test_deployScoreAttestationRevertsOffBaseSepolia() public {
        DeployScoreAttestation script = new DeployScoreAttestation();
        vm.chainId(8453);
        vm.expectRevert(bytes("mainnet held: deploy Base Sepolia only"));
        script.run();
        vm.chainId(1);
        vm.expectRevert(bytes("mainnet held: deploy Base Sepolia only"));
        script.run();
    }

    function test_deployScoreAttestationDefaultsThenHandoff() public {
        address extra = address(0xCA11);
        vm.chainId(84532);
        vm.setEnv("DEPLOYER", vm.toString(attester));
        vm.setEnv("ATTESTER_ADDRESS", vm.toString(address(0)));
        vm.setEnv("ATTESTER_ADDRESS_2", vm.toString(address(0)));
        vm.setEnv("ATTESTER_ADDRESS_3", vm.toString(address(0)));
        vm.setEnv("ATTESTERS", "");
        vm.setEnv("FINAL_OWNER", vm.toString(address(0)));

        DeployScoreAttestation script = new DeployScoreAttestation();
        ScoreAttestation deployed = script.run();
        assertEq(deployed.attestationFee(), 0.001 ether);
        assertEq(deployed.owner(), attester);
        assertEq(deployed.pendingOwner(), address(0));
        assertTrue(deployed.isAttester(attester));
        assertTrue(deployed.authorized(attester));
        assertFalse(deployed.isAttester(stranger));
        assertFalse(deployed.isAttester(extra));

        vm.setEnv("ATTESTER_ADDRESS_3", vm.toString(stranger));
        vm.setEnv("ATTESTERS", vm.toString(extra));
        vm.setEnv("FINAL_OWNER", vm.toString(stranger));
        ScoreAttestation handed = script.run();
        assertEq(handed.owner(), attester);
        assertEq(handed.pendingOwner(), stranger);
        assertTrue(handed.isAttester(attester));
        assertTrue(handed.isAttester(extra));
        assertTrue(handed.isAttester(stranger));
        vm.prank(stranger);
        vm.expectRevert(ScoreAttestation.NotOwner.selector);
        handed.setFee(0);
        vm.prank(stranger);
        handed.acceptOwnership();
        assertEq(handed.owner(), stranger);
        assertEq(handed.pendingOwner(), address(0));
    }
}

/// @dev A receive hook that writes storage. `transfer` (2300 gas) cannot pay this.
contract GasHungry {
    uint256 public hits;

    receive() external payable {
        hits = 1;
    }
}
