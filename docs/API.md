# HTTP API

The server is implemented in C++20. PostgreSQL is the authoritative inventory store. Browser UI and integrations use the same `/api/v1` endpoints. `/api/openapi.json` is the OpenAPI 3.0 document, `/api/docs` is an offline, same-origin interactive client. `/health` returns 200 only after database initialization succeeds.

## Authentication

1. `GET /api/v1/bootstrap` reports whether the first administrator must be created.
2. `POST /api/v1/bootstrap` accepts `username` and `password`, once only. Complete setup before making the port available to other users.
3. `POST /api/v1/auth/login` returns `token`, `csrf`, `expires_in` (43200 seconds), and an HttpOnly, SameSite=Strict session cookie.
4. Integrations send `Authorization: Bearer TOKEN`. Browser requests use the cookie and `X-CSRF-Token` on changes. Tokens are not stored in localStorage. PostgreSQL stores only hashes of session tokens. Use HTTPS for access across untrusted networks.
5. `POST /api/v1/logout` ends the user's sessions. Disabled users lose access immediately. API permissions are the same as UI permissions.

## Retry and concurrency contract

Authenticated changes require `Idempotency-Key` matching `[A-Za-z0-9_-]{16,128}`. Generate a key per logical operation; preserve it and the request body on network retry. Reusing a key with different data yields 409. PostgreSQL serializes inventory-changing transactions with a transaction-scoped advisory lock. Success is returned only after commit. Regrind receipt legs are edited/cancelled together, and all affected ledgers are validated. Corrections expire after 24 hours. Negative historical quantities are not hidden.

Send JSON (`Content-Type: application/json`) for all operations except photo upload. Multipart upload uses field `photo`, PNG/JPEG/WebP, at most 5 MiB. Photos are authenticated at `/photo/FILENAME`.

Examples below omit the real token intentionally:

```sh
curl -X POST http://SERVER:8000/api/v1/transactions/issue \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: UNIQUE-OPERATION-UUID' \
  -d '{"product_id":1,"worker_id":1,"machine_id":1,"state":"new","qty":2}'
```

A return supplies `product_id`, `worker_id`, `machine_id`, `state_from`, `qty`, and `action` (`return`, `regrind`, `writeoff`) to `/transactions/settle`. A return changes the warehouse state to used; regrind adds to the dispatch queue. Writeoff requires a reason in `note`.

For bulk dispatch, read `/regrind-queue` and POST its exact array as `snapshot` to `/transactions/regrind-send-all`. A changed queue yields 409 instead of sending unseen additions.

Products store `params_json` with diameter, l1, length, radius, thread, fixture and insert parameters. ISO inserts use `insert_kind: "iso"` and `insert_iso: {shape,clearance,tolerance,fixing,size,thickness,corner}`. Metadata/field definitions are at `/meta`. Quantities and minimums are integers; sizes and prices allow decimals. Zero stock is a valid state; negative dimensions are invalid.

`GET /products` returns cards including `stocks`, `available`, and `held`. Query `q` performs Unicode case-insensitive search; `archived=1` reads archived cards. `/holdings` is grouped by worker, machine, product and state. Worker accounts can only read their own holdings/history.

`GET /movements` returns `{items,total,limit,offset}`. Filters: `worker_id`, `machine_id`, `from`, `to`, `limit` (1–500), `offset`. Dates use Moscow time. `format=csv` exports the entire selected history. `/reports` returns balances and holdings by state, with the same worker/machine filters; `format=csv` is available. `/movements/ID/changes` exposes the correction audit.

Purchase requests accept `lines: [{product_id,qty}]`; the server snapshots stock, minimum and price. `close` never creates a receipt. `archive` removes the request from working lists and preserves lines/CSV; `restore` returns it with its prior status. Lists accept `view=open|closed|archived`.

Machine, worker, account and role management requires ADMIN. Disabling preserves historical references. A worker holding tools cannot be disabled. The last active administrator cannot be disabled or demoted. User updates can set a new password, which invalidates existing sessions.

Errors use HTTP 400/401/403/404/405/409/413/415/429/503 and JSON `{error: "human-readable message"}`. Validation failures roll back the entire database operation. HTTP API v1 is a new contract; the old Python form POST URLs are not compatibility endpoints.
