# Agent Guidelines

Instructions for AI assistants working on this project. Read this before making any changes.

Read `ARCHITECTURE.md` before making any changes to understand the service design, login/2FA state
machine, and key decisions.

---

## Project Overview

`tr-bridge` is a thin HTTP wrapper around [pytr](https://github.com/pytr-org/pytr). It manages Trade
Republic sessions (login, 2FA, credential persistence) and exposes raw timeline events over a REST API.

**No business logic.** This service must never contain SQLite, Wallet, Telegram, categorisation, or
mapping logic. All of that belongs in downstream consumers.

**Tech stack:** Python, FastAPI, Pydantic, pytr, Docker.

---

## Workflow

### Commits
- **Never create git commits.** The user reviews changes and commits manually.
- Prepare each improvement as a clean, self-contained change ready to commit, then stop and wait.
- Always propose a commit message following
  [Conventional Commits](https://www.conventionalcommits.org/es/v1.0.0-beta.2/)
  (`type(scope): description`). Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`.

### Code review comments
- **Evaluate before acting.** When a reviewer raises a concern, first assess whether it is
  technically correct and relevant to this project's context. Not every comment warrants a change.
- If a comment conflicts with an explicit design decision (e.g. Docker-only distribution,
  no PyPI packaging), explain the rationale and push back rather than blindly applying the change.
- If a comment is valid, apply it. If it is partially valid, apply the valid part and explain
  the rest. If it is incorrect or out of scope, say so clearly.
- Never implement a change just to satisfy a reviewer if you have a sound technical reason not to.

### TDD — tests first, always
1. Write the test and watch it fail before writing any implementation.
2. Implement the minimum code to make it pass.
3. Refactor with the tests as the safety net.

Never write implementation code without a corresponding test. Coverage should stay at or above 99%.

Tests must verify observable behaviour, not internal implementation details. Never expose private
methods, add artificial getters, or break encapsulation to make something testable — if you feel
the urge to do so, the code design is wrong; fix the design instead.

### Code quality
- **SOLID**: single responsibility, open/closed, no god objects.
- **Clean Code**: small functions, descriptive names, no magic numbers, no dead code.
- **OOP**: encapsulate state, prefer methods over module-level functions when state is involved.
- **DRY**: extract shared logic; never copy-paste across modules.
- Run `ruff format .` to auto-format all code before considering any task done.
- Run `ruff check .` before considering any task done. Fix all warnings — do not suppress with `noqa`
  unless genuinely justified.

---

## Documentation

### Update `README.md` when:
- An endpoint is added, removed, or its request/response shape changes.
- A new environment variable is introduced or an existing one is renamed/removed.
- Setup steps or deployment instructions change.

### Update `ARCHITECTURE.md` when:
- A new module is added or an existing one is renamed/removed.
- A key design decision changes (e.g. session persistence, auth model, error format).
- The login/2FA state machine gains new states or transitions.
- A new workflow or release mechanism is introduced.

### Roadmap items
- Roadmap items are tracked as GitHub issues with the `roadmap` label.
- When a roadmap item is implemented, close the corresponding issue via the PR.
- New improvement ideas should be opened as issues with the `roadmap` label.

---

## Project conventions

- All environment variables are read in a single `config.py` module — never call `os.getenv` directly
  in other modules.
- Instance names must match `^[a-zA-Z0-9_-]+$`. Names containing `.` or `..` are always rejected.
- Session files live under `{TR_DATA_ROOT}/tr_session_{name}/`. Never construct that path outside the
  session management layer.
- All error responses follow RFC 9457 Problem Details (`application/problem+json`). Never return a
  plain JSON error outside of that contract.

---

## Versioning

- The `VERSION` file at the repo root is the single source of truth for the release version.
- Bumping `VERSION` on `main` automatically triggers tag creation, GitHub release, and image build
  via CI.
- Update the image tag in the relevant compose file after releasing.

---

## Before finishing any task
- Run `ruff format .` — no unformatted code may be left.
- Run `ruff check .` — no warnings.
- Ensure all new code has tests and coverage stays at or above 97%.

---

## Local smoke tests

- Never touch the developer's local `data/` directory. It may hold a real `config.yml` and live
  `tr_session_*` folders; deleting or overwriting it is destructive.
- When a manual/live smoke test needs a config file, create it in a throwaway temp directory outside
  the repo (e.g. under `/var/folders/.../opencode/`) and point `TR_CONFIG_PATH` at it. Clean up that
  temp directory afterwards — never run `rm -rf data` or otherwise clobber the repo's `data/`.
