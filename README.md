# anything-pipeline

> **Pre-alpha (v0.0.1). Read this first.**
> Only the `verify` pipeline exists. The agent-safety pipelines that are the point of this project (`agent-gate`, `harden`, and the workflow audit) are **not built yet**. The reusable workflow has passed static checks and stubbed-tool tests but has **not yet been run on GitHub Actions**. Do not rely on it to protect a production repository.

Detect what kind of project a repository is, then install the right pipelines as thin, SHA-pinned GitHub Actions workflows that you can customise. The long-term goal is pipelines that are safe to use with AI coding agents: agents may push code without holding the keys.

## What exists today

| Piece | Status |
|---|---|
| `ap detect` – deterministic project-type detection (node, python, go; monorepos via pnpm, npm workspaces, `go.work`) | Built, unit-tested |
| `verify` – reusable workflow: lint, test, build for node, python, go and `mise`-based repos | Built; **never run on GitHub** |
| `gate` – one job to require in branch protection | Built (rendered into your workflow) |
| `ap init` – renders the caller workflow and `.anything-pipeline.yml` | Built, unit-tested |
| `agent-gate`, `harden`, `ship-package`, `ship-app`, `ap audit`, `ap doctor`, `ap sync` | **Not built** (see `DESIGN.md`) |

## Quick start

Requires Python 3.11+ and PyYAML. Nothing is installed by running it; it only reads your repository.

```bash
git clone https://github.com/tejgokani/anything-pipeline && cd anything-pipeline
python3 -m pip install 'PyYAML>=6'     # or run through uv: uv run --with 'PyYAML>=6' python cli/ap.py ...

# 1. See what it detects, and why
python3 cli/ap.py detect /path/to/your/repo --explain

# 2. Preview the files it would write (no ref needed)
python3 cli/ap.py init /path/to/your/repo --dry-run

# 3. Write them, pinned to this exact commit of the kit
python3 cli/ap.py init /path/to/your/repo --ref "$(git rev-parse HEAD)" --ref-label v0.0.1
```

Then commit the two files and, in your repository settings, make the **`gate`** check required. See [what it does not protect against](#threat-model).

If detection is ambiguous (for example a Python backend and a Node frontend in one directory), `ap init` writes nothing and tells you what to decide in `.anything-pipeline.yml`:

```yaml
overrides:
  units:
    ".": { verify: { flavor: node, vars: { pm: pnpm } } }
  verify:
    node-version: "22"
```

## What the `verify` workflow does

- Runs, per unit of your repo: lint/typecheck, test, build, using the tools your project already declares (package.json scripts, ruff/mypy/pytest config, `go vet`/`go test`/`go build`, `mise` tasks).
- **Fails closed:** an unknown flavor fails; a run in which no task executed fails; npm without a lockfile fails. Other package managers install from their frozen/immutable lockfile.
- Read-only token (`contents: read`), no secrets, no dependency caching, no `pull_request_target`.
- Every action is pinned to a full commit SHA, and caller values reach shell only through `env:`.

## Threat model

**What it is built to resist**

- A malicious repository being scanned by `ap`: no repo code is executed; files over 5 MB are ignored; symlinks that leave the repo are not read; terminal escape sequences and newlines in file names are neutralised before printing; deeply nested files do not crash it.
- Injection into the files `ap init` generates: unit paths, refs, labels and override values are validated and quoted, and `${{` is rejected in overrides.
- Overwriting your work: `ap init` refuses to replace a workflow you edited or did not generate (without `--force`) and refuses to write outside the repo.
- Mutable-tag attacks on actions: the kit pins its own dependencies by SHA and refuses unpinned `--ref` values unless you override it.

**What it does not protect against (yet)**

- **A pull request that weakens your CI.** On `pull_request`, GitHub runs the PR's own copy of the workflows, so a PR can delete the verify job and its `gate` still passes. Mitigate with code-owner review on `.github/` (see `.github/CODEOWNERS` in this repo for an example) and branch protection. The planned `agent-gate` pipeline addresses this; it does not exist yet.
- **Malicious dependencies.** CI installs your dependencies and runs your scripts. There are no secrets in the job, but there is no egress control either.
- **A compromised third-party action.** SHA pins stop a moved tag, not a malicious commit that you pinned. The workflow uses `actions/checkout`, `setup-node`, `setup-python`, `setup-go` and four third-party actions (`pnpm/action-setup`, `oven-sh/setup-bun`, `astral-sh/setup-uv`, `jdx/mise-action`), none of which I have audited line by line. GitHub's advisory database listed no advisories for them when last checked.
- **Prompt injection into AI agents running in CI.** No agent is run by this project today.
- **Anything about releases or deployments.** The ship pipelines are not built; do not put publish credentials into the `verify` workflow.

**What has and has not been verified**

- Verified: unit and golden-fixture tests; the workflow YAML passes `zizmor` and `actionlint`; shell steps are tested under stub tools; the Go step ran against the real Go toolchain.
- Not verified: any run on GitHub Actions; Windows; zizmor's online audits (they need a token and run in this repo's CI).

## Development

```bash
python3 -m pip install 'PyYAML>=6'
python3 -m unittest discover -s tests -t .
```

CI (`.github/workflows/self-ci.yml`) runs the tests on Python 3.11 and 3.14, plus `zizmor` and `actionlint` over the workflows. Dependencies in CI are hash-pinned.

## Reporting a vulnerability

See [SECURITY.md](SECURITY.md). Please do not open public issues for security problems.

## Roadmap

1. Run `verify` live on real repositories (next).
2. `agent-gate`: policy checks for agent-authored PRs, with attack-replay fixtures.
3. `harden` and `ap audit`: workflow hardening and a containment audit for agent workflows.
4. `ap sync`, then `ship-package` and `ship-app`.

Design notes and the full plan are in [DESIGN.md](DESIGN.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
