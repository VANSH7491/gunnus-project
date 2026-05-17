# HTTP Metadata Inventory Service

A FastAPI service that collects HTTP response **headers**, **cookies**, and
**page source** for arbitrary URLs and serves them from a MongoDB-backed
inventory. On a cache miss, GET replies immediately with `202 Accepted`
and triggers an **in-process background task** to fetch and persist the
metadata — no broker, no service-to-self HTTP call.

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
  config.py        # pydantic-settings — env-driven configuration
  database.py      # Motor connection with retry-on-startup
  dependencies.py  # FastAPI DI providers
  models.py        # Pydantic models (request, response, persistence)
  repository.py    # Data-access layer over the Mongo collection
  services.py      # Fetcher + service + URL normalisation + SSRF guard
  routers/
    metadata.py    # POST and GET endpoints
  main.py          # App factory + lifespan
tests/
  conftest.py      # In-memory Mongo + MockTransport fixtures
  test_metadata.py # 28 hermetic tests
docker-compose.yml
Dockerfile
requirements.txt
pytest.ini
.env.example
```

Three-layer separation: **transport** (`routers`) → **logic** (`services`)
→ **data** (`repository`).

---

## How to run and test

### 1. Start the stack

```bash
docker compose up --build
```

First run builds the API image (~1–2 min) and pulls `mongo:7`. Wait for:

```
metadata-api  | INFO ... Connected to MongoDB at mongodb://mongo:27017
metadata-api  | INFO:     Uvicorn running on http://0.0.0.0:8000
```

Leave this terminal open — it streams logs from both containers.

Or run detached and tail logs separately:
```bash
docker compose up --build -d
docker compose logs -f api
```

### 2. Verify it's alive

In a second terminal:
```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Swagger UI: <http://localhost:8000/docs>

### 3. POST a real URL — synchronous fetch + store

```bash
curl -X POST http://localhost:8000/metadata \
  -H "Content-Type: application/json" \
  -d '{"url":"https://httpbin.org/get"}'
```

Expected `201 Created` with the full `MetadataRecord` (headers, cookies,
page_source, status_code, final_url, timestamps).

POST a cookie-setting URL to verify multi-cookie capture:
```bash
curl -X POST http://localhost:8000/metadata \
  -H "Content-Type: application/json" \
  -d '{"url":"https://httpbin.org/cookies/set?freeform=blue"}'
```

You'll see `cookies: {"freeform": "blue"}` and the `final_url` ending in
`/cookies` (httpbin redirects).

### 4. GET — cache hit

The URL you just POSTed is already in the inventory:
```bash
curl "http://localhost:8000/metadata?url=https://httpbin.org/get"
# 200 OK + the stored record
```

### 5. GET — cache miss (the core spec requirement)

A URL you have never asked for:
```bash
curl -i "http://localhost:8000/metadata?url=https://httpbin.org/headers"
# HTTP/1.1 202 Accepted
# {"url":"...","status":"pending","detail":"Metadata collection scheduled..."}
```

The API answered immediately. The background task is now fetching and
storing. Wait ~1–2 seconds and ask again:
```bash
curl -i "http://localhost:8000/metadata?url=https://httpbin.org/headers"
# HTTP/1.1 200 OK + the populated record
```

That's the spec's required behaviour: 202 → background task → 200 on the
next call, with no broker and no service-to-self HTTP.

### 6. Validation behaviour

```bash
# Malformed URL → 422
curl -i -X POST http://localhost:8000/metadata \
  -H "Content-Type: application/json" -d '{"url":"not-a-url"}'

# SSRF guard blocks loopback by default → 400
curl -i -X POST http://localhost:8000/metadata \
  -H "Content-Type: application/json" -d '{"url":"http://127.0.0.1/secret"}'
```

### 7. Run the test suite inside the container

The test suite is hermetic — `mongomock-motor` replaces Mongo,
`httpx.MockTransport` replaces the network.

```bash
docker compose run --rm api pytest -v
```

Expected: **28 passed**.

### 8. Inspect Mongo directly

```bash
docker compose exec mongo mongosh metadata_inventory
```

Then in `mongosh`:
```javascript
db.pages.find().pretty()
db.pages.getIndexes()        // shows the unique index on `url`
exit
```

### 9. Tear down

`Ctrl+C` in the `docker compose up` terminal, then:
```bash
docker compose down            # remove containers, keep Mongo volume
docker compose down -v         # also drop the Mongo volume (fresh state)
```

---

## API reference

### `POST /metadata`

| Status | Meaning |
| ------ | ------- |
| `201`  | Fetched and persisted; full record in body. |
| `400`  | Malformed URL or host blocked by the SSRF guard. |
| `422`  | URL failed Pydantic `HttpUrl` validation. |
| `502`  | Upstream URL could not be fetched (timeout, DNS, TLS). |

### `GET /metadata?url=…`

| Status | Meaning |
| ------ | ------- |
| `200`  | Record exists and is `ready`; full body. |
| `202`  | Cache miss or stale; background collection has been scheduled. |
| `400`  | URL is malformed or blocked. |

### `GET /health`

Liveness probe used by the Docker healthcheck.

---

## Background-worker design (spec compliance)

| Spec requirement | Implementation |
| --- | --- |
| Triggered internally by GET on cache miss | `BackgroundTasks.add_task` in [app/routers/metadata.py](app/routers/metadata.py) |
| Runs independently of the request/response cycle | `BackgroundTasks` executes after the response is flushed, same event loop |
| Avoid loops or service-to-self HTTP | Background task is a plain `await service.collect_and_store(url)` — no broker, no loopback HTTP |
| Result persists for next GET | Same repository upsert; the next call reads it from Mongo |

**Concurrent-fetch dedup.** A GET that finds an existing `pending` record
with a fresh `updated_at` returns 202 without re-firing the collector
(prevents duplicate upstream fetches). Pendings older than
`PENDING_GRACE_SECONDS` are treated as stuck and retried.

---

## SSRF defence

The service rejects IP-literal URLs targeting loopback / private (RFC1918)
/ link-local / reserved / multicast ranges plus the `localhost` and
`0.0.0.0` aliases. This blocks classic SSRF probes (e.g.
`http://169.254.169.254/`, the AWS metadata service).

It does **not** perform DNS resolution — close that gap with an egress
firewall in production. Set `ALLOW_PRIVATE_HOSTS=true` to disable the
guard for local development.

---

## Configuration

All settings come from environment variables — see [.env.example](.env.example).

| Variable                        | Default                                              |
| ------------------------------- | ---------------------------------------------------- |
| `MONGO_URI`                     | `mongodb://localhost:27017`                          |
| `MONGO_DB`                      | `metadata_inventory`                                 |
| `MONGO_COLLECTION`              | `pages`                                              |
| `MONGO_CONNECT_RETRIES`         | `10`                                                 |
| `MONGO_CONNECT_BACKOFF_SECONDS` | `1.5`                                                |
| `FETCH_TIMEOUT_SECONDS`         | `15`                                                 |
| `FETCH_MAX_BYTES`               | `5000000`                                            |
| `FETCH_MAX_REDIRECTS`           | `5`                                                  |
| `FETCH_USER_AGENT`              | `Mozilla/5.0 (compatible; metadata-inventory/1.0)`   |
| `ALLOW_PRIVATE_HOSTS`           | `false`                                              |
| `PENDING_GRACE_SECONDS`         | `30`                                                 |
| `LOG_LEVEL`                     | `INFO`                                               |

---

## Schema (MongoDB)

Collection: `pages`. Unique index on `url`.

```jsonc
{
  "_id": "ObjectId(...)",
  "url":        "https://httpbin.org/cookies/set?freeform=blue",
  "final_url":  "https://httpbin.org/cookies",
  "status":     "ready",                                          // pending | ready | failed
  "status_code": 200,
  "headers":    { "content-type": "text/html; charset=utf-8" },
  "set_cookie_headers": [ "freeform=blue; Path=/" ],
  "cookies":    { "freeform": "blue" },
  "page_source": "<html>…</html>",
  "error":       null,
  "created_at":  ISODate(...),
  "updated_at":  ISODate(...)
}
```

`headers` is the single-value view. `Set-Cookie` is the one header that
legitimately repeats — captured raw in `set_cookie_headers` and parsed
into `cookies`.

---

## Resilience

- `MongoConnection.connect` retries with backoff so the API survives Mongo
  booting after it.
- `api` service `depends_on: service_healthy` in compose — API starts
  only once Mongo answers `ping`.
- Fetcher errors (timeouts, DNS, TLS, redirect loops) are caught,
  recorded as `status: failed`, and surfaced as `502` on POST. GET
  treats `failed` like a miss and retries.
- Response bodies are capped at `FETCH_MAX_BYTES` to stay within Mongo's
  16 MB document limit.
