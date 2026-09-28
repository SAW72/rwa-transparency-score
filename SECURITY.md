# Security summary

**Status:** Base Sepolia only. This is an operator snapshot of the score contract on `main`, not a third-party audit and not a statement that an issuer is safe.

**Reviewed:** 2026-09-27. **HEAD:** `b20f9a7`. Contract: `contracts/src/ScoreAttestation.sol`.

## What the contract does

It locks a hash. It does not store the score, the band, or the pillar breakdown. Anyone can call `verify(scoreHash, ticker)`. A miss means that hash was never locked, or the ticker does not match the locked row.

## What holds

- `attest` reverts unless `msg.sender` is the owner or an address the owner passed to `setAttester`. Paying `attestationFee` does not let a stranger occupy a digest.
- The same hash cannot be attested twice.
- Deploy and attest scripts revert unless `block.chainid` is Base Sepolia (84532). Base mainnet is held.

## Limits

- `timestamp` is an argument. It is not `block.timestamp`. A compromised owner or attester key can lock any hash with any time. That key is the trust boundary. The contract cannot tell a wrong score from a right one.
- There is no `transferOwnership`. The deployer stays the only address that can change attesters, change the fee, or withdraw. The owner remains authorized even if removed from the attester map.
- `withdraw` uses `transfer`, which forwards 2300 gas. A contract recipient that needs more gas reverts, and the ETH stays in the contract.
- A payment above `attestationFee` is kept. The owner withdraws the balance.
- A locked hash does not mean the off-chain score was computed correctly. It means an authorized key published that hash.

## Not claimed

Not on Base mainnet. Not a certification of a tokenized stock. Read [TERMS.md](TERMS.md) and [PRIVACY.md](PRIVACY.md).
