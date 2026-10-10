# 0015 — Release hardening
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision
**The workflows.**
- `ci.yml` runs on every push to `main`, on every pull request into `main`, and by hand. Its jobs are:
  - lint;
  - the unit suite on Linux, Windows and macOS, with Python 3.10 to 3.13;
  - the integration suite against a real Blender (decision 0014). Windows runners have no OpenGL 4.3 driver,
    so the test of the Workbench and EEVEE previews is left out there;
  - a package job on all three systems, described below.
- `release.yml` runs when a `v*` tag is pushed. Releases are made from `main`: it runs the whole of `ci.yml` on
  the tagged commit, builds, checks and attests the artifacts, and makes the GitHub release that holds them.
- `publish-pypi.yml`, `publish-npm.yml` and `publish-mcp-registry.yml` each publish one release to one registry.
  Each runs when `release.yml` succeeds, and by hand for a tag.

**Least privilege.**
- Every action is pinned to a full commit SHA, with its version in a `# vX.Y.Z` comment, and each action is
  pinned to the same SHA in every workflow file. Moving to a newer version changes the SHA and the comment
  together.
- Every workflow sets `permissions: contents: read` at the top. A job asks for more only to do its own work:

  | Job | Asks for |
  |---|---|
  | `github-release` | `contents: write`, `id-token: write`, `attestations: write` |
  | `publish-pypi` | `attestations: read` |
  | `publish-npm` | `id-token: write`, `attestations: read` |
  | `publish-registry` | `id-token: write` |

- Checkouts do not keep the token (`persist-credentials: false`).
- Every job has `timeout-minutes`. CI cancels a run that a newer push supersedes; a release or a publish is
  never cancelled.

**Nothing is built until all of the following holds** (checked by the release's `verify` job):
- the tagged commit is on `main`;
- the tag equals `__version__`, the add-on manifest's version, `npm/package.json` and `server.json`;
- `CHANGELOG.md` has a `## [x.y.z] - YYYY-MM-DD` heading for this version ("Unreleased" fails);
- the `PYPI_API_TOKEN` secret is set and is an API token for pypi.org (`release_kit.py pypi-token` reads the
  macaroon's location, without printing the token);
- `mcp-publisher validate` accepts `server.json`.

**The artifacts are built once, checked as built, run in a clean environment, and published byte for byte.**
- The wheel and the sdist hold the add-on, `background.py`, LICENSE and NOTICE, and the package description
  carries the `mcp-name:` line. The sdist holds only the paths that `only-include` in `pyproject.toml` names, and
  `release_kit.py dist` checks the built sdist against the same list. Installed into a clean virtual environment,
  the wheel must reply to `initialize` with the version, and to `tools/list` with exactly as many tools as
  `tools.py` defines. That number is read at run time, never written into a workflow.
- The add-on zip, built by the installed wheel's `build-addon` command, has `blender_manifest.toml` at its
  root, the right version, and no bytecode.
- The npm tarball is built by `release_kit.py npm-bundle` and `npm pack`. Its contents are read from the
  tarball itself: LICENSE, NOTICE, README, the launcher, `server/blenderlens_mcp/__main__.py`, the add-on
  manifest, and the `package.json` version and `mcpName`. Installed into an empty folder, it must give the
  same replies as the wheel.
- CI's package job runs the same steps on Linux, Windows and macOS on every push to `main`.
- The GitHub release holds the add-on zip, the wheel, the sdist, the npm tarball and their `SHA256SUMS`, each
  attested with `attest-build-provenance`, with the CHANGELOG entry as its notes.

**Publishing, one workflow per registry.** Each checks out the released commit, checks that the tag and every
manifest carry one version, and skips a version its registry already has, so a retry is always safe.
- `publish-pypi.yml` checks the token, downloads the wheel and the sdist from the GitHub release, checks them
  against `SHA256SUMS`, their attestations and `release_kit.py dist`, and uploads them with the pinned
  `pypa/gh-action-pypi-publish` action and the `PYPI_API_TOKEN` secret.
- `publish-npm.yml` downloads the tarball, checks it the same way and with `release_kit.py npm-tarball`, and
  runs `npm publish <the checked tarball> --provenance`. npm accepts it through trusted publishing: GitHub's OIDC
  token proves the publish comes from this repository's `publish-npm.yml`, so no npm token is stored.
- `publish-mcp-registry.yml` waits, up to half an hour, until PyPI and npm both have the version, because the
  registry accepts an entry only for packages that exist. It then publishes with `mcp-publisher` 1.8.1, the same
  pinned version and SHA-256 as the release's check, logged in through GitHub OIDC, and retries while the
  packages become visible.

**One copy of the code.** The checks live in `.github/scripts/release_kit.py`, and the Blender download in
`.github/scripts/fetch_blender.py`. Both need only the standard library and Python 3.10. CI, the release and
`tests/test_release.py` run the same code.

**Publishing also needs, outside the repository:**
- a `PYPI_API_TOKEN` secret holding a PyPI API token;
- a trusted publisher on npmjs.com for `blenderlens-mcp`: this repository, the workflow `publish-npm.yml`, with
  `npm publish` allowed. npm deletes a trusted publisher that has not published within two days of being added.

The registry needs nothing: GitHub OIDC proves the `io.github.pzalutski-pixel` namespace.

## Why
Each check guards against a way a release can go wrong without anyone noticing:
- **A package is proven only by running the built artifact.** A launcher that points at the wrong path inside
  the bundle passes every check of the source tree, and fails for every user. So `ci.yml` and `release.yml`
  both install the artifact and require a real reply, and a unit test runs the launcher with Node against a
  bundle built by the same function the workflows call.
- **A manifest's file list is not the tarball.** `package.json` can list LICENSE and NOTICE files that `npm
  pack` cannot reach from inside `npm/`, and a check of the manifest would still pass. So the bundle step
  copies them in, and the check reads the tarball.
- **A tarball check must survive `set -o pipefail`.** In `tar -tzf | grep -q`, grep exits on the first match,
  tar receives SIGPIPE, and pipefail reports the files as missing. So Python's `tarfile` reads the archive
  instead. It also avoids GNU tar, which Git Bash provides on Windows, reading the `C:` at the start of a
  Windows path as the name of a remote host.
- **Versions written in four files drift apart.** So a test keeps them equal, and the release refuses a tag
  that disagrees.
- **A release must be tested as tagged.** A tag can name any commit, and a branch's CI run tested whatever that
  push held. So the release calls `ci.yml` on the tagged commit itself, and makes the GitHub release only after
  every job passes: lint, the unit tests on every system, the integration tests on real Blender and the package
  checks.
- **What is checked must be what is published.** A package rebuilt at publish time is one that no check saw.
  So the artifacts are built once, attached to the GitHub release, and published from there as built.
- **A missing or wrong token fails late.** Without a check, a release would build, publish to one registry and
  then fail on the next. So both tokens are checked before anything is built, and again by each publish
  workflow.
- **An unpinned "latest" `mcp-publisher`** would run whatever that repository ships on the day, with an OIDC
  token in hand. So it is pinned and checked against its published SHA-256.

Pinning actions to commit SHAs closes the hole of a tag moved to other code. The PyPI and npm tokens reach
only the job that publishes, through secrets that Actions masks in its logs. Provenance and attestations let
anyone check which workflow run and which commit produced the published bytes.

**The shape of the release.**
- It runs on a tag pushed on `main`, so a version is always code that `main` holds, and the workflow makes the
  GitHub release itself, with the files its own build produced.
- Each registry has its own workflow. A registry that is down, or a token that has expired, then fails one
  workflow, which is re-run on its own once the cause is fixed, while the others stay published. Re-running the
  whole release instead would fail on the versions already out.
- The publish workflows start from `workflow_run`, which runs them from `main`'s copy of the workflow file, and
  check out the released commit for everything else.
- The tool count is read from `tools.py` rather than written into the workflow, because the count changes
  whenever a tool is added.

**Where Blender is downloaded from.** download.blender.org sits behind a Cloudflare challenge, and can return
403 to non-browser clients such as curl and Python's urllib. Blender's official redirector, mirror.blender.org,
serves the same files, so `fetch_blender.py` falls back to it. The pinned SHA-256 makes either source
trustworthy.

**What only a tag push runs.** CI runs every build and check of the artifacts on every push to `main`. The
`verify` job and the publish workflows run only for a tagged version, so only a release exercises them as a
whole.
