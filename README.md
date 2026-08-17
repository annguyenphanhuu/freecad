# Tolery FreeCAD — Model Generator API

HTTP service that runs FreeCAD scripts headlessly. A client uploads a FreeCAD
Python script, the API enqueues it on Redis (RQ), and a pool of workers executes
it inside a real FreeCAD runtime — producing STEP / OBJ geometry plus optional
technical drawings. Progress is streamed over MQTT and mirrored in Redis, so a
client can either poll REST or subscribe to a topic.

---

## Architecture

```
                 ┌──────────────┐
   HTTP  ───────▶│   Flask API  │ app.py           (Swagger at /swagger/)
                 └──────┬───────┘
                        │ enqueue (priority queues)
                        ▼
                 ┌──────────────┐
                 │    Redis     │  freecad_jobs_p{0..100} + freecad_jobs
                 └──────┬───────┘
                        │ RQ
                        ▼
                 ┌──────────────┐
                 │  RQ workers  │ worker.py  ──▶ FreeCAD 1.0.0 (in-process)
                 └──────┬───────┘                 FreeCadUtil/, sheetmetal/
                        │ progress / status
                        ▼
                 ┌──────────────┐
                 │ MQTT broker  │  freecad/progress/{user_id}
                 └──────────────┘  freecad/status/{user_id}
```

Everything runs in a single container image (`MODE=all` starts the API and the
workers side by side). Redis and MQTT are separate containers.

Jobs are keyed by **`user_id`**, not by a generated job id. Input, output and
status all live under `storage/{user_id}/`, so **do not submit two concurrent
jobs with the same `user_id`** — the second overwrites the first.

---

## Quick start (Docker Compose)

```bash
cp .env.example .env      # optional; every value has a working default
docker compose up -d --build
```

Then open <http://localhost:8020/swagger/>.

| Service | Container | Host port |
|---|---|---|
| API + workers | `tolery_freecad_api` | `8020` |
| Redis | `tolery_redis` | — (internal) |
| MQTT (Mosquitto) | `tolery_mqtt` | `1883`, `9001` (websocket) |

Useful commands:

```bash
docker compose logs -f freecad
docker compose down
```

### Rebuilding after a code change

The image bakes the source in via `COPY . /app`; only `storage/` and `outputs/`
are bind-mounted. Any change to `app.py`, `worker.py`, `src/`, `FreeCadUtil/` or
`sheetmetal/` therefore requires a rebuild:

```bash
docker compose up -d --build freecad
```

The rebuild reuses the cached conda/pip layers, so it is fast unless
`requirements.txt` changed. Changes to `mosquitto.conf` or to environment
variables need only a restart (`docker compose up -d`), not a rebuild.

### `docker-compose.dev.yml`

The company deployment. It pulls a prebuilt image from the private registry
(`registry.dfm-europe.com/...`) instead of building locally, runs 3 workers, and
attaches to an external `tolery` network. Used by the GitLab CI deploy job — see
[CI/CD](#cicd).

---

## Local development (without Docker)

FreeCAD 1.0.0 must be importable by the interpreter running `worker.py`; the
simplest route is a conda environment matching the Dockerfile.

```bash
mamba install -c conda-forge freecad=1.0.0 wkhtmltopdf networkx
pip install -r requirements.txt

redis-server &                                        # terminal 1
python app.py                                         # terminal 2
rq worker --url redis://localhost:6379/0 freecad_jobs # terminal 3
```

Sanity check with the bundled client:

```bash
python client_user_upload.py oblong.py user123 --auto-download
```

---

## API

Interactive docs: `GET /swagger/`.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `POST` | `/freecad/generate` | Upload a script and enqueue a job |
| `GET` | `/freecad/status/{user_id}` | Job status + progress percentage |
| `GET` | `/freecad/result/{user_id}` | Result payload with generated files |
| `GET` | `/freecad/download/{user_id}/{filename}` | Download one generated file |
| `GET` | `/freecad/python/{user_id}` | Read back the submitted script |
| `GET` | `/freecad/download-script/{user_id}` | Download the submitted script |
| `GET` | `/freecad/template/oblong` | Example template parameters |
| `GET` | `/freecad/queue` | Queue depth per priority |
| `GET` | `/freecad/workers/status` | Status of every registered worker |
| `GET` | `/freecad/monitor` | Runtime monitor dashboard (HTML) |
| `GET` | `/freecad/monitor/overview` | Workers, queue, resources, history |
| `GET` | `/freecad/monitor/resources` | Recent RAM/CPU samples |
| `GET` | `/freecad/monitor/jobs` | Running, queued and historical jobs |
| `GET` | `/freecad/monitor/export` | Export the monitor snapshot as JSON |

`/` and `/monitor` both redirect to the dashboard.

### `POST /freecad/generate`

Multipart form:

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | yes | FreeCAD Python script (`.py`) |
| `user_id` | string | yes | Job identifier / storage namespace |
| `metadata_file` | file | no | Threaded-hole metadata; stored under `input/`, currently unused by the worker |
| `priority` | int | no | `0`–`100`, higher runs first (default `0`) |
| `auto_download` | bool | no | Block until finished and return the files |

```bash
curl -X POST http://localhost:8020/freecad/generate \
  -F "file=@script.py" \
  -F "user_id=user123" \
  -F "priority=90"

curl -s http://localhost:8020/freecad/status/user123
curl -s http://localhost:8020/freecad/result/user123
curl -O  http://localhost:8020/freecad/download/user123/<filename>
```

Priority only reorders *queued* jobs; a running job is never preempted. Each
priority level is a separate RQ queue (`freecad_jobs_p{N}`) and workers drain
them highest-first, with the legacy `freecad_jobs` queue last.

### MQTT

Workers publish JSON progress to:

- `freecad/progress/{user_id}` — incremental progress updates
- `freecad/status/{user_id}` — terminal status transitions

`listen_mqtt.py` is a minimal subscriber for debugging.

### Job outcome contract

A job ends in exactly one state, and **this server decides which, and why** — it
is the only side that saw the failure happen. It names the failure with a code;
the client (`tolery-api-ai`) turns that code into a message for the end user and
never guesses from the wording. The contract lives in
[`job_contract.py`](job_contract.py); the user-facing message for each code
lives in the client's `src/core/error_codes.py`.

| `status` | `progress` | `code` | Meaning |
|---|---|---|---|
| `complete` | 100 | `null` | Every expected file was produced |
| `partial_success` | 90 | `104.6` | Model produced, an optional export (PDF) is missing |
| `failed` | 0 | see below | Nothing usable was produced |

Codes emitted here:

| Code | Cause |
|---|---|
| `101.1` | `freecadcmd` exceeded `EXEC_TIMEOUT_SECONDS` and was stopped |
| `104.1` | The generated script raised while running |
| `104.2` | The generated script hit an encoding error |
| `104.3` | Execution finished without producing the required CAD output |
| `104.5` | The server itself failed (bad input, crash, worker died) |
| `104.6` | An optional export is missing |

The same envelope — `status`, `progress`, `code`, `message`, `error`, `details`
— is published on MQTT, written to `{output}/_job_outcome.json`, and served by
`GET /freecad/status/{user_id}` and `GET /freecad/result/{user_id}`. The file on
disk is what keeps those two endpoints honest when the broker is down, a
retained message is missed, or the API process restarts.

`error` and `details` are real JSON objects, never JSON encoded into a string:
anything the reader has to parse back out of a string is lost the first time
someone wraps it.

---

## Configuration

All values are read from the environment (see `config.py`), with `.env` loaded
automatically via `python-dotenv`. Copy `.env.example` to `.env` to override.

| Variable | Default | Description |
|---|---|---|
| `MODE` | `all` | `api`, `worker`, or `all` (set on the container, not in `.env`) |
| `API_HOST` | `0.0.0.0` | Bind address |
| `API_PORT` | `8080` (compose sets `8020`) | API port |
| `API_BASE_URL` | `http://localhost:8020` | Base URL used to build download links |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `QUEUE_NAME` | `freecad_jobs` | Base queue name |
| `MQTT_BROKER` | `mqtt://localhost:1883` | Broker URL |
| `STORAGE_PATH` | `/app/storage` | Where job input/output is written |
| `NUM_WORKERS` | `3` (compose sets `1`) | FreeCAD worker processes per container |
| `JOB_TIMEOUT` | `3600` | Per-job timeout, seconds |
| `RESULT_TTL` | `43200` | How long successful results are kept |
| `FAILURE_TTL` | `43200` | How long failed results are kept |
| `MIN_JOB_PRIORITY` | `0` | Lowest priority queue |
| `MAX_JOB_PRIORITY` | `100` | Highest priority queue |
| `DEFAULT_JOB_PRIORITY` | `0` | Priority when the field is omitted |

FreeCAD is memory-hungry and single-threaded per document; raise `NUM_WORKERS`
only as far as the host RAM allows (roughly 1–2 GB per concurrent job).

---

## Project layout

```
app.py                      Flask + Flask-RESTX API, Swagger, monitor routes
worker.py                   RQ job: runs the script inside FreeCAD, exports STEP/OBJ
config.py                   Environment-backed configuration
entrypoint.sh               Container init: Redis fallback, N workers, API
script_preprocessor.py      Normalizes user scripts before execution
job_contract.py             Job outcome envelope + error codes shared with the API
mqtt_client.py              Progress/status publisher
freecad_monitor.py          Resource + job sampling behind /freecad/monitor
client_user_upload.py       Reference CLI client
listen_mqtt.py              Debug MQTT subscriber

FreeCadUtil/                Geometry helpers (plate, bend, tube, coffre, analyzer)
src/utils/techdraw/         Technical drawing generation + A4 SVG templates
sheetmetal/                 Vendored FreeCAD SheetMetal workbench (third party)
static/, templates/         Monitor dashboard assets
font/                       Font used by generated drawings
storage/, outputs/          Runtime artifacts — volume-mounted, not versioned
```

`sheetmetal/` is a vendored copy of the community
[FreeCAD SheetMetal workbench](https://github.com/shaise/FreeCAD_SheetMetal)
(LGPL-2.1). It is pinned alongside FreeCAD 1.0.0 on purpose — do not upgrade one
without the other.

---

## CI/CD

`.gitlab-ci.yml` runs on the GitLab mirror for the `develop` branch and merge
requests targeting it:

1. **build** — builds the Docker image and pushes `latest`, the short SHA, and
   the branch slug to the private registry.
2. **deploy** — SSHes to the dev host, pulls, and restarts the stack with
   `docker-compose.dev.yml`.
3. **post-deploy** — HTTP health check against the deployed Swagger endpoint.

Required CI variables: `SSH_PRIVATE_KEY_DEVELOP`, `DEPLOY_TOKEN_NAME`,
`DEPLOY_TOKEN_SECRET`, plus the registry credentials GitLab injects.

---

## Notes

- FreeCAD is pinned to **1.0.0**. The generated scripts and the vendored
  sheetmetal workbench were verified against that release; an unpinned install
  resolves to 1.1.x and breaks geometry.
- `storage/` grows without bound (roughly 1 GB in normal use). Prune it
  periodically — nothing in the service reclaims it.
- The API runs Flask's development server. Put it behind a reverse proxy in
  production; the CI deployment does exactly that.
