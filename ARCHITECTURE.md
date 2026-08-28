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

## Configuration

The service reads a single YAML file at the fixed path `/data/config.yml`. The file contains the
API key and the list of instances:

```yaml
api_key: "changeme"

instances:
  - name: user1
    phone: "+49123456789"
    pin: "1234"
```

All configuration is loaded once at startup through `tr_bridge/config.py`. No other module may call
`os.getenv` or read the file directly. There are no environment variables.

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

`GET /timeline` returns pytr event dicts exactly as received, including nested `details` /
`timelineDetailV2` payloads. The bridge applies no mapping, deduplication, or filtering beyond the
`[since, until)` time window.

Rationale:

- Mapping logic belongs in downstream consumers; keeping it out of the bridge avoids coupling the bridge
  to any specific integration's data model.
- pytr's event schema evolves with the TR app; an opinionated mapping would break whenever pytr adds or
  renames fields.
- Consumers must tolerate unknown keys — this is documented in the API reference.

---

## Authentication model

All endpoints except `GET /health` require an `X-API-Key` header validated against the `api_key`
field in `/data/config.yml`. The bridge is intended for intranet/private deployment; API-key auth is
sufficient for that threat model and avoids the overhead of OAuth or mTLS.

---

## Error handling

All errors are returned as [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457) with
`Content-Type: application/problem+json`. This provides a consistent, machine-readable error format.
Each error body includes a `code` field (snake_case) in addition to the standard `type`, `title`,
`status`, and `detail` fields to make programmatic handling easy without parsing URIs.

Domain errors carry their own HTTP mapping. Each subclasses `DomainError` (in `errors.py`) and
declares its `status`, `code` and `title` as class attributes; `detail` defaults to the exception
message but can be overridden for a fixed, message-independent explanation. A single generic handler
in `main.py` (`_domain_error_handler`) resolves *any* `DomainError` via the exception's MRO and calls
`to_problem_detail()`, so introducing a new domain error never requires editing the web layer. Only
the genuinely distinct cases keep dedicated handlers: `HTTPException`, `RequestValidationError`, and
the `Exception` catch-all.
