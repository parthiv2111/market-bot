# Market Bot

Python research and paper-trading project. Implementation roadmap: issue #1, starting with PB-01 in issue #2.

Live trading and broker order placement remain disabled. No returns are guaranteed.

Development uses a separate branch per issue and merges only after the acceptance criteria and validation pass. Work resumes from committed checkpoints.

## Architecture

- [Python modular monolith ADR](docs/adr/0001-python-modular-monolith.md)
- [MVP scope and execution modes](docs/mvp-scope.md)
- [PB-01 acceptance cases and validation](docs/testing/pb-01.md)

This checkpoint contains architecture documentation only. To run its offline
checks with Python 3, use `python -m unittest discover -s tests -v` from this
directory. Package installation and application commands arrive with PB-02.
