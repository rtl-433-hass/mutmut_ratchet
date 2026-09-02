# mutmut-ratchet

Shared CI tooling that makes a slow [mutmut](https://github.com/boxed/mutmut)
run usable as a **blocking** pull-request gate: scope the run to what a PR
touched, fan it across a time-balanced matrix, and fail only on a real per-file
mutation-score regression.

It is the de-duplicated form of the `scripts/mutation_*.py` helpers that were
copy-pasted between [`rtl-433-hass/rtl_433`](https://github.com/rtl-433-hass/rtl_433)
and [`rtl-433-hass/pyrtl_433`](https://github.com/rtl-433-hass/pyrtl_433). Only
the per-repo constants differed; those are now configuration.

## Why

A full-package mutmut run is slow (tens of minutes), and its score is not
perfectly reproducible. Naively gating on it either blocks every PR for an hour
or fails spuriously. The five subcommands fix that:

| Subcommand | What it does |
| --- | --- |
| `targets` | Turns `git diff --name-only` into the modules worth mutating, or escalates to a full run |
| `shards`  | Deterministic LPT partition of the package into N **time**-balanced shards |
| `stats`   | Reduces mutmut's on-disk `mutants/*.meta` into a comparable JSON payload |
| `ratchet` | Compares that payload to a committed baseline with a mutant-denominated tolerance band |
| `timings` | Records the measured per-file mutmut runtime that `shards` balances on |

Two design choices carry most of the value:

* **Shards balance by measured time, not mutant count.** Per-mutant test time
  varies several-fold across modules, so a count-balanced split still leaves a
  slow pole that the gate waits on. Modules are sorted heaviest first (ties by
  path) and dropped into the lightest bin (ties by lowest index) — plain LPT,
  within 4/3 of optimal makespan and fully reproducible, so every matrix job
  computes the identical assignment with no coordination.
* **The ratchet's tolerance band is denominated in mutants.** A flat percentage
  is wrong in both directions: 2% is ~13 mutants on a 630-mutant module but
  *zero* mutants on a 29-mutant one. The band is
  `max(tolerance_fraction × total, tolerance_mutants)` mutants, converted back to
  score space. That absorbs both run-to-run noise and the scoped-vs-full gap (a
  scoped run is a slight lower bound: some mutants are only killed by tests in
  other files) while still failing on a genuine regression, which typically
  costs far more mutants than the band. The baseline only ever ratchets upward.

## Install

```bash
uv add --dev mutmut-ratchet
```

`mutmut` itself is a dependency (the tooling reads mutmut's module walk and its
per-file meta through `mutmut.__main__`), so installing this pulls it in. Pin
the exact mutmut version you test against in your own dev group if you care
which one CI uses.

## Configure

All settings live in your `pyproject.toml` under `[tool.mutmut_ratchet]`. Only
`package_path` is required; unknown keys and wrong types are hard errors, so a
typo can never silently weaken the gate.

```toml
[tool.mutmut_ratchet]
package_path = "my_package"
escalate_paths = ["pyproject.toml", "uv.lock", "tests/conftest.py"]

[tool.mutmut_ratchet.explicit_test_sources]
"tests/test_urls.py" = ["_urls.py"]
"tests/test_mut_client_floor.py" = ["client.py"]
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `package_path` | string | *(required)* | Repo-relative path of the package under mutation, e.g. `pyrtl_433` or `custom_components/rtl_433`. |
| `package_dotted` | string | `package_path` with `/` → `.` | Dotted name mutmut uses in mutant filters. Set it only when the two differ (e.g. a `src/` layout). |
| `baseline` | string | `"scripts/mutation_baseline.json"` | Committed per-file score baseline. Relative paths anchor to the `pyproject.toml`'s directory. |
| `timings` | string | `"scripts/mutation_timings.json"` | Committed per-file mutmut runtime profile, used as the shard bin-pack weight. |
| `escalate_paths` | list of strings | `["pyproject.toml", "tests/conftest.py"]` | Changed paths that force a **full** run because they can move results package-wide. Exact repo-relative matches. |
| `explicit_test_sources` | table of string → list of strings | `{}` | Test file → the source modules (relative to `package_path`) it exercises, for tests the `test[_mut]_<name>.py` → `<name>.py` convention cannot resolve. Genuinely broad tests are deliberately *left out* so they still escalate. |
| `tolerance_fraction` | float | `0.02` | Fractional half of the band; scales the cushion on large files. |
| `tolerance_mutants` | int | `3` | Absolute half of the band, in mutants; protects small files where a percentage rounds to nothing. |
| `precision` | int | `6` | Decimals scores are rounded to before comparison. |
| `floor` | float | `0.70` | Advisory floor recorded into a freshly written baseline payload. |
| `fallback_seconds_per_mutant` | float | `1.0` | Weight used when neither a timing nor any timing profile exists at all (a fresh checkout). |

A committed baseline carries the `tolerance_fraction` / `tolerance_mutants` it
was written with, and those win over the config defaults — so retuning the band
is a baseline edit, not a workflow edit. An explicit `--tolerance-*` flag beats
both.

Precedence throughout is **CLI flag → `pyproject.toml` → built-in default**.

## Commands

Every subcommand also accepts `--config PYPROJECT` (default: the nearest
`pyproject.toml` at or above the cwd), `--package-path`, and `--package-dotted`.

```
mutmut-ratchet targets [PATH ...]
```
Maps changed paths to targets. With no arguments, reads whitespace-separated
paths from stdin. Prints three lines: `all` or `scoped`; the mutmut filter
patterns; the source paths. Exit 0.

```
mutmut-ratchet shards --shard N --of M [--restrict PATH ...] [--baseline P] [--timings P]
```
Prints two lines: this shard's filter patterns, and its source paths (both blank
for an empty shard). `--restrict` intersects the shard with a scoped set without
changing the global assignment, so a scoped PR fans across the same shards.
Exit 0, or 2 for out-of-range `--shard`/`--of`.

```
mutmut-ratchet stats [--paths PATH ...]
```
Prints the per-file stats JSON on stdout. `--paths` is **required after a
filtered `mutmut run`**: mutants outside the filter stay "not checked", which
would otherwise read as 0%. Exit 0.

```
mutmut-ratchet ratchet --mode floor|strict --stats FILE [--baseline P] [--update] [--tolerance-fraction F] [--tolerance-mutants N]
```
`floor` is the CI gate (improvements never fail); `strict` also fails on upward
drift, as a local check that the committed baseline is still representative.
`--update` writes current scores back, keeping the higher of baseline/current
per file, and refuses while regressions stand. Exit 0 pass, 1 regression/drift,
2 missing stats or baseline.

```
mutmut-ratchet timings [--out PATH]
```
Writes the runtime profile after a **full** `mutmut run`. Exit 0, or 2 when no
timing data exists yet.

## Worked GitHub Actions example

Scoped on PRs, full on push/schedule, sharded six ways either way, with one
aggregated status check.

```yaml
name: Mutation

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  schedule:
    - cron: "0 3 * * *"

permissions: {}

jobs:
  mutation:
    name: "Mutation floor — shard ${{ matrix.shard }}"
    runs-on: ubuntu-latest
    timeout-minutes: 30
    strategy:
      fail-fast: false
      matrix:
        shard: [0, 1, 2, 3, 4, 5]
    steps:
      - uses: actions/checkout@v5
        with:
          # Need history back to the base branch to diff a PR's changed files.
          fetch-depth: 0
      - uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true
      - run: uv sync --locked --dev

      - name: Determine scope for this trigger
        id: scope
        run: |
          if [ "${{ github.event_name }}" = "pull_request" ]; then
            git fetch --no-tags origin "${{ github.base_ref }}"
            changed=$(git diff --name-only "origin/${{ github.base_ref }}...HEAD")
            uv run mutmut-ratchet targets $changed > targets.txt
            cat targets.txt
            {
              echo "mode=$(sed -n '1p' targets.txt)"
              echo "paths=$(sed -n '3p' targets.txt)"
            } >> "$GITHUB_OUTPUT"
          else
            echo "mode=all" >> "$GITHUB_OUTPUT"
            echo "paths=" >> "$GITHUB_OUTPUT"
          fi

      - name: Determine this shard's modules
        id: shard
        run: |
          if [ "${{ steps.scope.outputs.mode }}" = "all" ]; then
            uv run mutmut-ratchet shards --shard ${{ matrix.shard }} --of 6 > shard.txt
          elif [ -n "${{ steps.scope.outputs.paths }}" ]; then
            uv run mutmut-ratchet shards --shard ${{ matrix.shard }} --of 6 --restrict ${{ steps.scope.outputs.paths }} > shard.txt
          else
            # Scoped run with nothing in scope (e.g. a docs-only PR): no work.
            printf '\n\n' > shard.txt
          fi
          {
            echo "patterns=$(sed -n '1p' shard.txt)"
            echo "paths=$(sed -n '2p' shard.txt)"
          } >> "$GITHUB_OUTPUT"
          cat shard.txt

      - name: Run mutation testing (this shard's modules)
        if: steps.shard.outputs.patterns != ''
        run: |
          uv run mutmut run ${{ steps.shard.outputs.patterns }}
          uv run mutmut-ratchet stats --paths ${{ steps.shard.outputs.paths }} > mutation-stats.json

      - name: Enforce per-file floor vs baseline
        if: steps.shard.outputs.patterns != ''
        run: uv run mutmut-ratchet ratchet --mode floor --stats mutation-stats.json

      - name: No modules in this shard
        if: steps.shard.outputs.patterns == ''
        run: echo "No modules in scope for this shard; nothing to check."

  mutation-gate:
    name: "Mutation floor"
    needs: [mutation]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Aggregate shard results
        run: |
          if [ "${{ needs.mutation.result }}" != "success" ]; then
            echo "One or more mutation shards failed."; exit 1
          fi
          echo "All mutation shards passed."
```

A simpler single-job form, for a package small enough not to need a matrix:

```yaml
      - name: Mutation test (per-module floor ratchet)
        run: |
          uv run mutmut run --max-children 4
          uv run mutmut-ratchet stats > mutation-stats.json
          uv run mutmut-ratchet ratchet --mode floor --stats mutation-stats.json
```

Never cache the `mutants/` working tree: a stale cache can report a higher score
than reality and let a regression through. Caching uv's download cache is fine.

## Bootstrapping a baseline

```bash
uv run mutmut run                                   # a FULL run
uv run mutmut-ratchet stats > mutation-stats.json
uv run mutmut-ratchet ratchet --mode floor --stats mutation-stats.json --update
uv run mutmut-ratchet timings                       # writes the shard weights
```

Commit both JSON files. Refresh the timings profile periodically; a stale one
degrades to a slightly suboptimal — never incorrect — split.

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.
