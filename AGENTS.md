# timeline cities

build a city-stay ledger from google timeline exports and gpslogger gpx tracks.

## AGENTS.md

- this file and the `.agents/` directory follow the progressive disclosure policy.
- keep this file below 50 lines when possible and below 100 lines always.
- keep only project identity, the package manager, non-obvious commands, rule links, and verification here.
- put detailed instructions in `.agents/rules/`.
- do not put api documentation, code examples, type definitions, generic advice, obvious instructions, redundant information, or vague guidance here or in `.agents/`.

## package manager

- use `uv` for python dependencies and execution.
- use the `Makefile` for checks, locking, bundling, and deployment.

## commands

- `make lock` — update the PEP 723 script lockfile.
- `make build` — build the server bundle.
- `DEPLOY_PATH=... make deploy/prod` — rsync the bundle to the server.

## verification

after making changes:

- `make check` — run tests, coverage, ruff, and ty.
- `make build` — verify that the deployment bundle builds.
