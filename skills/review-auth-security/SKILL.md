---
name: review-auth-security
description: Review authentication, HMAC, webhook signature, token and authorization changes. Use when a PR changes trust boundaries or contains auth, signature, hmac, webhook, token or permission signals.
metadata:
  capyreview-domains: security
---

# Authentication Security Review

## Workflow

1. Identify the authentication boundary and attacker-controlled values.
2. Inspect the complete implementation at the fixed PR head commit.
3. Check bypasses, secret handling, replay protection and authorization scope.
4. Read file history when the intent or previous invariant is unclear.
5. Report only defects introduced by added lines with exact evidence.

## Tool guidance

- Use `read_code_context` for the complete function around a changed line.
- Use `read_file_history` to recover the previous security invariant.
- Use `read_code_scanning_findings` as supporting evidence, not as ground truth.

Read [authentication threat patterns](references/threat-patterns.md) when reviewing signatures, tokens or authorization checks.
