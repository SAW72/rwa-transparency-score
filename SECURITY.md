# Security summary

**Status:** Base Sepolia only. This is an operator snapshot of the score contract on `main`, not a third-party audit and not a statement that an issuer is safe.

**Reviewed:** 2026-09-28. Contract: `contracts/src/ScoreAttestation.sol`.

## What the contract does

It locks a hash. It does not store the score, the band, or the pillar breakdown. Anyone can call `verify(scoreHash, ticker)`. A miss means that hash was never locked, or the ticker does not match the locked row.

## What holds

- `attest` reverts unless `msg.sender` is the owner or an address the owner passed to `setAttester`. Paying `attestationFee` does not let a stranger occupy a digest.
- The same hash cannot be attested twice.
- Deploy and attest scripts revert unless `block.chainid` is Base Sepolia (84532). Base mainnet is held.

## Limits

- `attestedAt` is `block.timestamp` at `attest`. The `timestamp` argument is stored only as `claimedAt` and is not trusted (zero, backdated, and future values are all stored as claims). A compromised attester key can still lock any hash. That key is the trust boundary. The contract cannot tell a wrong score from a right one.
- Owner rotation is two-step: `transferOwnership`, then `acceptOwnership`. The previous owner stays on the attester list until `setAttester` revokes them. The owner remains authorized even if removed from that list.
- `withdraw` forwards the remaining gas with `call`, so a contract wallet that writes storage can receive the fee. It does not copy returndata.
- A payment above `attestationFee` is kept. The owner withdraws the balance.
- A locked hash does not mean the off-chain score was computed correctly. It means an authorized key published that hash.
- This bytecode is not what is already on Base Sepolia until that deployment is replaced. Do not treat an older deployment as having these checks.

## Not claimed

Not on Base mainnet. Not a certification of a tokenized stock. Read [TERMS.md](TERMS.md) and [PRIVACY.md](PRIVACY.md).
