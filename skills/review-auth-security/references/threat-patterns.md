# Authentication threat patterns

## HMAC and webhook signatures

- Reject empty secrets and fixed development bypass values.
- Authenticate the original request bytes with the intended secret.
- Compare signatures in constant time.
- Verify timestamps or delivery identifiers when replay matters.

## Tokens and authorization

- Validate signatures before trusting claims.
- Constrain issuer, audience, algorithm, expiry and not-before time.
- Enforce authorization at the protected resource, not only in the UI.
