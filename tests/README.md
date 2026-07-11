# Immutable acceptance tests

These tests were revised once for the authorized single-container TerminalSession product directive.

Rules:

- Existing files under `tests/` MUST NOT be edited after the revised test-only commit without a new user directive.
- Production code MUST adapt to the tests, never the reverse.
- Run the suite with `uv run python -m unittest discover -s tests -v`.
- `test_spec_lock.py` prevents any change to the specification bytes or file set.
