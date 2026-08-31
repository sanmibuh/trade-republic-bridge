# Architecture

This document describes the design rationale, internal structure, and key decisions behind `tr-bridge`.

---

## Purpose and scope

`tr-bridge` is a thin HTTP adapter. Its only responsibilities are:

1. Manage one or more Trade Republic sessions (login, 2FA, credential persistence).
2. Expose raw pytr timeline events over a REST API.

It contains **no business logic**: no SQLite, no Wallet integration, no Telegram notifications, no transaction
categorisation, and no event mapping. All of that belongs in downstream consumers.

---

## Framework: FastAPI

pytr's `Timeline` and websocket transport are built on Python `asyncio`. Wrapping them in a synchronous
framework (Flask, Django) would require bridging async ↔ sync at every call, increasing complexity and
defeating the purpose.

FastAPI was chosen because:

- It is natively async, matching pytr's concurrency model.
- It generates OpenAPI automatically (`/docs`, `/openapi.json`) with no extra work.
- Pydantic validation is built in, keeping request/response parsing simple.
- Resource overhead compared to Flask is marginal for this workload.

---

## Module structure — a light hexagonal architecture

The service follows a proportionate ports-and-adapters (hexagonal) layout. It
keeps the core of the pattern — a domain-driven use case that depends on a
**port**, with concrete **adapters** on either side — without the heavy
inter-layer DTO mapping that would be overkill for a thin wrapper.

```
tr_bridge/
  adapters/
    web/            # PRIMARY adapter: FastAPI routes, schemas, handlers, auth wiring
      routes.py
      schemas.py
      handlers.py
    pytr/           # SECONDARY adapter: implements the port using pytr
      pytr_client.py
  application/
    session.py      # USE CASE: login/2FA state machine — depends ONLY on the port
    ports.py        # SECONDARY port: Protocol TradeRepublicClient
  domain/
    state.py        # SessionState, LoginChallenge, and domain exceptions
  config.py
  errors.py         # RFC 9457 DomainError base + ProblemDetail
  auth.py
  timewindow.py
  instance_registry.py  # per-instance composition factory
  main.py           # composition root
```

### Boundary between the use case and the secondary adapter

`domain/state.py` is the pure core: it declares the states
(`idle/authenticator/push/confirmed/failed`), the `LoginChallenge`
(`authenticator | push`), and the domain exceptions. It depends on nothing but
the `DomainError` base.

`application/session.py` is the **use case**. It owns the asyncio-centric
orchestration — the per-instance `asyncio.Lock`, the 2FA timeout task, and the
background push-polling task — and drives the state transitions. It talks to
Trade Republic **exclusively through the `TradeRepublicClient` port**
(`application/ports.py`) and has no import of pytr. Only upstream failures
(`RateLimitedError`, `TrUpstreamError`) drive the machine to `failed`; a rejected
2FA code leaves the login pending for a retry.

`adapters/pytr/pytr_client.py` is the **secondary adapter** and the *only* module
that imports pytr. It is thin and holds no business state: it builds and caches
the `TradeRepublicApi` handle, bridges pytr's blocking calls onto a thread via
`run_in_executor`, and translates transport-level failures into domain errors
(HTTP 429 → `RateLimitedError`, other `requests` errors → `TrUpstreamError`,
`ValueError` on a bad code → `CodeRejectedError`, HTTP 401 on a timeline fetch →
`SessionExpiredError`).

`adapters/web/` is the **primary adapter**: `routes.py` defines the FastAPI
endpoints (thin — parse/echo the HTTP contract and delegate to the use case),
`schemas.py` holds the Pydantic request/response models, and `handlers.py` wires
the `X-API-Key` middleware and the RFC 9457 exception handlers.

`instance_registry.py` is the per-instance **composition factory**: for each
configured instance it builds a `PytrClient` and injects it into an
`InstanceSession` use case. `main.py` is the **composition root**: it builds the
app, registers the web adapter's handlers and routes, and constructs the registry
on startup.

### Why this shape

Depending on a `Protocol` port rather than on pytr restores the Dependency
Inversion Principle: the use case is exercised in tests through an in-memory fake
of the port (`tests/test_session.py`) with no pytr mocking, while the pytr-specific
translation is covered in isolation (`tests/test_pytr_client.py`). pytr is
referenced from a single module, so a change in its API has a single blast radius.

---


## Configuration

The service reads a single YAML file. In production the path is the fixed container default
`/data/config.yml`; for local development it can be overridden via the `TR_CONFIG_PATH`
environment variable. The file contains the API key and the list of instances:

```yaml
api_key: "changeme"

instances:
  - name: user1
    phone: "+49123456789"
    pin: "1234"
```

All configuration is loaded once at startup through `tr_bridge/config.py`. No other module may call
`os.getenv` or read the file directly. The only environment variable is `TR_CONFIG_PATH`, which
solely relocates the config file for local runs (a relative path would be fragile because it depends
on the process working directory); it is read exclusively in `config.py`.

A YAML file was chosen over environment variables because each instance carries structured data
(`name`, `phone`, `pin`); encoding a list of structs in env vars is error-prone. The file is mounted
as a Docker volume, keeping credentials out of the image and out of the command line.

The data root is always `/data` inside the container. Its actual location on the host is controlled
entirely by the Docker volume mount — there is no config option for it, because it would be
redundant: operators always decide the mount point from outside the container.

Each instance maps to a single Trade Republic account. Instances are defined in `/data/config.yml`
under the `instances` key and are addressed in every endpoint via a `{name}` path segment:

```
/instances/{name}/status
/instances/{name}/login
/instances/{name}/login/2fa
/instances/{name}/timeline
```

There is no single-instance convenience alias. All callers must always specify the instance name; this keeps
routing unambiguous and makes it straightforward to add or remove instances without changing URL structure.

Instance names are validated against `^[a-zA-Z0-9_-]+$`. Names containing `.` or `..` are rejected to
prevent path-traversal when constructing the session directory path.

---

## Session persistence

Each instance stores its session on disk:

```
/data/
  tr_session_user1/
    credentials.json
    cookies.txt
  tr_session_user2/
    credentials.json
    cookies.txt
```

`/data` is a mounted volume so session files survive container restarts. On startup the bridge
attempts `resume_websession()` for each instance; if a valid session is found, no login is required.

---

## Login / 2FA state machine

Trade Republic login has three possible outcomes after credentials are submitted:

- The existing session is still valid (`resume_websession()` succeeds) → **confirmed**
- A TOTP authenticator code is required → **authenticator**
- A push notification has been sent; the user must approve in the TR app → **push**

This is modelled as a per-instance state machine:

```
         POST /login
              │
              ▼
     resume_websession()
        succeeds?
       ┌────┴────┐
      yes        no
       │          │
       ▼          ▼
  confirmed   start weblogin
                  │
          ┌───────┴────────┐
          │                │
          ▼                ▼
    authenticator        push
          │                │
          │ POST /login/2fa│ (poll /status)
          │                │
          ▼                ▼
        push           confirmed
          │
          ▼
      confirmed
```

`idle` is the initial state and the state after a session is torn down.
`failed` is entered when pytr reports a hard error (wrong credentials, rate-limit, upstream failure).

State transitions are serialised per instance; a `409 login_in_progress` is returned if a second login is
initiated while one is already running.

### 2FA timeout

If a login enters `authenticator` or `push` state and no code is submitted / no approval arrives within the
configured timeout, the state transitions to `failed` and a subsequent `/status` call returns `failed`.
A fresh `POST /login` is required to retry.

### Why a state machine (and why it can't just delegate to pytr)

A recurring design question is whether the bridge could skip the state machine and simply delegate to
pytr. It cannot: there is a fundamental impedance mismatch between pytr — a **stateful, blocking,
multi-step SDK** — and the bridge's **stateless, polling REST API**. The five states
(`idle/authenticator/push/confirmed/failed`) are the minimum required to reconcile the two, not
business logic.

- **A multi-step conversation is split across separate HTTP requests.** pytr login is
  `initiate_weblogin()` and then, later, `complete_weblogin(code)` on the *same* `TradeRepublicApi`
  object, which holds the challenge / process id server-side. `POST /login` and `POST /login/2fa` are
  independent requests, so the in-flight api object must be held in memory between them. It cannot be
  serialized back to the client because it carries credentials and cookies.
- **`authenticator` vs `push` are distinct observable outcomes.** The caller must know whether to
  submit a code or to poll for approval. pytr only exposes a `weblogin_needs_authenticator` flag; the
  bridge must surface that distinction as queryable state.
- **`push` is blocking.** `complete_weblogin()` blocks until the user approves in the app. A REST
  request cannot be held open that long, so a background task drives the blocking call while the
  caller polls `/status` — which requires a "push in progress" state that resolves to `confirmed` or
  `failed`.
- **pytr offers no cheap status query.** There is no lightweight liveness ping; session expiry is only
  discovered via a `401` on `/timeline`. The current state must therefore be derived and tracked by
  the bridge, not read back from the library.
- **REST exposes concurrency that the SDK does not model.** Independent HTTP requests can race, hence
  the `409 login_in_progress` guard enforcing a single in-flight login per instance, and the 2FA
  timeout → `failed` policy that avoids stuck states and frees background tasks.

Each of the five states therefore maps to a distinct observable REST behaviour. This is the *minimum*
state required to bridge a stateful blocking flow onto a stateless polling API — removing it would
require either holding HTTP requests open indefinitely or leaking pytr's in-memory session object to
clients, neither of which is viable.

---

## No exposed session state

The TR session silently expires roughly every 24 hours regardless of cookie timestamps. Exposing a
`valid/expired` flag derived from cookie metadata would be misleading because:

- The expiry time in the cookie does not reflect the actual server-side session lifetime.
- There is no lightweight ping endpoint to verify liveness without side effects.

Instead, callers discover session expiry via `401 session_expired` on `GET /timeline` and react by initiating
a fresh login. This is the only reliable signal.

---

## Raw event pass-through

`GET /timeline` returns pytr event dicts essentially as received, including nested `details` /
`timelineDetailV2` payloads. The bridge applies no mapping, deduplication, or filtering beyond the
`[since, until)` time window.

### Typed guaranteed subset

pytr does **not** define a formal schema for timeline events: they are raw Trade Republic dicts. pytr
only accesses a handful of fields by direct indexing (`event["id"]`, `event["timestamp"]`,
`event["title"]`, `event["subtitle"]`) and injects its own `source`
(`timelineTransaction`/`timelineActivity`). Because a missing one of those keys would crash pytr
*before* the bridge ever receives the event, that subset is effectively a stable contract.

`TimelineResponse.events` is therefore typed as a list of `TimelineEvent`
(`adapters/web/schemas.py`), a Pydantic model that declares exactly that guaranteed subset
(`id`, `timestamp`, `source` as non-null strings; `title`, `subtitle` as nullable strings) and sets
`model_config = ConfigDict(extra="allow")` so every other upstream attribute — including the raw
`action` and `details` objects — is forwarded verbatim. This documents the contract in a single place
(surfacing it in OpenAPI) without mapping or renaming anything: consumers get a typed floor and full
passthrough for the rest. Declaring the subset is deliberately *not* business logic — it renames
nothing and transforms nothing; it only names the fields pytr already guarantees.

### Commonly-observed optional fields

Beyond the guaranteed subset, consumers routinely need a few Trade Republic fields that pytr does
**not** guarantee: the monetary `amount` (`{value, currency}`), the `eventType` category, and the
`status`. These are declared on `TimelineEvent` as **optional** (`amount: TimelineEventAmount | None`,
`eventType`/`status` as nullable strings), so they surface in OpenAPI for discoverability while their
absence never fails validation. The timeline route sets `response_model_exclude_unset=True` so these
optional fields are emitted only when the upstream event actually carries them — the response stays a
faithful echo and never injects `null` placeholders for fields TR omitted. This is a documentation
aid, not a mapping: values are forwarded verbatim, and typing them as required was rejected precisely
because TR could rename or drop them, which would otherwise 500 the bridge for every consumer.

The `[since, until)` window itself is parsed and validated by a dedicated `tr_bridge/timewindow.py`
module, keeping this domain-ish logic out of the FastAPI entry point. Its public surface is
`parse_window(since, until) -> (datetime, datetime)` and `to_utc_iso(dt)`; it owns the validation
contract (`since` required, `until` defaults to now, `until` strictly after `since`, date-only values
rejected) and raises `InvalidRequestError` (`400 invalid_request`) on malformed input. `parse_window`
assumes UTC for naive timestamps and preserves any explicit offset; converting to a `Z`-suffixed UTC
string is done separately by `to_utc_iso` when the route echoes the window back.

Rationale:

- Mapping logic belongs in downstream consumers; keeping it out of the bridge avoids coupling the bridge
  to any specific integration's data model.
- pytr's event schema evolves with the TR app; an opinionated mapping would break whenever pytr adds or
  renames fields.
- Consumers must tolerate unknown keys — this is documented in the API reference.

---

## Authentication model

All data endpoints require an `X-API-Key` header validated against the `api_key`
field in `/data/config.yml`. Only the base URL (`GET /`, which redirects to `/docs`), the liveness
probe (`GET /health`) and the public documentation endpoints (`/openapi.json`, `/docs`, `/redoc`) are
reachable without it. The bridge is intended for intranet/private deployment; API-key auth is
sufficient for that threat model and avoids the overhead of OAuth or mTLS.

The OpenAPI schema and the doc UIs (`/openapi.json`, `/docs`, `/redoc`) are intentionally public and
listed in the middleware's `_PUBLIC_PATHS` alongside `/health`. Hiding the schema would be security
through obscurity: it adds no real protection because the access control is the `X-API-Key` header on
the data endpoints, the route list is already public in the docs, and the (configurable) API key
never appears in the schema. Publishing it gives an always-up-to-date, importable API reference. The
schema declares an `apiKey` security scheme (`X-API-Key`) applied globally so Swagger UI's *Authorize*
and *Try it out* work; `/health` opts out via an empty per-operation `security` list.

---

## Error handling

All errors are returned as [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457) with
`Content-Type: application/problem+json`. This provides a consistent, machine-readable error format.
Each error body includes a `code` field (snake_case) in addition to the standard `type`, `title`,
`status`, and `detail` fields to make programmatic handling easy without parsing URIs.

Domain errors carry their own HTTP mapping. Each domain error subclasses `DomainError` (in
`errors.py`) and declares its `status`, `code` and `title` as class attributes; `detail` defaults to
the exception message but can be overridden for a fixed, message-independent explanation. A single
generic handler in the web adapter (`adapters/web/handlers.py::domain_error_handler`) resolves *any*
`DomainError` via the exception's MRO and calls `to_problem_detail()`, so introducing a new domain
error never requires editing the web layer. Only the genuinely distinct cases keep dedicated
handlers: `HTTPException`, `RequestValidationError`, and the `Exception` catch-all. The session
domain errors themselves live alongside the state machine's vocabulary in `domain/state.py`.

---

## Packaging and release pipeline

Distribution is **Docker-only**; the service is never published to PyPI. The
`Dockerfile` is a two-stage build: a `builder` stage installs the pinned runtime
dependencies (`requirements.txt`) into an isolated `/opt/venv`, and a slim
`runtime` stage copies only that virtualenv plus the `tr_bridge` package and the
`VERSION` file. It runs as a non-root `app` user that owns `/data` (the mounted
volume) and starts `uvicorn tr_bridge.main:app` on `0.0.0.0:8000`. The image is
published to `ghcr.io/sanmibuh/tr-bridge`.

Three GitHub Actions workflows automate quality and releases:

- **`ci.yml`** — runs on pushes to `main` and PRs targeting `main`: `ruff check`,
  `ruff format --check`, and `pytest` with 100% coverage enforced. On merge to
  `main` it also persists
  coverage stats to a `ci-data` branch and, on PRs, posts a coverage-diff comment.
- **`prepare-release.yml`** — a manually dispatched workflow that bumps the
  `VERSION` file (`patch`/`minor`/`major`), regenerates the `CHANGELOG.md`
  section via `scripts/generate-changelog.sh`, and opens a release PR. It uses a
  `PAT_RELEASE` token so the PR triggers CI.
- **`release.yml`** — triggers when `VERSION` changes on `main`. It creates the
  `vX.Y.Z` tag and GitHub Release (auto-generated notes) and builds/pushes the
  multi-arch (`linux/amd64,linux/arm64`) image tagged `vX.Y.Z` and `latest`.

The single source of truth for the version is the `VERSION` file, read at runtime
by `tr_bridge/main.py::_read_version()` to populate the OpenAPI/`/health` version. The
changelog follows [Keep a Changelog](https://keepachangelog.com/); the generator
script derives entries from commit subjects since the previous tag and is written
to be portable across BSD and GNU `awk`.
