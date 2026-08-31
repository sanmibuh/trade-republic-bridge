# tr-bridge

A thin HTTP wrapper around [pytr](https://github.com/pytr-org/pytr) that manages Trade Republic sessions
(login, 2FA, credential persistence) and exposes timeline events over a REST API.

**No business logic.** This service contains no SQLite, Wallet, Telegram, categorisation, or mapping logic —
it only wraps pytr.

---

## Quickstart

### Prerequisites

- Docker (recommended) or Python 3.12+
- A Trade Republic account per instance you want to expose

### Running with Docker

```bash
docker run -d \
  --name tr-bridge \
  -p 8000:8000 \
  -v /path/to/data:/data \
  ghcr.io/sanmibuh/tr-bridge:latest
```

The host directory `/path/to/data` must contain `config.yml`. Session files are written there
automatically under `tr_session_{name}/` subdirectories.

```
/path/to/data/
  config.yml           ← your config file
  tr_session_user1/    ← created automatically on first login
  tr_session_user2/
```

### Running with docker-compose

A ready-to-use [`docker-compose.yml`](docker-compose.yml) is provided:

```bash
mkdir -p data
cp config.example.yml data/config.yml   # then edit it (api_key, instances)
docker compose up -d
```

It mounts `./data` into the container at `/data` and exposes the API on
`http://127.0.0.1:8000`. To build the image locally instead of pulling it from
GHCR, comment out the `image:` line and uncomment `build: .` in the compose file.


### Running locally

```bash
make install                      # create .venv and install deps
mkdir -p data                     # local config/session directory
cp config.example.yml data/config.yml   # then edit it (see Configuration)
make run                          # starts uvicorn on http://127.0.0.1:8000 with autoreload
```

By default the app reads `/data/config.yml`. For local development set the
`TR_CONFIG_PATH` environment variable to point elsewhere — the `make run` target
already defaults it to the repo's `data/config.yml` (resolved to an absolute
path). To override it:

```bash
TR_CONFIG_PATH=/path/to/config.yml make run
```

> **Note:** without a readable config file the server exits immediately with a
> `ConfigError`. Create it first (see [Configuration](#configuration)).

Interactive API docs are public: Swagger UI at `/docs`, ReDoc at `/redoc`, and the
raw schema at `/openapi.json`. The base URL (`/`) redirects to the Swagger UI, so
opening `http://127.0.0.1:8000/` lands directly on the docs. They require no API
key — access control is the `X-API-Key` header on the data endpoints, and the
schema contains no secrets. The schema is also handy for importing a collection
into Postman/Insomnia.

---

## Configuration

The service reads `/data/config.yml` on startup and writes session files under `/data/tr_session_{name}/`. Mount `/data` as a writable volume.

```yaml
# config.yml
api_key: "changeme"           # secret key required in X-API-Key header

tfa_timeout: 120              # seconds to wait for 2FA / push confirmation (default: 120)

instances:
  - name: user1               # session subdirectory name; must match ^[a-zA-Z0-9_-]+$
    phone: "+49123456789"     # Trade Republic phone number
    pin: "1234"               # Trade Republic PIN
  - name: user2
    phone: "+49987654321"
    pin: "5678"
```

There are no required environment variables. The data location inside the container is always
`/data`; mount the volume wherever you need it on the host.

For local development, `TR_CONFIG_PATH` overrides the config file location (default
`/data/config.yml`). Docker deployments should leave it unset.

Session files (`credentials.json`, `cookies.txt`) are stored under `/data/tr_session_{name}/`,
one directory per instance.

---

## Authentication

All endpoints **except** `GET /health` require the header:

```
X-API-Key: <value of api_key in config.yml>
```

Missing or invalid keys return `401 unauthorized`.

---

## API Reference

### `GET /health`

Liveness probe. No authentication required.

**Response `200`**
```json
{
  "status": "ok",
  "service": "tr-bridge",
  "version": "0.1.0",
  "dependencies": {
    "pytr": "0.4.1",
    "python": "3.12.4"
  }
}
```

---

### `GET /instances`

List configured instance names.

**Response `200`**
```json
{ "instances": ["user1", "user2"] }
```

---

### `GET /instances/{name}/status`

Current login-flow state for the instance.

**Response `200`**
```json
{ "name": "user1", "state": "idle" }
```

`state` is one of: `idle | authenticator | push | confirmed | failed`

| State           | Meaning |
|-----------------|---------|
| `idle`          | No login in progress |
| `authenticator` | Waiting for a TOTP code via `POST .../login/2fa` |
| `push`          | Waiting for user approval in the TR app |
| `confirmed`     | Session established, timeline calls are possible |
| `failed`        | Login attempt failed |

---

### `POST /instances/{name}/login`

Initiate a login. Attempts `resume_websession()` first; if no valid session exists, starts a web login
and returns the required 2FA path.

**Request body:** none

**Responses `200`**
```json
{ "state": "confirmed" }
```
```json
{ "state": "authenticator" }
```
```json
{ "state": "push" }
```

| Problem code        | HTTP | When |
|---------------------|------|------|
| `login_in_progress` | 409  | A concurrent login is already running for this instance |
| `rate_limited`      | 429  | TR login rate-limit hit |
| `tr_upstream_error` | 502  | pytr / TR websocket or HTTP failure |

---

### `POST /instances/{name}/login/2fa`

Submit the TOTP authenticator code for a login that is in `authenticator` state.

**Request body**
```json
{ "code": "123456" }
```

**Responses `200`**
```json
{ "state": "push" }
```
```json
{ "state": "confirmed" }
```

| Problem code       | HTTP | When |
|--------------------|------|------|
| `code_rejected`    | 401  | The code was wrong |
| `no_login_pending` | 409  | No authenticator login is currently awaiting a code |
| `rate_limited`     | 429  | TR login rate-limit hit |
| `tr_upstream_error`| 502  | pytr / TR websocket or HTTP failure |

---

### `GET /instances/{name}/timeline`

Fetch raw pytr timeline events in the half-open interval `[since, until)`.

**Query parameters**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `since`   | yes      | ISO-8601 timestamp (must include a time component), inclusive lower bound |
| `until`   | no       | ISO-8601 timestamp (must include a time component), exclusive upper bound (default: now); must be later than `since` |

**Response `200`**
```json
{
  "instance": "user1",
  "since": "2026-08-01T00:00:00Z",
  "until": "2026-08-10T00:00:00Z",
  "count": 1,
  "events": [
    {
      "id": "a1b2c3d4-...",
      "timestamp": "2026-08-03T10:12:04.000+0000",
      "source": "timelineTransaction",
      "title": "Supermarket XY",
      "subtitle": "Karte",
      "eventType": "CARD_TRANSACTION",
      "status": "EXECUTED",
      "amount": { "value": -12.34, "currency": "EUR" },
      "action": { "type": "timelineDetail", "payload": "a1b2c3d4-..." },
      "details": { "sections": [ "...raw timelineDetailV2 payload, unchanged..." ] }
    }
  ]
}
```

Each event carries a minimal **guaranteed floor**, a set of
**commonly-observed optional fields**, and an upstream-dependent remainder:

| Field       | Type            | Guarantee |
|-------------|-----------------|-----------|
| `id`        | string          | Always present and non-null (pytr uses it as an event key). |
| `timestamp` | string          | Always present and non-null (ISO-8601, pytr's original format, unmodified). |

These two fields are accessed directly by `pytr` (and `timestamp` drives the
`[since, until)` window), so a valid event is guaranteed to contain them —
otherwise pytr would fail before the bridge sees the event. They are the only
required, non-null keys.

In addition, the following fields come from Trade Republic's payload and are
frequently needed by consumers. They are **optional**: documented in the schema
for discoverability, but their absence never fails validation, and they are
omitted from the response when the upstream event does not include them. This
keeps the endpoint resilient against upstream shape drift instead of returning a
500 at response time.

| Field       | Type                         | Notes |
|-------------|------------------------------|-------|
| `source`    | string \| null               | Added by pytr — typically `timelineTransaction` or `timelineActivity`. |
| `title`     | string \| null               | Short description; value may be `null`. |
| `subtitle`  | string \| null               | Notes/secondary description; value may be `null`. |
| `amount`    | object \| null               | `{ "value": number \| string \| null, "currency": string \| null }`; `value` keeps its upstream type (an integer stays an integer — no coercion); extra keys (e.g. `fractionDigits`) pass through. |
| `eventType` | string \| null               | TR event category, e.g. `CARD_TRANSACTION`, `BANK_TRANSACTION_INCOMING`. |
| `status`    | string \| null               | TR execution status, e.g. `EXECUTED`, `CANCELED`. |

**Everything else is passed through unchanged and is _not_ guaranteed by the
bridge** — notably the nested `action` and `details` (raw `timelineDetailV2`)
objects, plus any other attribute Trade Republic adds. Its shape mirrors pytr
output and evolves with the TR app; consumers must tolerate unknown keys and
must not rely on any field outside the guaranteed floor above.

The echoed `since`/`until` are normalised to UTC and rendered with a `Z` suffix regardless of the
offset supplied in the request. Raw event fields keep pytr's original values unchanged.

| Problem code     | HTTP | When |
|------------------|------|------|
| `session_expired`| 401  | No valid TR session — re-login required |
| `invalid_request`| 400  | Missing or malformed `since`/`until` |
| `tr_upstream_error` | 502 | pytr / TR failure |

---

## Error responses

All errors follow [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457) (`application/problem+json`):

```json
{
  "type": "https://tr-bridge/errors/session-expired",
  "title": "Session expired",
  "status": 401,
  "detail": "The Trade Republic session for instance 'user1' has expired — login required.",
  "instance": "user1",
  "code": "session_expired"
}
```

| HTTP | `code`               | When |
|------|----------------------|------|
| 400  | `invalid_request`    | Malformed `since`/`until`, missing `code`, invalid instance name |
| 401  | `session_expired`    | No valid session for a timeline request |
| 401  | `code_rejected`      | Authenticator code was wrong |
| 401  | `unauthorized`       | Missing/invalid `X-API-Key` |
| 404  | `instance_not_found` | Unknown instance name |
| 408  | `twofa_timeout`      | Login initiated but no code/approval within the timeout |
| 409  | `no_login_pending`   | `login/2fa` called with no authenticator login awaiting |
| 409  | `login_in_progress`  | Concurrent login attempt for the same instance |
| 429  | `rate_limited`       | TR login rate-limit hit |
| 502  | `tr_upstream_error`  | pytr / TR websocket or HTTP failure |
| 500  | `internal_error`     | Unexpected failure |

---

## Development & releases

### Docker image

The image is published to `ghcr.io/sanmibuh/tr-bridge`. It is a multi-stage,
non-root build that runs `uvicorn tr_bridge.main:app` on port `8000`. Build it
locally with:

```bash
docker build -t tr-bridge .
```

### CI

Pushes to `main` and pull requests targeting `main` run
[`.github/workflows/ci.yml`](.github/workflows/ci.yml):
`ruff check`, `ruff format --check`, and `pytest` with 100% coverage enforced.

### Cutting a release

Releases are driven entirely by the `VERSION` file at the repo root:

1. Trigger the **Prepare release PR** workflow
   ([`.github/workflows/prepare-release.yml`](.github/workflows/prepare-release.yml))
   from the Actions tab and pick a bump type (`patch` / `minor` / `major`). It
   bumps `VERSION`, regenerates the `CHANGELOG.md` section from the commits since
   the last tag, and opens a PR.
2. Review the generated `CHANGELOG.md` notes and merge the PR.
3. Merging changes `VERSION` on `main`, which triggers
   [`.github/workflows/release.yml`](.github/workflows/release.yml): it creates
   the `vX.Y.Z` git tag and GitHub Release, then builds and pushes the multi-arch
   image tagged `vX.Y.Z` and `latest` to GHCR.

The **Prepare release PR** workflow requires a `PAT_RELEASE` repository secret so
that the opened PR triggers CI — PRs created with the default `GITHUB_TOKEN` do
not start workflow runs. Prefer a **fine-grained** personal access token scoped
to this repository only, with the minimal permissions **Contents: Read and
write** (push the release branch) and **Pull requests: Read and write** (open the
PR). Avoid a classic token with the broad `repo` scope.

---

## License

See [LICENSE](LICENSE).
