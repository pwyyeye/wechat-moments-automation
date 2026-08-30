# Publisher Agent contracts

The files under `v1/` mirror the canonical, language-neutral contracts in
`auto-content/contracts/publisher-agent/v1/`. Keep them byte-for-byte identical.

Python tests validate the same fixtures used by the NestJS data source. Runtime
models add defensive validation, but they do not redefine the wire contract.
