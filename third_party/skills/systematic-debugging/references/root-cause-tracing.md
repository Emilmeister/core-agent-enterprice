# Root-cause tracing

Start at the first observable wrong result and walk backward through the actual
data or control path:

1. Record the failing operation and the exact bad value or state.
2. Identify the immediate producer of that value.
3. Find the caller, message, query, configuration, or prior state transition that
   supplied it.
4. Repeat until reaching the earliest transition that violated an invariant.
5. Confirm the path with a narrow trace, log field, query, or minimal
   reproduction. Redact secrets and personal data.
6. Fix the earliest owned cause. Add a downstream guard only where it protects a
   real trust or durability boundary.

Do not stop at the deepest stack frame merely because it raised the error. If an
external dependency is the earliest cause, distinguish its observed failure from
your inference and specify the local handling or evidence still needed.
