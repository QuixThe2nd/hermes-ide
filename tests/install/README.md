# Install and Update E2E Tests

These tests answer one question: can a user on a released version get to this commit?

Each leg installs an old released version, then updates it to HEAD through the real installer or CLI updater. The drivers use isolated checkouts and homes, not mocks of the install/update path.

## The layers

1. `scripts/sandbox/generate-e2e-matrix.mjs` declares every {os, install-method, update-method} pair and expands it against sampled release tags.
2. `.github/workflows/install-e2e.yml` picks release tags, runs the generator, fans out one matrix job per OS, and writes plan/result charts.
3. The reusable run workflows own capability gates. `install-e2e-run.yml` serves Linux, `install-e2e-windows-run.yml` serves Windows, and `install-e2e-macos-run.yml` selects the shared driver on macOS.
4. `tests/install/installer-script-e2e.sh` handles POSIX installs and updates; `tests/install/windows-e2e.ps1` handles Windows.

To declare a new method, edit the generator. To implement it, update the relevant workflow gate and driver.

## Isolation

The drivers do not touch the network for git operations. Each makes a bare clone of the checkout at `serve.git`, then redirects git operations to that clone. The POSIX driver uses a driver-owned `GIT_CONFIG_GLOBAL` with `url.<file://serve.git>.insteadOf` rewrites for the canonical repository URLs.

The driver parks `serve.git`'s `main` at the old release, runs the installer, then advances `main` to HEAD so an update becomes available as it would for a real user. The install leg runs the old ref's own installer; an installer-script update runs HEAD's copy.

## What one leg does

1. Stage the bare clone with `main` at the old release.
2. Run the old release's installer and assert the checkout is at the old commit and `hermes --version` works.
3. Advance `main` to HEAD and apply one update method.
4. Assert the checkout is at HEAD and `hermes --version` still works.

Old releases may not have today's flags. Drivers probe the old installer or installed CLI and omit unsupported flags rather than assuming the current interface.

## Methods

Install method:

- `installer-script`: the platform one-liner (`curl | bash` on Linux/macOS, `irm | iex` on Windows).

Update methods:

- `hermes-update`: run the installed `hermes update` command.
- `installer-script`: rerun the current platform installer.

A grey leg means the declared pair has no driver arm. The result chart distinguishes passed, failed, skipped, and exactly matched historical known failures. [Confirmed historical upgrade limitations](KNOWN_FAILURES.md) documents those failures; unrelated errors on the same release still fail.

## Triggers and cost

The matrix runs every 12 hours, on release tags, and by manual dispatch—not on pull requests. Each OS currently has two legs per sampled tag, so a full run has six legs per tag. A typical green leg takes 7–15 minutes and is capped at 60 minutes.

```sh
gh workflow run install-e2e.yml --ref <branch> -f route=all -f tag-count=2
```

Route slices are `all`, `both`/`update`/`installer` for Linux, `windows`, and `macos`. `tag-count` accepts 1–10; `install-ref` can select one exact release tag instead.

Run drivers locally only in a disposable VM or runner: they install real toolchains and manage Hermes processes in their isolated workroots.

## Artifacts

Each leg uploads installer/update logs and a screen recording. `.github/actions/e2e-screen-record` installs ffmpeg and fails if the recording is missing or empty. Linux uses Xvfb, Windows uses gdigrab, and macOS uses avfoundation. Download artifacts with `gh run download <run-id>`.
