# Security summary

**Status:** Base Sepolia only. This is an operator snapshot of the score contract on `main`, not a third-party audit and not a statement that an issuer is safe.

**Reviewed:** 2026-09-28. Contract: `contracts/src/ScoreAttestation.sol`.

## What the contract does

It locks a hash. It does not store the score, the band, or the pillar breakdown. Anyone can call `verify(scoreHash, ticker)`. A miss means that hash was never locked, or the ticker does not match the locked row.

## Live deployment

`0x2F073a3628D498d92956e7eFE2b26633eDa75b00` is deployed on Base Sepolia (chain id 84532). `attestationFee()` is `0`. The owner called `setFee(0)` in [tx 0x2ea70e2b3fd004bf7165f2cbf13d764f7418b1869a570bb92c5f2fc967471eae](https://sepolia.basescan.org/tx/0x2ea70e2b3fd004bf7165f2cbf13d764f7418b1869a570bb92c5f2fc967471eae) at block 47546350. That transaction succeeded and the fee event moved from 0.001 ETH to 0.

Sourcify reports `exact_match` for creation bytecode and runtime bytecode (`verifiedAt` 2026-09-28T21:28:46Z, solc 0.8.24, contract name ScoreAttestation): `https://sourcify.dev/server/v2/contract/84532/0x2F073a3628D498d92956e7eFE2b26633eDa75b00`. A read of sepolia.basescan.org from this environment returned HTTP 403, so the Basescan verified badge was not re-checked. The contract is Sourcify-verified and not Basescan-verified.

`RWA_ATTEST_VALUE_CAP_WEI` defaults to `0`, which matches the current fee. If the fee is raised above that cap, the attester refuses and sends nothing.

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
- The live Base Sepolia deployment above is the contract these checks describe. Sourcify's exact match is the verification record. Do not treat a different address as this deployment.

## Not claimed

Not on Base mainnet. Not a certification of a tokenized stock. Read [TERMS.md](TERMS.md) and [PRIVACY.md](PRIVACY.md).
