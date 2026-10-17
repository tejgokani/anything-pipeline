# anything-pipeline: repo layout and detection manifest (draft v0)

Status: Phases 0 and 1 are implemented: detection (`ap detect`), the `verify` reusable workflow (node, python, go, generic flavors), the `gate` job, and `ap init` (renders the caller workflow and `.anything-pipeline.yml`), with 161 tests, plus pre-publication hardening (section 4b). Not yet validated by a real GitHub Actions run. Everything else is still design. Names are placeholders.

## 1. Principles

1. **Layers, not alternatives.** Three pipelines run on every repo (`verify`, `agent-gate`, `harden`). Detection adds one `ship-*` pipeline.
2. **Detection is deterministic.** Plain file-based rules, explainable, unit-tested against fixtures. The agent never decides in CI.
3. **The agent is the installer and maintainer.** It resolves ambiguity at `init` time, writes overrides, and opens update PRs. It is not the runtime.
4. **Thin callers, fat reusable workflows.** The user's repo holds small caller workflows they can edit. The logic lives in versioned, SHA-pinned reusable workflows here. Renovate or Dependabot bump the pins, so a customised repo does not rot.
5. **Every layer of customisation has an exit.** Inputs, then overrides, then `eject` (copy the pipeline into your repo and own it).
6. **This repo obeys its own rules.** Least-privilege `permissions`, SHA pins, no `pull_request_target`, zizmor in CI.

## 2. Repo layout

```
anything-pipeline/
├── README.md
├── DESIGN.md
├── .github/workflows/            # reusable workflows (workflow_call). Must live here to be callable.
│   ├── verify.yml
│   ├── agent-gate.yml            # unprivileged PR job; emits report artifact
│   ├── agent-gate-report.yml     # privileged workflow_run job; treats artifact as data
│   ├── harden.yml
│   ├── ship-package.yml          # release PR + trusted publishing
│   ├── ship-app.yml              # build, attest by digest, OIDC deploy
│   └── self-ci.yml               # this repo's own CI (zizmor, fixture tests)
├── pipelines/                    # one folder per pipeline: metadata + flavors + docs
│   ├── verify/
│   │   ├── pipeline.yml          # pipeline manifest (section 4)
│   │   ├── flavors/              # node.yml python.yml go.yml rust.yml java.yml dotnet.yml generic.yml
│   │   └── README.md
│   ├── agent-gate/
│   │   ├── pipeline.yml
│   │   ├── policy.default.yml    # path rules, diff-size cap, coverage ratchet, trailer rules
│   │   └── README.md
│   ├── harden/
│   │   ├── pipeline.yml
│   │   └── README.md
│   ├── ship-package/
│   │   ├── pipeline.yml
│   │   ├── flavors/              # npm.yml pypi.yml crates.yml go-module.yml
│   │   └── README.md
│   └── ship-app/
│       ├── pipeline.yml
│       ├── flavors/              # container.yml platform.yml worker.yml
│       └── README.md
├── detect/
│   ├── rules.yml                 # detection manifest (section 3)
│   ├── schema/                   # JSON Schemas for rules.yml, pipeline.yml, .anything-pipeline.yml
│   └── engine/                   # reads rules.yml, walks the repo, emits a plan
├── actions/                      # composite actions shared by workflows
│   ├── setup-toolchain/          # picks pm/runtime from the flavor, restores cache
│   ├── gate/                     # (not built; Phase 1 renders the gate job inline, see section 4a)
│   └── check-agent-trailer/
├── presets/                      # files copied into the user's repo
│   ├── renovate.json
│   ├── dependabot.yml
│   ├── CODEOWNERS.agent-gate
│   ├── zizmor.yml
│   └── SECURITY.md
├── templates/                    # Copier template: renders thin callers + .anything-pipeline.yml
│   ├── copier.yml
│   └── .github/workflows/
│       └── {{ pipeline }}.yml.jinja
├── cli/                          # `ap` command
│   └── (init | detect --explain | sync | eject | doctor)
├── tests/
│   └── fixtures/                 # tiny sample repos + expected plan (section 7)
│       ├── npm-library/
│       ├── next-app-vercel/
│       ├── python-uv-library/
│       ├── go-service-container/
│       ├── pnpm-turbo-monorepo/
│       └── ambiguous-polyglot/
└── docs/
    ├── security-invariants.md
    ├── customising.md            # inputs, overrides, eject
    └── adding-a-pipeline.md
```

Why two places for a pipeline (`.github/workflows/x.yml` and `pipelines/x/`): GitHub only resolves `uses: owner/repo/.github/workflows/x.yml@ref`, so the workflow file has to be there. Everything else (metadata, flavors, docs, defaults) lives under `pipelines/` so the manifest reader never parses workflow YAML.

## 3. Detection manifest: `detect/rules.yml` (implemented, Phase 0)

`detect/rules.yml` is the authoritative manifest; this document no longer copies it, to avoid drift. It has three evaluation stages (signals, then kinds, then selection) plus flavors and workspaces. All rules are declarative and contain no code. The loader (`detect/engine/rules.py`) validates unknown keys, unknown signal references, signal cycles and unknown kinds.

Vocabulary (kept deliberately small):
- **Leaves:** `file`, `file_any`, `glob`, `json` and `toml` (`path` exists / `equals` / `exists: false`). A mapping holds one leaf.
- **Combinators:** `all`, `any`, `none`. Several in one mapping are AND-ed, e.g. `{ all: [node], any: [next_app, vercel_cfg], none: [npm_publishable] }`.
- **Modifier:** `search_up: true` on a leaf also looks in parent directories up to the repo root (lockfiles and `mise.toml` live at the workspace root).
- **Selection:** `pipelines.ship[].when: { kind_any: [...] }`.
- **Flavors:** `flavors.verify` is first-match per language; two different languages in one unit is ambiguous; `exclusive: true` stops evaluation (used for `mise` tasks, which win).
- **Workspaces:** `units_from` is one of `pnpm-workspace.yaml`, `package.json` (`workspaces`), `go.work`. `affected` is a changed-only command template (turbo, nx).

Rules for the engine:
- Pure function of the file tree. No network, no LLM, no execution of repo code. Symlinks that resolve outside the repo are not read. Files over 5 MB are ignored.
- `ap detect --explain` prints every signal that fired (with the file that triggered it), the kind rules that matched, and the rule index behind each flavor and ship decision.
- Ambiguity is never silently resolved (see section 6).
- A missing or unparsable JSON/TOML file never matches a `json`/`toml` leaf, even with `exists: false`.

### Output: the plan

`ap detect --json` prints the plan below (verified by the six golden fixtures). With `--explain`, each unit also carries an `explain` object and the workspace carries its matched rules; decisions are identical.

```json
{
  "version": 1,
  "ambiguous": false,
  "problems": [],
  "always": ["verify", "agent-gate", "harden"],
  "workspace": null,
  "units": [
    {
      "path": ".",
      "kinds": ["npm-library"],
      "verify": { "flavor": "node", "vars": { "pm": "pnpm" } },
      "ship":   { "pipeline": "ship-package", "flavor": "npm" },
      "ambiguous": false,
      "reasons": []
    }
  ]
}
```

`problems` holds repo-level issues (for example a workspace whose members cannot be resolved). `reasons` holds per-unit ambiguity. `ap init` stores the confirmed plan in the user's repo (section 5).

Known classification limits (found by running on real repos): frameworks other than Next.js (Vite, Astro, Nuxt, ...) are not classified, so such apps come out ambiguous; a repo with two languages in one unit (for example a Python backend plus a Node frontend, as in open-webui) is ambiguous until multi-language units are supported; "has a `name` and is not `private`" counts as publishable, so Electron apps are excluded explicitly via `electron_app`.


## 4. Pipeline manifest: `pipelines/<id>/pipeline.yml`

Describes a pipeline to the engine, the CLI and humans. It does not duplicate the workflow.

```yaml
id: ship-package
layer: ship                        # always | ship
summary: Release PR plus trusted publishing for libraries and CLIs
requires: [verify]                 # must be enabled and green before this runs
workflow: .github/workflows/ship-package.yml
flavors: [npm, pypi, crates, go-module]

inputs:                            # what a caller may set in `with:`
  release_tool:      { type: choice, options: [release-please, changesets], default: release-please }
  publish_env:       { type: string, default: release }      # GitHub environment with protection rules
  provenance:        { type: boolean, default: true }

permissions:                       # documented contract; checked by zizmor and `ap doctor`
  default: {}                      # nothing unless granted per job
  jobs:
    release-pr: { contents: write, pull-requests: write }
    publish:    { contents: read, id-token: write }          # OIDC only on the publish job

secrets: []                        # none expected: trusted publishing means no stored tokens

guards:                            # invariants `ap doctor` verifies in the user's repo
  - no_long_lived_publish_token
  - publish_job_uses_protected_environment
  - workflow_filename_matches_trusted_publisher   # npm and PyPI match on the exact filename

repo_settings:                     # things the workflow cannot set itself; `ap doctor` lists them
  - "npm: require 2FA and disallow tokens after trusted publisher is configured"
  - "PyPI: register trusted publisher (owner, repo, workflow file, environment)"
```

Per-pipeline specifics worth recording here from the research:
- `verify`: expose a `gate` job (always runs, `needs:` every conditional job). Branch protection requires only `gate`, which avoids pending required checks when a job is skipped.
- `agent-gate`: `policy.default.yml` holds protected paths (`.github/workflows/**` always), max diff size, coverage ratchet, and the commit trailer format. The report job is the only one with write access, and it treats the artifact as untrusted data.
- `harden`: runs zizmor, dependency review, secret scanning, Scorecard. Presets add Renovate (digest pinning, release-age delay) or Dependabot (cooldown).

### 4a. As built in Phase 1: `verify` and `ap init`

**`.github/workflows/verify.yml`** (reusable, zone 1). Inputs: `flavor`, `pm`, `working-directory`, `node-version`, `python-version`, `pnpm-version`, `poetry-version`, `ignore-scripts`, `check-command`, `test-command`, `build-command`, `allow-empty`, `allow-unlocked`. Every job has an explicit `name:` so check names stay stable. One job per flavor plus `unsupported-flavor`.
- Default tasks. node: `lint`/`typecheck`/`type-check` scripts, then `test`, then `build` (npm's placeholder test script is ignored). python: `ruff check` and `mypy` when configured, `pytest` when tests or config exist, `uv build` / `poetry build`. go: `gofmt -l` plus `go vet`, `go test ./...`, `go build ./...`. generic: `mise run` for whichever of `check`/`test`/`build` mise.toml defines.
- **Fails closed:** an unknown flavor fails; a node, python or generic run in which no task executed fails unless `allow-empty` is set; npm with no lockfile fails unless `allow-unlocked` is set (all other package managers already install frozen/immutable).
- Command inputs replace the default for that task. All caller values reach shell through `env:`, never interpolated into `run:`.
- All actions are pinned to full commit SHAs with the version in a comment. SHAs were resolved with `git ls-remote` on 2026-10-17 and each action's input names were checked at the pinned commit. Third-party actions in use: `pnpm/action-setup`, `oven-sh/setup-bun`, `astral-sh/setup-uv`, `jdx/mise-action`.
- **Deviation from the zone table:** no dependency caching in v1 (zone 1 is meant to be cache read-only; `setup-node`, `setup-python` and `setup-uv` cannot restore without also saving). Slower but removes the cache-poisoning path. A restore-only cache plus a save job on the default branch is the follow-up.

**Gate.** Rendered inline into the caller (about 8 lines of shell over `toJSON(needs)` and `jq`), not a composite action. A composite action in this repo could only be referenced by a SHA of this repo that is unknown at commit time, which would force a mutable tag reference. Revisit with the release process in a later phase. `skipped` counts as pass; `failure` and `cancelled` fail.

**`ap init`.** Detect, then apply overrides from `.anything-pipeline.yml`, then render. The caller carries a header and a sha256 of its body. Re-running `init` overwrites an unedited file, refuses an edited or foreign file without `--force`, and writes nothing if any target is refused. Customise through `overrides:` (kept across runs) or edit the caller and accept that `init` will then refuse to overwrite it.
- `--ref` must be a full 40-hex SHA (`--allow-unpinned` for tags, with a warning). `--dry-run` needs no ref.
- Unit paths become YAML, so only `[A-Za-z0-9._@+-]` and `/` are allowed; others block rendering. Override strings may not contain `${{` or newlines, and only the declared input names are accepted.
- Targets must stay inside the repo: symlinked `.github` or plan file pointing out is refused.
- Phase 1 renders `verify` only. `agent-gate` and `harden` are reported as not available; ship pipelines are detected and listed but not rendered.

Overrides schema (validated by `render/planfile.py`):

```yaml
overrides:
  verify:                       # inputs applied to every unit
    node-version: "22"
  units:
    "apps/web":
      verify: { flavor: node, vars: { pm: pnpm } }   # replaces detection (resolves ambiguity)
      inputs: { test-command: "pnpm test:ci" }
disabled: []                    # e.g. [verify]
```

Known gaps: not run on GitHub (only static checks, scripts under stub tools, and the Go script with the real toolchain); `pnpm/action-setup` with an empty `version` input relies on `packageManager` in package.json (`init` prints a note when it is missing); npm workspaces installs should use `working-directory: "."`.

Lint status (2026-10-17): zizmor 1.31.1 (default, auditor and pedantic personas, **offline**) and actionlint 1.7.12 are clean on `verify.yml` and rendered callers for the monorepo, Go and Python fixtures. zizmor's one low finding (an unlocked `npm install`) was fixed by making it an explicit opt-in, and that single line carries an inline `zizmor: ignore`. Not covered: zizmor's online audits (impostor commits, known-vulnerable actions, stale version comments; need a GitHub token), and actionlint's shellcheck and pyflakes integrations (neither tool is installed; `tests/test_workflow_invariants.py` runs `bash -n` and a strict-mode check on every run block instead).

### 4b. Pre-publication hardening (as built)

Found by probing the CLI with hostile inputs, then fixed with tests:
- **Terminal safety.** `detect/engine/text.py: escape_controls` turns control, format (including bidi overrides), surrogate and line-separator characters into visible escapes. Applied to every repo-derived value in `render_text` and, as a second layer, to all CLI output (`emit`). Before: an ANSI escape plus a newline in a directory name could clear the screen and forge a `result: clear` line. Non-UTF-8 file names no longer crash printing.
- **Pathological nesting.** Deeply nested JSON, TOML or YAML (a 400 KB `package.json` crashed the CLI with `RecursionError`) is now treated as unreadable; the CLI also has a last-resort handler.
- **Runtime guard.** `cli/ap.py` checks for Python 3.11+ and PyYAML before any other import and prints how to proceed (macOS system Python 3.9 used to crash on `tomllib`). The file stays parseable by Python 3.9 so the message can be shown.
- **Packaging and repo hygiene.** `pyproject.toml` (Python 3.11+, PyYAML, `ap` entry point; the wheel was built and its installed `ap` run from outside the repo), Apache-2.0 `LICENSE`, `README.md` with an explicit pre-alpha status and threat model, `SECURITY.md`, `.github/CODEOWNERS`, `.github/dependabot.yml` with a 7-day cooldown, and `self-ci.yml` (tests on Python 3.11 and 3.14 on Linux and macOS, plus zizmor and actionlint). CI Python dependencies are installed with `--require-hashes` from files that carry every release hash; `tests/test_repo_hygiene.py` guards all of this, including that the README never overclaims.

Still open: top-level package names `cli`, `detect`, `render` are generic (fine for uvx/pipx, rename before PyPI); Dependabot's handling of `--hash` requirement files is unverified; the self-CI has not run on GitHub; the non-UTF-8 file-name test only runs on filesystems that allow such names (skipped on macOS).

## 5. What lands in the user's repo

```
their-repo/
├── .anything-pipeline.yml          # plan + overrides + template version (the single source of truth)
├── .copier-answers.yml             # Copier's own file; never edit by hand
├── .github/
│   ├── workflows/
│   │   ├── verify.yml              # ~15 lines: uses: owner/anything-pipeline/.github/workflows/verify.yml@<sha>  # v1.4.0
│   │   ├── agent-gate.yml
│   │   ├── harden.yml
│   │   └── ship-package.yml
│   ├── CODEOWNERS                  # includes .github/workflows/ via preset
│   └── dependabot.yml | renovate.json
└── SECURITY.md
```

`.anything-pipeline.yml` example:

```yaml
version: 1
template: { repo: owner/anything-pipeline, ref: v1.4.0 }

plan:                                # written by `ap init` from the detected plan
  units:
    - path: .
      kinds: [npm-library]
      verify: { flavor: node, vars: { pm: pnpm } }
      ship:   { pipeline: ship-package, flavor: npm }

overrides:                           # human or agent edits; survive `ap sync`
  verify:
    node_version: "22"
    test_command: "pnpm test:ci"
  agent-gate:
    protected_paths: ["src/billing/**", ".github/workflows/**"]
    max_diff_lines: 800
  ship-package:
    release_tool: changesets

disabled: []                          # e.g. [ship-package] to opt out of a ship pipeline
ejected: []                           # pipelines copied locally; sync will not touch them
```

### Commands

| Command | What it does |
|---|---|
| `ap detect [--explain] [--json]` | Run the manifest against the repo. No writes. |
| `ap init --ref <sha>` | **Implemented (verify only).** Detect, apply overrides, render the caller workflow and `.anything-pipeline.yml`. Refuses ambiguous plans (exit 2) and unpinned refs. `--dry-run` previews. |
| `ap sync` | Copier update plus pin bumps. Opens a PR. Conflicts appear as merge markers. |
| `ap eject <pipeline>` | Inline the reusable workflow into the repo and mark it `ejected`. |
| `ap doctor` | Check pipeline guards and the repo settings the workflows cannot set (branch protection requires `gate`, trusted publisher configured, CODEOWNERS covers workflows). |

### Where the agent fits
- **`ap init`, ambiguous result:** the agent reads the repo, proposes the missing kind or flavor, and writes it into `.anything-pipeline.yml`. A human confirms in the PR.
- **Customisation:** "use Changesets", "protect `src/billing`": the agent edits `overrides:` and never the shared workflows.
- **Maintenance:** the agent triages `ap sync` conflicts and pipeline failures, with the same read-only rules as `agent-gate`.
- **Never:** choosing pipelines at CI run time.

## 6. Ambiguity handling

Conditions that set `ambiguous: true`:
- no kind matches a unit
- two kinds with different ship pipelines match one unit (for example `npm-library` and `container-app`)
- a workspace tool is detected but its unit list cannot be resolved
- `mise.toml` exists but defines none of `check`, `test`, `build` (verify would be empty)

The CLI never guesses. Without an agent it asks the user. With an agent it proposes an `overrides:` block as a diff.

## 7. Testing the manifest

Each fixture is a minimal repo with an `expected.plan.json`. The self-CI job runs `ap detect --json` on every fixture and diffs against the expected plan. Adding a new kind or flavor requires a fixture.

```
tests/fixtures/pnpm-turbo-monorepo/
├── package.json
├── pnpm-lock.yaml
├── pnpm-workspace.yaml
├── turbo.json
├── apps/web/package.json              # next dependency, no publish
├── packages/ui/package.json           # publishable library
└── expected.plan.json                 # two units: web-app (ship-app) and npm-library (ship-package)
```

## 8. Suggested build order

1. **Done (Phase 0):** `detect/rules.yml` for node, python, go and the engine plus `--explain`, with 6 fixtures. No workflows yet.
2. **Done (Phase 1):** `verify` (node, python, go, generic flavors), the inline `gate` job, and `ap init`.
3. `agent-gate` as two workflows (unprivileged and report) plus the default policy.
4. `harden` plus presets (zizmor, Renovate or Dependabot, CODEOWNERS).
5. Copier template, `ap init` and `ap sync`.
6. `ship-package` (npm and PyPI first), then `ship-app` (container first).
7. Agent-assisted `init` for ambiguous plans, last. It adds value only once the deterministic path is solid.

## 9. Open questions

- **Single repo or split?** Reusable workflows must be in `.github/workflows/`, which fits one repo. A separate CLI repo is possible later.
- **Language for the engine.** Python (matches Copier, easy for contributors) or Go/Rust (one static binary). The manifest format is independent of this choice.
- **Release tool default.** release-please versus Changesets is still unresolved from the research: release-please is simpler for single packages, and Changesets was repeatedly reported as better for monorepo intent. Treat as unverified until tested.
- **Mixed-language units.** The verify flavor list is first-match, so a Python project with a tooling-only `package.json` would pick the node flavor. Likely fix: flavor rules that also check which manifest sits at the unit root, or a `primary_language` override. Needs a fixture.
- **Private repos.** npm provenance is public-only. Whether GitHub artifact attestations work on private repos was not confirmed. `ship-*` flavors need a documented fallback.
- **Non-GitHub CI.** Out of scope for v1. The manifest and plan are CI-agnostic, so a GitLab renderer could be added later.

## 10. Trust zones and agent safety (from the strategy plan)

Positioning: let agents push code without letting them hold the keys. Every job in every pipeline declares a trust zone, and `ap audit` (Phase 3) enforces the zone rules.

| Zone | What runs | Token | Secrets | Cache |
|---|---|---|---|---|
| 0 untrusted | fork code, issue and PR text | none | none | no write |
| 1 unprivileged CI | verify, agent review | `contents: read` | none | read-only |
| 2 reporting | `workflow_run` job that treats artifacts as data | scoped write | minimal | none |
| 3 release | publish, deploy | OIDC on that job only | protected environment | no restore |

Containment audit (`ap audit`, N1) checks any workflow that runs an agent action: no secrets in the agent job, read-only token, actor gating (`author_association`), untrusted text passed as data, outputs scanned for secrets, no cache restore in release jobs, no `pull_request_target` checkout. The attack-replay fixtures in `tests/attacks/` (Cline, hackerbot-claw, Comment and Control patterns) are both the test suite and the proof.

Phase order and exit criteria are in the strategy plan; build order for the pipelines is section 8.
