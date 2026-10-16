# Contributing

This module encodes hard-won lessons from real Printify API failures. Contributions are welcome if they follow the design:

1. **The five rules are load-bearing.** Any change must not violate the publish rules documented in the README.
2. **Tests use injected time and sessions.** No network calls, no real sleep. The clock and HTTP layer are injectable.
3. **Negative assertions matter most.** The tests that say "exactly one publish call" and "zero API calls before refusal" are more important than the happy-path tests.

## Pull requests

1. Run `python -m unittest test_publish_queue -v` — all green.
2. If you add a new failure mode, document it in the README table.
3. If you change the state machine, update `diagram.svg`.
