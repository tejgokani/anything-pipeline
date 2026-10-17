# Security policy

anything-pipeline is a **pre-alpha, single-maintainer** project. It generates CI workflows that run in other people's repositories, so security reports are taken seriously, but response times are best effort.

## Supported versions

Only the latest commit on `main` and the latest `v0.0.x` release receive fixes.

## Reporting a vulnerability

Please report privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability**. Do not open a public issue or pull request for a security problem.

Please include:
- what is affected (`ap` command, a generated workflow, `verify.yml`, a pinned dependency),
- steps or a minimal repository that reproduces it,
- what an attacker gains.

I aim to acknowledge a report within 7 days and to agree a disclosure date with you. Reporters are credited unless they ask otherwise.

## In scope

- `ap detect` / `ap init` mishandling hostile repository content: path traversal or symlink escapes, writing outside the repository, injection into generated YAML or shell, terminal output spoofing, crashes or hangs on crafted input.
- `.github/workflows/verify.yml` and the caller workflows `ap init` renders: privilege escalation, secret exposure, script injection, unpinned or mutable references, ways to make a failing check pass.
- Supply-chain weaknesses in this repository's own build, release and CI configuration.

## Out of scope

- Vulnerabilities in third-party actions or tools themselves (report them upstream; tell me if a pinned version should be bumped).
- Issues that require the attacker to already have write access to your repository's workflows.
- Findings that apply to any CI system equally and are documented under "does not protect against" in the README (for example, a pull request editing the workflow it runs under).

## Supply-chain notes for users

- Always pin the kit by full commit SHA (`ap init --ref <40-hex SHA>`); review changes before you bump it.
- Keep Dependabot (or Renovate) enabled for your workflow files and use a cooldown on new versions.
