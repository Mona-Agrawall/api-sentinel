# API Sentinel: module walkthrough

Each section covers what the module does, the key design choice behind it, and one thing
that could go wrong. Three quiz questions follow, with answers hidden; try to answer
before expanding them.

---

## 1. Configuration (`app/config.py`)

**What it does.** Reads every setting from environment variables, or a `.env` file, into
a typed Pydantic `Settings` object. Bad values fail fast at startup, before any run starts.
Examples: a negative timeout, an unknown log level, a Mongo URI with the wrong scheme.

**Key design choice.** The Groq key is a `SecretStr`. Printing or logging the settings
shows `**********`. The only way to get the real value is an explicit
`.get_secret_value()`, called in exactly one place (the `Authorization` header). It's
much harder to leak a secret by accident when leaking it takes a deliberate method call.

**What could go wrong.** `get_settings()` is cached. Change an environment variable
after the first call and the process won't see it. That's why the tests build their own
`Settings(...)` instead of calling `get_settings()`.

**Quiz**
1. Why does `TARGET_ALLOWLIST` need the `NoDecode` annotation?
2. What happens if `GROQ_API_KEY=` is set to an empty string?
3. Why is `host_allowed` a method on `Settings` instead of a free function in the runner?

<details><summary>Answers</summary>

1. pydantic-settings would otherwise try to parse a list-typed variable as JSON, and
   `localhost,127.0.0.1` isn't valid JSON. `NoDecode` hands the raw string to the
   validator, which splits it on commas.
2. The `_empty_key_is_none` validator turns it into `None`, so `llm_available` is
   False and the run falls back to rule cases. It doesn't send `Bearer ` with an empty key.
3. The API (rejecting bad requests early) and the pipeline (defence in depth) both use
   it. Keeping it next to the allow-list it reads gives one source of truth.
</details>

---

## 2. Schema parser (`app/generator/schema_parser.py`)

**What it does.** Loads an OpenAPI 3.0 or 3.1 document (JSON or YAML) and flattens it into
`Endpoint` objects. Each one carries its method, path, path/query/header params, and the
top-level fields of its JSON body. For each value it records the type, required/nullable
status, enum, min/max with exclusivity, min/max length and format.

**Key design choice.** It normalises the spec versions into one model. OpenAPI 3.0 marks
nullability with `nullable: true` and uses a boolean `exclusiveMinimum`. OpenAPI 3.1,
which FastAPI emits, uses `anyOf: [{type: string}, {type: null}]` and a *numeric*
`exclusiveMinimum`. `_flatten` collapses `$ref`, `allOf` and nullable `anyOf`/`oneOf` so
nothing downstream needs to know which version it came from.

**What could go wrong.** `$ref` cycles, such as a `Node` schema that references itself.
A naive resolver would loop forever. `_Resolver` tracks the refs it has seen and caps
the depth. Remote refs (`https://…`) are rejected outright, which also means the
parser never makes network calls.

**Quiz**
1. In OpenAPI 3.1, how is "price must be greater than 0" written, and how does it appear in `FieldSpec`?
2. Why does the parser reject paths that don't start with `/`?
3. A parameter is declared at both path-item level and operation level with the same name. Which one wins?

<details><summary>Answers</summary>

1. `exclusiveMinimum: 0`. It becomes `minimum=0, exclusive_minimum=True`.
2. A path like `http://evil.com/x` would make httpx ignore `base_url` and send traffic
   to another host. Rejecting it at parse time is the first line of defence.
3. The operation-level one. Params are keyed by `(in, name)`, and operation params are
   applied after path-level ones.
</details>

---

## 3. Rule-based cases (`app/generator/rule_cases.py`, `models.py`)

**What it does.** Builds one fully valid *baseline* request per endpoint. It then
mutates one value at a time: null, missing required field, wrong type, min-1/min/max/max+1,
exclusive bounds, empty string, minLength-1, maxLength/maxLength+1, a 10,000-character string,
an invalid enum value, 2**63, a null/empty/array body, and "only required fields".
Each case carries the outcome it expects (`2xx`, `4xx`, or `not_5xx`).

**Key design choice.** Generation is deterministic. The same spec always produces the
same cases with the same `payload_hash`, the SHA-256 of the canonical method, path,
params and body. That gives two things. Merging with LLM cases can dedupe exact
duplicates. And the detector can tell that "case X passed last run and fails now",
which is impossible if the cases change every run.

**What could go wrong.** Wrong expectations cause false alarms. Take a valid
`GET /items/{id}`: it may return 404 because item 1 doesn't exist. That's why endpoints
with path params expect `not_5xx` rather than `2xx`. Empty-string path params are skipped
for a similar reason: `/items/` is a different route that often redirects.

**Quiz**
1. Why mutate only one value per case instead of combining several bad values?
2. For a nullable optional field, what do the "null" and "only required fields" cases expect, and which demo bug did they catch?
3. Why does the merge keep the *first* case for a hash, and why are rule cases passed first?

<details><summary>Answers</summary>

1. When a case fails you know exactly which value caused it. With several bad values,
   a 422 from the first one hides how the API handles the others.
2. Both expect a successful response (`2xx`). Both returned 500 on the faulty target,
   because `description.strip()` ran on `None`.
3. Rule cases have stable names and categories, so keeping them makes run-over-run
   comparisons and the UI consistent. An LLM duplicate adds nothing new.
</details>

---

## 4. Prompts and LLM client (`app/generator/prompts.py`, `llm_client.py`, `generate.py`)

**What it does.** For each endpoint it sends the parsed endpoint plus a valid baseline
request to Groq (LLaMA-3) in JSON mode. It asks for cases a rule engine would miss,
like unicode, injection-style strings and precision issues. Every returned case is
validated with Pydantic. `generate_all` merges LLM cases with rule cases and records
whether a fallback happened, and why.

**Key design choice.** LLM failure is a value, not an exception. Timeouts, HTTP errors,
malformed JSON and invalid cases all end up in `GenerationResult.error` or
`invalid_dropped`. The run continues with rule cases, and the run document records
`llm_fallback: true` with the reason. Prompts are versioned (`v1`), and the version is
stored on the run, so changes in output can be traced to prompt changes.

**What could go wrong.** The model can hallucinate parameters or point requests
somewhere else. Cases whose `path_params` don't exactly match the spec's path params,
or whose query keys aren't declared, are dropped. Combined with the origin guard, LLM
output can only ever fill in values. It can never choose a URL.

**Quiz**
1. How many HTTP calls does the client make for one endpoint when Groq keeps returning 503?
2. Why use `response_format: json_object` *and* Pydantic validation; isn't one enough?
3. Where could the API key leak, and what prevents each path?

<details><summary>Answers</summary>

1. Two: the original try plus one retry (`MAX_ATTEMPTS = 2`). After that it gives up
   and the endpoint uses rule cases only.
2. JSON mode guarantees *syntactically* valid JSON, not the right *shape*. Pydantic
   enforces the shape: required fields and an allowed `expected` value.
3. In logs, reprs and error messages. Logs only include status codes and exception
   type names, `SecretStr` masks the repr, and a test asserts the key isn't in captured
   logs even when the server echoes it back in a 401 body.
</details>

---

## 5. Functional runner (`app/runner/functional.py`)

**What it does.** Sends every case with an `httpx.AsyncClient`, with bounded concurrency
(a semaphore). It records status, latency, a 300-character response excerpt and
pass/fail. A 5xx or a transport error always fails.

**Key design choice.** An `OriginGuard` request hook compares each outgoing URL's
`(scheme, host, port)` with `base_url` and refuses anything else, *before* the request is
sent. Path values are URL-encoded with `safe=""`, so `../../etc/passwd` stays inside one
path segment.

**What could go wrong.** Storing request data in MongoDB. The integration test caught
this: the `2**63` boundary case crashed the whole run with
`OverflowError: MongoDB can only handle up to 8-byte ints`. Now `_bson_safe` turns
out-of-range ints into strings, and bodies are stored as a JSON string. Bodies can
contain keys like `$where` that Mongo rejects as field names.

**Quiz**
1. Why is `follow_redirects=False`?
2. What does a case with `expected="4xx"` that gets 200 back tell you about the API?
3. Why is a failed request (connection refused) recorded as a result instead of raising?

<details><summary>Answers</summary>

1. A redirect could point to another host. The guard would block it, but not following
   redirects also keeps the result honest: you see the 3xx the API actually sent.
2. Input validation is missing: the API accepted data its own schema says is invalid.
3. One unreachable endpoint shouldn't discard the results of every other case, and the
   failure itself is useful data.
</details>

---

## 6. Load runner (`app/runner/load.py`, `locust_worker.py`)

**What it does.** Turns each endpoint's valid baseline request into a Locust task. It
runs N users for T seconds headlessly with Locust's `Environment` and `LocalRunner`,
then returns p50/p95/p99, RPS and error rate per endpoint. Only 5xx and connection errors
count as load errors.

**Key design choice.** Locust runs in a **subprocess**. Importing Locust applies gevent's
monkey-patching to sockets and threads, which breaks the asyncio event loop that FastAPI
and the functional runner rely on. A separate interpreter, talking through JSON files,
isolates it completely. The parent re-checks that every target is a relative path on the
allowed origin before starting it.

**What could go wrong.** Load-testing unsafe methods. Hammering `POST /items` or
`DELETE /items/1` for 60 s changes the target's data. So only GET/HEAD/OPTIONS are
load-tested unless `LOAD_INCLUDE_UNSAFE_METHODS=true`.

**Quiz**
1. Why name each Locust request after the endpoint key (`GET /items/{item_id}`) instead of the concrete URL?
2. Why doesn't a 404 count as a load-test failure?
3. What would you see if you imported `locust` inside `app/main.py`?

<details><summary>Answers</summary>

1. Stats are aggregated by name. Using the template means stats from different runs
   line up even when concrete ids differ, and that's what the detector compares.
2. The load test measures speed and stability. A 404 for a missing resource is a
   correct, fast answer; correctness belongs to the functional runner.
3. gevent would monkey-patch the process: hangs, warnings about patching after
   `ssl`/`asyncio` imports, and a broken event loop under uvicorn.
</details>

---

## 7. Storage (`app/db/models.py`, `mongo_client.py`, `repo.py`)

**What it does.** Two collections. `runs` holds one document per run: status, generation
info, summary counts, per-endpoint load stats and flags. `results` holds one document per
case. Indexes cover `results(endpoint, timestamp)`, `results(run_id)`, `runs(created_at)`,
`runs(base_url, status, created_at)` for the previous-run lookup, and
`runs(base_url, status, users, duration_s, created_at)` for the latency-baseline query.

**Key design choice.** All database access goes through `RunRepository`, and
`create_client` returns either a real `MongoClient` or a mongomock client for
`mongomock://` URIs. Tests and quick experiments need no database, and the rest of the
code doesn't know which one it's using.

**What could go wrong.** Load stats are a *list* of `{endpoint, …}`, not a dict keyed by
endpoint. Endpoint keys are user-controlled strings: a path like `/v1.2/items` contains a
`.`, and keys can start with `$`. Both are awkward or illegal as MongoDB field names.

**Quiz**
1. Which index serves "the last K completed runs against this base_url with this load profile, newest first"?
2. Why store a run's results in a separate collection instead of embedding them in the run?
3. What's the catch with `mongomock://` when running the API and the dashboard as separate processes?

<details><summary>Answers</summary>

1. `runs(base_url, status, users, duration_s, created_at desc)`: equality on the first four
   fields, sorted on the last.
2. MongoDB documents are capped at 16 MB, and a run can have thousands of cases with
   10 kB bodies. Separate documents also allow per-endpoint queries across runs.
3. Each process gets its own in-memory database, so the dashboard sees nothing. Use
   real MongoDB for that.
</details>

---

## 8. Regression detector (`app/regression/detector.py`)

**What it does.** Pure functions, with no I/O. Latency: per endpoint, compare current p95
with the median p95 of the last K runs, and flag if it's slower by more than X% **and**
more than Y ms. Functional: compare with the previous run by payload hash and flag `new_5xx`
and `newly_failing`.

**Key design choice.** Using *both* thresholds and a *median* baseline. The median
ignores a single noisy run; the percentage threshold alone over-flags fast endpoints
(2 → 4 ms is +100%), and the ms threshold alone over-flags slow endpoints (3000 → 3060 ms
is +60 ms). In the demo, 3 ms → 220 ms is +217 ms and +7233%, so it's flagged.

**What could go wrong.** Comparing runs that aren't comparable. If one run used 10
users and the next 200, p95 rises for reasons that have nothing to do with code changes.
That is why latency history is scoped by `base_url` *and* load profile (`users`,
`duration_s`). Hardware is still your responsibility: a baseline recorded on a laptop and a
run on a CI runner will differ.

**Quiz**
1. Baseline p95s are 10, 30 and 1000 ms. What's the baseline, and would a mean have been better?
2. With X=20 and Y=50, is 100 → 150 ms flagged? 100 → 151 ms?
3. Why is a 5xx that was *already* failing last run not flagged again?

<details><summary>Answers</summary>

1. 30 ms. The mean (≈347 ms) would be dominated by the outlier and hide real regressions.
2. 150: no, because +50 ms is not *greater than* 50. 151: yes (+51%, +51 ms).
3. Flags signal *change*. Known failures still show as FAIL in the results table, but
   repeating them every run creates alert fatigue.
</details>

---

## 9. Pipeline and API (`app/pipeline.py`, `app/main.py`, `app/cli.py`)

**What it does.** `POST /runs` first checks the API key, when `API_KEY` is set, and the
per-caller token-bucket rate limit. These are dependencies, so they reject a request with `401`
or `429` before the body is even validated. It then validates the body. That covers URLs, the
allow-list, and the user and duration caps. It stores a `pending` run and returns `202`, then
`execute_run` runs as a background task. The pipeline saves the run after each stage, so
`GET /runs/{id}` shows progress. The CLI runs the same pipeline synchronously.

**Key design choice.** `execute_run` never raises. Any exception marks the run `failed`
with an error message, and `finished_at` is always set. Blocking work (LLM HTTP calls,
the Locust subprocess) goes through `asyncio.to_thread`, so the API keeps responding
during a run.

**What could go wrong.** FastAPI background tasks run in the API process, so a restart
loses any run in flight. On startup the API therefore marks every `pending` or `running`
run as `failed` ("interrupted: process restarted before run completed"). That recovery
assumes a single API instance: with several replicas sharing one database, a restarting
replica would also fail runs that another replica is still executing. A multi-instance
deployment would use a job queue (RQ, Celery, Arq) with worker heartbeats instead.

**Quiz**
1. Why return `202 Accepted` rather than `201 Created` or `200`?
2. Why is the allow-list checked both in `POST /runs` *and* in `execute_run`?
3. Why does the CLI exit with code 1 when there are flags?

<details><summary>Answers</summary>

1. The work hasn't happened yet. 202 means "accepted for processing", and the client
   polls `GET /runs/{id}`.
2. Defence in depth. The CLI and future callers reach `execute_run` without going
   through the API.
3. So it can gate a CI pipeline: a regression fails the build.
</details>

---

## 10. Dashboard (`app/dashboard/streamlit_app.py`)

**What it does.** Reads MongoDB directly and shows:
- the run list for a selected target
- a p95 latency trend line per endpoint, where triangles mark flagged runs
- the flags and summary metrics for a selected run
- the LLM fallback notice
- load stats
- a filterable pass/fail table

**Key design choice.** Colour follows the endpoint, not its position. The colour domain
is the *sorted* endpoint list over a fixed palette, so deselecting an endpoint doesn't
repaint the others. There's one y-axis (p95 ms), and the trend is also available as a
table.

**What could go wrong.** Very many runs or results make the dashboard slow, because it
loads up to 500 runs per refresh. That's fine for a portfolio project; a larger
deployment would add pagination or pre-aggregated trend documents.

**Quiz**
1. Why does the script modify `sys.path` before importing `app`?
2. Why does the dashboard read MongoDB directly instead of calling the FastAPI endpoints?
3. Why are flagged points marked with a different *shape* and not just a different colour?

<details><summary>Answers</summary>

1. `streamlit run app/dashboard/streamlit_app.py` puts the script's own folder on the
   path, not the project root, so `import app` would fail.
2. It's simpler (no second network hop) and the repository is already the read model.
   The trade-off is that both processes need database access.
3. Colour already encodes the endpoint, and colour alone isn't accessible (colour-blind
   readers, print). Shape is a second, independent signal.
</details>

---

## 11. Demo target, CI and Docker (`target_api/`, `scripts/run_demo.py`, `.github/workflows/ci.yml`, `docker-compose.yml`)

**What it does.** `target_api` is a small store API (health, list, get, create, search).
`TARGET_API_FAULTS=1` adds `await asyncio.sleep(0.2)` to `GET /items/{item_id}` and a
`.strip()` on the nullable `description` in `POST /items`. `run_demo.py` performs
baseline runs, flips the switch, runs again, and writes `docs/demo_results.json`. The
README Results come from that file. CI runs ruff, mypy and pytest, and builds the image.
It ignores pushes that only touch `activity.log`.

**Key design choice.** The fault switch is read *per request*, not at import. Tests can
flip it with `monkeypatch.setenv` while one server keeps running, which is how the
integration test measures the delay with real Locust.

**What could go wrong.** On Windows, `localhost` first resolves to IPv6 `::1`, and uvicorn
only listens on IPv4. The first demo attempt saw ~2000 ms p99 outliers from that
connection fallback, a measurement artifact rather than the API being slow. The demo now
targets `127.0.0.1`.

**Quiz**
1. Why is the slow endpoint's sleep `await asyncio.sleep` rather than `time.sleep`?
2. Why was the null bug caught by the *rule* generator without any LLM?
3. What would happen to the latency flag if the baseline runs had been executed with faults on?

<details><summary>Answers</summary>

1. `time.sleep` in an async handler blocks the event loop and slows *every* endpoint.
   That would make the demo regression spill into the others.
2. The rule generator always sends `null` for nullable fields and an "only required
   fields" body. Those are exactly the inputs the bug mishandles.
3. Nothing would be flagged. The baseline median would already be ~220 ms, so the
   detector only catches *changes* against history.
</details>
