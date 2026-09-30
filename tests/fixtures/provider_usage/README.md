# Provider usage API fixtures

Pinned response shapes for the reconciliation connectors (`voicetoll_collector.reconcile`). The contract tests in
`tests/test_provider_contracts.py` replay every file here through the connector and check the result.

- `docs/`: shapes taken from each provider's API reference (the `source_url` in each file); the values are invented.
- `live/`: real responses captured with `voicetoll-collector recon-capture --day YYYY-MM-DD`, identifiers
  replaced with `REDACTED_n` aliases. `expected` is what the connector read at capture time. Check those numbers
  against the provider's dashboard before committing a file; after that, any parser change that reads the
  real response differently fails the tests.

Usage figures (characters, audio hours, dollars) are kept; they are not secret, but review a file before
committing it to a public repository.
