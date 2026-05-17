# HTTP Metadata Inventory Service

A FastAPI service that collects and serves HTTP metadata (response headers,
cookies, and page source) for arbitrary URLs, backed by MongoDB. On a cache
miss, the GET endpoint replies immediately with `202 Accepted` and triggers an
**in-process background task** to fetch and persist the metadata — no external
worker, broker, or service-to-self HTTP call.

## Stack

| Layer        | Technology                  |
| ------------ | --------------------------- |
| Language     | Python 3.11+                |
| Framework    | FastAPI + Uvicorn           |
| Database     | MongoDB 7 (via Motor async) |
| HTTP client  | httpx                       |
| Config       | pydantic-settings           |
| Tests        | pytest + pytest-asyncio     |
| Orchestration| Docker Compose              |

## Project layout

```
app/
  config.py        # pydantic-settings, env-driven configuration
  database.py      # Motor connection with retry-on-startup
  dependencies.py  # FastAPI DI providers
  models.py        # Pydantic models (request/response/persistence)
  repository.py    # Data-access layer over the Mongo collection
  services.py      # Fetcher + orchestration logic + URL normalisation
  routers/
    metadata.py    # POST and GET endpoints
  main.py          # App factory + lifespan
tests/
  conftest.py      # In-memory Mongo + httpx MockTransport fixtures
  test_metadata.py # Unit + integration tests
docker-compose.yml
Dockerfile
requirements.txt
```

A clear three-layer split is maintained: **transport** (routers) → **logic**
(services) → **data** (repository).

## Run with Docker Compose

```bash
docker compose up --build
```

This brings up:

- `mongo` — MongoDB 7 with a persistent named volume and a healthcheck
- `api` — the FastAPI service on `http://localhost:8000`, started only once
  Mongo is healthy

OpenAPI docs: <http://localhost:8000/docs>
ReDoc: <http://localhost:8000/redoc>

## API

### `POST /metadata`

Fetch a URL synchronously, store the result, return the stored record.

```bash
curl -X POST http://localhost:8000/metadata \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
```

- `201 Created` on success — returns the full `MetadataRecord`.
- `422` on malformed URL (Pydantic validation).
- `400` on URLs missing a scheme/host.
- `502` if the upstream URL cannot be fetched.

### `GET /metadata?url=…`

Look up a URL in the inventory.

```bash
curl "http://localhost:8000/metadata?url=https://example.com"
```

- `200 OK` — record exists and is ready; the body contains headers, cookies,
  page source, status code, and timestamps.
- `202 Accepted` — record was missing (or previously failed/pending); a
  background task has been scheduled and the URL will be available on a
  subsequent request.
- `400 Bad Request` — the URL is unparseable.

### `GET /health`

Liveness probe used by the Docker healthcheck.

## Background-worker design

Per the spec, collection on a cache miss must:

1. **Be triggered internally by the GET endpoint** — handled in
   `app/routers/metadata.py` via FastAPI's `BackgroundTasks`.
2. **Run independently of the request/response cycle** — `BackgroundTasks`
   runs *after* the response is sent on the same event loop.
3. **Avoid service-to-self HTTP calls** — the background task is a plain
   `await service.collect_and_store(url)` call; no loopback HTTP, no broker.
4. **Persist for future retrieval** — the same repository upserts the result;
   the next GET reads it directly from Mongo.

The route also calls `repo.mark_pending(url)` before scheduling so a concurrent
GET for the same URL does not return a misleading "not in inventory" state.

## URL normalisation

A canonical key form is used so trivial variations don't create duplicate
records:

- scheme/host lowercased
- default ports (`:80` / `:443`) stripped
- fragment removed
- empty path normalised to `/`
- query string preserved (different queries are different pages)

## Configuration

All settings come from environment variables (see `.env.example`):

| Variable                        | Default                                            |
| ------------------------------- | -------------------------------------------------- |
| `MONGO_URI`                     | `mongodb://localhost:27017`                        |
| `MONGO_DB`                      | `metadata_inventory`                               |
| `MONGO_COLLECTION`              | `pages`                                            |
| `MONGO_CONNECT_RETRIES`         | `10`                                               |
| `MONGO_CONNECT_BACKOFF_SECONDS` | `1.5`                                              |
| `FETCH_TIMEOUT_SECONDS`         | `15`                                               |
| `FETCH_MAX_BYTES`               | `5000000`                                          |
| `FETCH_MAX_REDIRECTS`           | `5`                                                |
| `FETCH_USER_AGENT`              | `MetadataInventoryBot/1.0 (+...)`                  |
| `LOG_LEVEL`                     | `INFO`                                             |

## Resilience

- `MongoConnection.connect` retries with backoff so the API survives the
  Mongo container booting after it.
- The `api` service in compose `depends_on` Mongo's healthcheck — the API
  only starts once Mongo answers `ping`.
- Fetcher errors (timeouts, connection failures, bad TLS) are caught,
  recorded as `status: failed` on the record, and surfaced as `502` on POST.
- Response bodies are capped at `FETCH_MAX_BYTES` to bound memory.

## Schema (MongoDB)

Collection: `pages`. Unique index on `url`.

```jsonc
{
  "_id": "ObjectId(...)",
  "url": "https://example.com/",
  "status": "ready",                          // "pending" | "ready" | "failed"
  "status_code": 200,
  "headers":            { "content-type": "..." },   // single-value view
  "set_cookie_headers": [ "a=1; Path=/", "b=2; HttpOnly" ],  // multi-value, lossless
  "cookies":            { "a": "1", "b": "2" },      // parsed name -> value
  "page_source": "<html>…</html>",
  "error": null,
  "created_at": ISODate(...),
  "updated_at": ISODate(...)
}
```

`headers` is the single-value-per-name view (the common case). `Set-Cookie`
is the one response header that legally repeats with different values, so it
is captured raw in `set_cookie_headers` and parsed into the `cookies` map.

## Tests

Tests use [`mongomock-motor`](https://pypi.org/project/mongomock-motor/) for an
in-memory Mongo replacement and `httpx.MockTransport` to stub upstream
responses, so the suite is hermetic and fast.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

(Or `docker compose run --rm api pytest -q` once the image is built.)

## Extending

- Swap `BackgroundTasks` for a real queue (Celery, RQ, Arq) by replacing one
  call site in `app/routers/metadata.py`; the service/repo layers are
  untouched.
- Add new fields to `MetadataRecord` + `MetadataRepository.upsert_ready`; the
  router code does not need to change.
- Add additional indexes (e.g. on `created_at` for TTL/cleanup) in
  `MongoConnection.connect`.
