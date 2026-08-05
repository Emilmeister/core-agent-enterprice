# Layered validation after root cause

Add validation where the same invalid state could cross a meaningful boundary:

- parse and schema validation at untrusted input;
- domain invariants before state changes;
- authorization and tenant checks immediately before access or dispatch;
- persistence constraints for invariants that must survive races or restarts;
- bounded, redacted diagnostics for later investigation.

Do not duplicate every check at every layer. Each guard must block a distinct
bypass, race, or failure mode. Preserve stable public errors and test the original
failure plus the boundary that now rejects it.
