# Immutable acceptance tests

These tests were written from the frozen `spec/` before implementation.

Rules:

- Existing files under `tests/` MUST NOT be edited after the test-only commit.
- Production code MUST adapt to the tests, never the reverse.
- Run the suite with `uv run python -m unittest discover -s tests -v`.
- `test_spec_lock.py` prevents any change to the specification bytes or file set.
