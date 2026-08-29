# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] - 2026-08-29

### What's Changed
* feat: Docker image and CI pipeline — [#44](https://github.com/sanmibuh/trade-republic-bridge/pull/44)
* Authenticator 2FA login does not fully establish the web session (timeline returns 401) — [#42](https://github.com/sanmibuh/trade-republic-bridge/pull/42)
* Timeline fetch fails with AUTHENTICATION_ERROR ("No auth token") right after login — [#40](https://github.com/sanmibuh/trade-republic-bridge/pull/40)
* Redirect root path (/) to the Swagger UI (/docs) — [#38](https://github.com/sanmibuh/trade-republic-bridge/pull/38)
* Expose OpenAPI schema and Swagger UI publicly — [#36](https://github.com/sanmibuh/trade-republic-bridge/pull/36)
* Allow relocating the config/data path for local development — [#34](https://github.com/sanmibuh/trade-republic-bridge/pull/34)
* docs(architecture): explain why the login/2FA state machine is irreducible — [#32](https://github.com/sanmibuh/trade-republic-bridge/pull/32)
* test: enforce 100% coverage threshold in pytest config and CI — [#31](https://github.com/sanmibuh/trade-republic-bridge/pull/31)
* refactor: adopt a light hexagonal architecture (web primary adapter / use case + secondary port / pytr secondary adapter) — [#30](https://github.com/sanmibuh/trade-republic-bridge/pull/30)
* refactor: extract the time-window parsing out of main.py into its own module — [#29](https://github.com/sanmibuh/trade-republic-bridge/pull/29)
* refactor(main): remove exception-handler duplication with a data-driven mapping (DRY/OCP) — [#28](https://github.com/sanmibuh/trade-republic-bridge/pull/28)
* test: replace private-state assertions with functional tests (respect encapsulation) — [#27](https://github.com/sanmibuh/trade-republic-bridge/pull/27)
* feat: GET /instances/{name}/timeline endpoint — [#18](https://github.com/sanmibuh/trade-republic-bridge/pull/18)
* feat: login endpoints (POST /login and POST /login/2fa) — [#17](https://github.com/sanmibuh/trade-republic-bridge/pull/17)
* feat: login state machine and session persistence — [#16](https://github.com/sanmibuh/trade-republic-bridge/pull/16)
* feat: GET /health and GET /instances endpoints — [#15](https://github.com/sanmibuh/trade-republic-bridge/pull/15)
* feat: authentication middleware (X-API-Key) — [#14](https://github.com/sanmibuh/trade-republic-bridge/pull/14)
* feat: project scaffold — package layout, config, error format — [#13](https://github.com/sanmibuh/trade-republic-bridge/pull/13)
* chore: set up Python project environment and CI pipeline — [#4](https://github.com/sanmibuh/trade-republic-bridge/pull/4)
* docs(agents): add initial documentation — [#2](https://github.com/sanmibuh/trade-republic-bridge/pull/2)

**Full Changelog**: https://github.com/sanmibuh/trade-republic-bridge/commits/v1.0.0
