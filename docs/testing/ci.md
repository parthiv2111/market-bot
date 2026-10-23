# Required CI checks

Require `quality` and `offline-tests (ubuntu-latest)` and
`offline-tests (windows-latest)` and `postgres-tests` in the main branch protection rule or ruleset.
Maintain these exact job names when editing the workflow. GitHub branch
protection is repository administration; adding this file alone does not enable
merge protection. Maintainers must select the checks after their first run.

The workflow runs on pushes and pull requests, with read-only repository
permissions and no provider keys. Dependency installation needs package-index
access; test execution uses the installed locked environment and synthetic
inputs only. A pytest autouse fixture rejects socket connections in the test
process (loopback is allowed for Windows asyncio's internal socketpair).
Subprocess import checks install their own connection prohibition.
Future subprocess/provider integration tests must preserve this restriction;
the fixture is not an operating-system network firewall.

Run the same commands locally:

```console
uv sync --locked
uv run --locked --offline ruff check .
uv run --locked --offline ruff format --check .
uv run --locked --offline mypy
uv run --locked --offline pytest --junitxml=artifacts/pytest.xml
```

Every command is a separate workflow step; no `continue-on-error` or ignored
exit status is allowed. Tests run on both Linux and Windows, Python 3.12.
The quality job also performs negative controls: intentionally invalid lint,
type and test snippets must return nonzero. If a checker silently succeeds,
the negative-control script itself fails. Temporary examples are deleted when
the script exits and never become part of the normal application package.

To demonstrate a failing required check end-to-end, a maintainer can open a
temporary PR containing `def test_failure(): assert False`. The offline-tests
jobs must fail and block merging once the required checks are configured.
Remove the deliberate failure and rerun before merging. Local negative controls
are not evidence that branch protection has been configured.
