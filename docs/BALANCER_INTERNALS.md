# ScareCrow Balancer — Internals & Behavior Reference

For full-control operators. Every claim below traces to a line in
`source/balancer.py` (1039 lines, extracted verbatim from the running
`sunny-scarecrow` docker container on Lappy) or `config/lanes.json`.
Line numbers are given as `L<n>` throughout. Where the code and the
module docstring disagree, this document follows the code; the known
disagreements are listed in Appendix A.

The balancer is ONE OpenAI-compatible endpoint: clients POST to
`/v1/chat/completions` with any model name, the lane mapping picks the
real upstream model (L4-7, L1005). Canonical model id: `scarecrow`
(`CANON_MODEL`, L64).

---

## 1. Runtime configuration

### Environment variables

| Variable | Default (L58-62, L629-635, L357) | What it does | What breaks without it |
|---|---|---|---|
| `FORK_BALANCER_PORT` | `8486` (L58) | Listen port. | Non-default deployments collide or misdirect clients; `int()` parse failure crashes startup. |
| `FORK_BALANCER_HOST` | `0.0.0.0` (L59) | Bind address. | Unset = binds all interfaces. Set to `127.0.0.1` to keep the router off the LAN. |
| `FORK_BALANCER_TOKEN` | `""` = auth OFF (L60) | Bearer gate for chat + `/api/*` (L770-773). Exact match `Authorization: Bearer <TOKEN>` required. | **Auth is completely off** — any host that can reach the port can chat and mutate the registry. |
| `FORK_PROBE_INTERVAL_S` | `1200` (L62) | Probe daemon period, seconds. `<= 0` disables the daemon entirely (L1028-1029). | No periodic health probing; lanes are only discovered bad on real traffic. |
| `FORK_LANES_PATH` | unset (L113) | First-priority registry file path (see §2.4). | Registry resolves to `/app/data/lanes.json`, then the fork-dir copy. |
| `SC_THINKING` | `"off"` (L357) | Pool-default thinking lever for lanes that do not specify one (see §2.1, `thinking`). | Defaults to `off` anyway. |
| `SC_COOLDOWN_STEP_S` | `5` (L629) | Seconds added per issue in adaptive cooling. | Defaults apply. |
| `SC_COOLDOWN_DECAY` | `1` (L630) | Issues removed per clean success. | Defaults apply. |
| `SC_COOLDOWN_RESET_S` | `10800` (3 h, L633) | Issue-count reset window (see §3.5 for the exact lazy mechanism). | Defaults apply. |
| `SC_COOLDOWN_MAX_S` | `600` (L635) | Hard cap on any single cooldown. | Defaults apply. |

### Lane key environment variables

Each lane names its key env var via `key_env`; keys NEVER live in the
registry (L11-12, L70-73). Resolved once at `Lane` construction
(`self.key = os.environ.get(self.key_env) or ""`, L347).

Per `config/lanes.json`: `NVIDIA_API_KEY` (lane `nvapi`),
`OPENROUTER_API_KEY` (lane `openrouter`), `BRIDGE_ADAPTER_NO_KEY`
(the four `bridge-*` lanes — a sentinel; keyless lanes are legal by
design and send no `Authorization` header, L366-371).

Consequence of a missing key on a keyed lane: the request goes out
with no auth header; the upstream typically answers 401; kind
`http401` **aborts failover** (see §3.4), so depending on rotation
position the client sees intermittent 502s even while other lanes are
healthy. The lane still rotates (health does not require a key,
L366-371).

### env.local

`<fork-dir>/env.local` is read once at startup, before the registry is
resolved (L56-57, L80-97): `KEY=VALUE` lines, `#` comments, values
into `os.environ`. This is the intended place for lane keys outside
docker. It never reloads at runtime — changes need a restart.

### Hard-coded constants

| Constant | Value | Line |
|---|---|---|
| `CALL_TIMEOUT_S` | 240 s — default upstream call timeout, used when the lane has no `timeout_s` (L61, L441-442) | L61 |
| `CANON_MODEL` | `"scarecrow"` | L64 |
| Registry fallback path | `/app/data/lanes.json` | L66 |
| Admin UI candidates | `/app/static/admin.html`, then `<fork-dir>/static/admin.html` | L68-69 |
| Probe body | `max_tokens=8, temperature=0, "Reply with exactly: OK"` | L75-77 |
| Instance cap | 50 tracked `X-Sunny-Instance` names (LRU-ish eviction) | L264 |
| Recent-requests ring | 200 entries, one per lane dispatch completion | L266 |

---

## 2. The lanes registry

### 2.1 Lane schema

Full field set accepted by admin upsert (`LANE_KEYS`, L70-72; strict
validation L686-729):

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | str, required | — | Lane id. Unique; also the pinning target (§4). |
| `base_url` | str, required | — | Upstream root; trailing `/` stripped (L344); `/chat/completions` appended (L437). |
| `model` | str, required | — | Upstream model id substituted into every request body (L475, L527). |
| `key_env` | str, required | — | Env var holding the bearer key. |
| `min_interval_s` | number > 0 | `1.0` (loader, L147-149; admin L698-700) | Pacing floor between dispatches on the lane (L522-525). |
| `weight` | int | `0` (L150-152, L701-703) | Ladder sort key only — see §3.2. |
| `enabled` | bool | `true` (L156, L704-706) | Disabled lanes leave the ladder (L619) and 503 when pinned (L987-990). |
| `timeout_s` | number > 0, optional | none → `CALL_TIMEOUT_S` 240 s (L351, L441-442) | Per-lane call timeout. See Appendix A.2: lost on file re-read. |
| `extra_body` | object, optional | `{}` | Merged into each request body; dict values deep-merge one level (L406-412). See Appendix A.2. |
| `thinking` | `"off" \| "low" \| "high"`, optional | lane value, else `SC_THINKING` env, else `"off"` (L357) | Per-lane reasoning knob, translated per upstream dialect (L417-435). See Appendix A.2. |

### 2.2 Lenient file loader vs strict admin validation

File loads go through `_normalize` (L129-157): non-dict entries
skipped, entries missing any of the four required strings skipped,
duplicate names keep the FIRST, unknown fields dropped, bad
`min_interval_s`/`weight` silently replaced by defaults, `enabled`
default true. A structurally bad file (not `{"lanes": [...]}`) raises,
and the last good parse is kept (L133-135, L209-213).

Admin upsert goes through `validate_lane_obj` (L686-729): unknown
top-level fields are REJECTED (400), all four required strings must be
non-empty, types enforced. The admin CRUD is the strict path by
design (L130-132).

### 2.3 Hot-reload semantics

The registry file is re-read **only when its mtime changed**
(`_read_registry`, L195-216; stat cached in `_REG["mtime"]`). The
mtime is checked:

- before every chat dispatch (`ensure_registry()` inside `dispatch()`,
  L646, and in `_chat`, L984);
- on every `/api/*` GET/POST that touches lanes (`/api/lanes` L803,
  upsert/delete via `ensure_registry` after write L874/894, reload
  L897-900, probe L903, reset L947);
- in the probe daemon round (L736).

An unreadable or corrupt file keeps the last good lanes and pins the
failed mtime, so it is retried only when the file changes again
(L209-213).

Rebuild (`ensure_registry`, L589-609): the normalized specs are
JSON-serialised into a signature; if unchanged from the loaded
signature, nothing happens. Otherwise every lane object is rebuilt.
**Stats survive rebuilds** — `_STATS` is keyed by lane name, not by
lane object (L223-234, L591). Cooldowns/issue counts do NOT survive
(object state on the fresh `Lane`); a registry edit is therefore also
an implicit cooldown clear for changed lanes.

### 2.4 Resolution order

`_resolve_reg_path` (L109-121) — first EXISTING file wins:

1. `$FORK_LANES_PATH` (if set and the file exists)
2. `/app/data/lanes.json` (docker volume)
3. `<fork-dir>/lanes.json` (baked-in copy)

If nothing exists: `$FORK_LANES_PATH` (if set) is the write target,
else `/app/data/lanes.json` if `/app/data` exists, else the fork-dir
copy. The resolved path is fixed at startup (`REG_PATH`, L124) — it is
not re-resolved at runtime.

Bootstrap: `_seed_registry_if_missing` (L179-192) copies the fork-dir
defaults into `REG_PATH` on first boot in a fresh docker volume.

### 2.5 Atomic write-back

`_write_registry` (L160-176): mkdir parent if needed → write
`REG_PATH + ".tmp"` → `os.replace` onto `REG_PATH` → refresh the
cached mtime and in-memory specs under `_REG_LOCK`. Readers can never
observe a half-written file.

---

## 3. Routing and the ladder

### 3.1 Ladder ranking

`ladder_order` (L612-619): enabled lanes sorted by
`(-weight, avg_wall if avg_wall > 0 else +inf, name)`, where
`avg_wall` is the mean of the last 20 successful wall times for that
lane name (`walls` deque `maxlen=20`, L232; L373-375). Lanes with no
successful calls yet sort after all measured lanes (infinite avg),
ties broken by name. `ladder_names` returns just the names (L622-623).

### 3.2 Dispatch order — flat round-robin

Despite the weight sort, scheduling is a flat round-robin: "One flat
round-robin pool — every lane equal, weight is display-only"
(L655-656). Mechanism (L657-660): the healthy-lane ladder is rotated
by a persistent index `_RR` that advances once per request; the
request then tries lanes in that rotated order. Net effect: **weight
fixes a lane's position within the rotation cycle but not how often it
is offered work** — over N requests every healthy lane gets N/len
turns regardless of weight.

Healthy-pool selection (L648-654): `healthy()` is `enabled and not
cooling` (L366-371 — a missing key does NOT make a lane unhealthy).
If every lane is cooling, the enabled lanes are tried anyway rather
than returning 503. If no lane is enabled at all: error
`"no dispatchable lanes (disabled or missing key)"` (L654 — the
"missing key" wording is stale; see Appendix A.3).

### 3.3 Per-lane behaviour on the hot path

Two dispatch mechanisms exist:

- `try_dispatch` (L464-510) — the normal path. Non-blocking lock grab;
  a busy or cooling lane is **skipped instantly** (returns `"busy"`,
  L469-473). No pacing sleep.
- `dispatch` (L512-565) — the blocking path. Serialises on the lane
  lock, sleeps until `next_ok_at` if pacing demands, then sets
  `next_ok_at = now + min_interval_s` (L521-525). Used (a) when the
  WHOLE pool was busy, at the head lane (L675-682), and (b) always on
  the pinned-lane path (§4). A `waiting` counter tracks lock queue
  pressure (L518-519, L563-565).

Both paths share `_http_call` (L397-462): one POST `/chat/completions`
with `model` rewritten to the lane's upstream model, optional bearer
header, `extra_body` merge, thinking translation (L417-435):
nvapi gets `chat_template_kwargs.enable_thinking=false` when off
(low/high are ignored by nvapi — L426); openrouter gets
`reasoning {effort, exclude}` per level; lanes whose base_url contains
`:8507` (the web-chat bridge) are skipped entirely (L419).

### 3.4 Fail-through rules

Per request, lanes are tried in rotated ladder order until one serves
(L664-674). Failure kinds from `_http_call` (L452-453, L461):
`429`, `5xx`, `httpN` (any other HTTP status: 400/401/403/404/422
...), `unreachable` (URLError/timeout/connection/OSError).

| Kind | Continues to next lane? | Cooldown |
|---|---|---|
| `429` | YES | see §3.5, includes Retry-After |
| `5xx` | YES | see §3.5 |
| `unreachable` (network/timeout) | YES | see §3.5 |
| `httpN` (other 4xx) | **NO — aborts the whole request** (L673-674: "request-shape error: other lanes won't fix it") | see §3.5 |

So an upstream 401 (e.g. missing key) or 400 kills the request
immediately wherever it lands in the rotation; only 429, 5xx and
network errors fail through.

Whole-pool-busy fallback (L675-682): if no lane failed outright but
every lane's lock is held, the request BLOCKS on the head lane via
`dispatch()`. If at least one lane actually failed (even while others
were busy), the request returns the failure instead of queueing.

Every completion — success or failure — lands in the fleet ring
(L480-482, L490-493, L532-534, L541-545).

Client-facing failures: pool exhausted → HTTP 502
`{"error": {"message": "all fork lanes failed", "last": "<lane>: <kind>"}}`
(L1010-1013). Malformed JSON body → 400 (L977-980).

### 3.5 Cooldowns (adaptive)

Owner law 2026-11-10, env-tunable (L626-635). Per lane: an `issues`
counter starts at 0 (L361-362).

On failure (L546-561, same logic in `try_dispatch` L495-507):

1. If the previous issue was more than `SC_COOLDOWN_RESET_S`
   (default 10800 s) ago, `issues` restarts at 0 first. NOTE: this
   reset is evaluated lazily **at the moment of a new failure** —
   there is no background timer that wipes the count while the lane
   sits clean (see Appendix A.1).
2. `issues += 1`; `cooldown = min(SC_COOLDOWN_STEP_S * issues,
   SC_COOLDOWN_MAX_S)` — cumulative: 5 s, 10 s, 15 s ... capped at
   600 s (defaults).
3. `429`: `cooldown_until = now + min(max(Retry-After, cooldown),
   SC_COOLDOWN_MAX_S)` — the upstream Retry-After is honored but
   never below the adaptive floor, and capped at 600 s
   (L553-559). Non-numeric Retry-After → 0 → pure adaptive value.
4. Everything else: `cooldown_until = now + cooldown`.

On success (L535-536): `issues = max(0, issues - SC_COOLDOWN_DECAY)`
and the cooldown is cleared to 0.

A cooling lane is skipped by `try_dispatch` (L472-473) and excluded
from the healthy pool (L649) — unless ALL lanes are cooling, in which
case the enabled ones are tried anyway (L651-653). Probe calls ignore
cooldowns entirely (L568-574). `/api/lanes/reset` manually zeroes
cooldown + issues (L952-953).

---

## 4. Lane pinning

Direct-name access (L981-1004): if the request's `model` string
(stripped) exactly equals a lane name in `LANES`, THAT lane alone
serves the request — no round-robin, no failover.

- Disabled pinned lane → HTTP 503 `{"error": {"message": "lane <name>
  disabled"}}` (L987-990).
- Pinned lane failure → HTTP 502 `{"error": {"message": "lane <name>
  failed", "last": <kind>}}` (L992-995). Any failure kind, including
  `429`/`5xx`/`unreachable`, ends the request — there is no
  fail-through on the pinned path.
- The pinned path uses blocking `dispatch()` — paced by
  `min_interval_s` and queued (not the non-blocking FIFO).
- Pinned requests are NOT fleet-attributed: `X-Sunny-Instance` is not
  read and `_instance_seen`/`_instance_lane` are not called on this
  path (L991 vs L647/L668).

Any OTHER model id — including `scarecrow` itself or a typo — is
overwritten with `CANON_MODEL` and dispatched to the round-robin pool;
the upstream model is whatever the chosen lane's `model` field says
(L1005, L475/L527). The client's model string is otherwise discarded;
there is no per-request model → lane mapping beyond the lane-name
match.

---

## 5. Admin surface

### 5.1 Endpoint table

| Method + path | Auth | Payload / body | Response | Lines |
|---|---|---|---|---|
| `GET /admin` | **none** | — | `admin.html` (first existing of `/app/static/admin.html`, `<fork-dir>/static/admin.html`), 404 JSON if neither exists | L809-810, L814-829 |
| `GET /health` | **none** | — | `{"ok": true, "model": "scarecrow", "ladder": [names], "active_instances": <int>, "lanes": [<snapshot>...]}` | L788-794 |
| `GET /v1/models` | **none** | — | `{"object": "list", "data": [{"id": "scarecrow", "object": "model", "owned_by": "fork-big"}]}` | L795-798 |
| `GET /api/lanes` | bearer | — | `{"ladder": [names], "instances": [<instance rows>], "recent": [<ring rows> newest-first, max 50], "lanes": [<snapshot>...]}` | L799-808 |
| `POST /api/lanes` | bearer | full lane object (§2.1 schema); strict validation, unknown fields → 400; same `name` replaces, new name appends | `{"ok": true}`; 400 validation error; 500 registry write failure | L921-929, L855-875, L686-729 |
| `POST /api/lanes/delete` | bearer | `{"name": "<lane>"}` | `{"ok": true}`; 400 missing name or no such lane; 500 write failure | L930-938, L877-895 |
| `POST /api/lanes/reset` | bearer | `{"name": "<lane>"}` | `{"ok": true, "lane": ..., "issues": 0, "cooldown_s": 0.0}`; 404 no such lane | L939-955 |
| `POST /api/lanes/reload` | bearer | — (body ignored) | `{"ok": true}` — forces a full re-read + rebuild | L956-960, L897-900 |
| `POST /api/lanes/probe` | bearer | `{"name": "<lane>"}` | `{"ok": true, "wall_s": <s>, "out_tok": <n>, "tok_s": <n>}` or `{"ok": false, "error": "<kind>: <last_err>"}`; 400 no such lane | L961-969, L902-915 |
| `POST <any path ending in /chat/completions>` | bearer | OpenAI chat body | chat completion (see §3, §7) | L919-920, L973-1018 |

Auth mechanics (L770-777): if `FORK_BALANCER_TOKEN` is empty every
request passes; otherwise the `Authorization` header must equal
`Bearer <TOKEN>` exactly. Failure → connection closed + 401
`{"error": "bad or missing bearer token"}`. Note the module docstring
says "Admin (all token-gated)" (L31) but in the code `/admin`,
`/health` and `/v1/models` are UNGATED — only `/api/*` and chat are
gated (code wins; Appendix A.4).

Lane snapshot fields (per lane in `/health` and `/api/lanes`,
L377-395): `lane, model, base_url, key_env, has_key, enabled, weight,
min_interval_s, timeout_s, extra_body (null when empty), thinking,
issues, cooldown_s (seconds remaining, rounded), last_err (first 120
chars), requests, errors, waiting, last_wall_s, avg_wall_s`.

### 5.2 The probe daemon

`_probe_loop` (L732-751), started only when `PROBE_INTERVAL_S > 0`
(L1028-1029), daemon thread:

1. Sleep `PROBE_INTERVAL_S` FIRST — the first round happens one full
   interval after boot, not at startup.
2. `ensure_registry()`, then probe every lane in ladder order
   (enabled lanes only).
3. Each probe (`Lane.probe`, L567-586): takes the lane lock, sends
   `PROBE_BODY` (`max_tokens=8`, temp 0, "Reply with exactly: OK",
   L75-77) with the lane's model/thinking/extra_body, **ignores
   cooldowns** ("it IS the health check"), never touches
   cooldown/pacing, but DOES update the rolling stats (success walls /
   error counts — probes therefore influence `avg_wall` ranking).
4. One summary line per round to stderr (L748-749); any exception is
   caught — the daemon never dies (L750-751).

Probes do NOT create ring entries or instance attribution (no
`_ring_add` in `probe`).

---

## 6. Observability

- **`X-Fork-Lane` header** — on successful chat responses (JSON and
  SSE alike), names the lane that served (L765-767, L846-847,
  L1003/L1018). Popped from the body (`fork_lane` key, L670/L996/
  L1014) so the JSON stays OpenAI-shaped. Admin endpoints do not
  carry it.
- **`X-Sunny-Instance` attribution** — on round-robin chat requests
  the client may send `X-Sunny-Instance`; it is read, stripped and
  truncated to 64 chars (L1008). Per instance the balancer tracks
  request count, last-seen time, and the set of lanes used (including
  failover targets) (L270-296). Cap 50 instances; over the cap the
  least-recently-seen is evicted (L279-284). NOT read on the
  pinned-lane path. Report rows: `{name, requests, last_seen_ago_s,
  lanes_used}`, sorted by `(-requests, name)` (L308-317).
- **Recent-requests ring** — `deque(maxlen=200)`, one entry per
  dispatch completion (L299-305):
  `{t, lane, instance ("-" if none), wall_s (2dp), status, out_tok}`.
  Status strings: `"200"`, `"429"`, `"5xx"`, the numeric code for
  other HTTP errors, or `"err"` for network failures. Quirk: the
  blocking `dispatch()` maps 5xx to `"5xx"` (L544) but the normal
  `try_dispatch` path records 5xx as `"err"` (L490-493).
  `/api/lanes` returns the newest 50, newest first (`_ring_report`,
  L320-325).
- **`/health` shape** — see §5.1. `active_instances` is the current
  tracked-instance count (L328-329, L792). Unlike `/api/lanes` it has
  no instances/recent breakdown.
- **stderr log** — one line per dispatch
  (`<lane> 200 wall=Xs` / `<lane> <kind> wall=Xs`), probe rounds,
  registry writes, lane loads (L100-102, L478, L488, L530, L539,
  L748, L176, L608). Access logging is suppressed (L757-758). Lane
  keys are never logged.

---

## 7. Streaming behaviour

The upstream call is ALWAYS non-streaming — `stream` is popped from
the payload before dispatch (L998, L1007). A client that asked for
`stream=true` gets the full reply wrapped as ONE OpenAI-shaped SSE
chunk plus `[DONE]` (`_send_sse`, L831-853):

```
data: {"id": <upstream id or "scarecrow">, "object": "chat.completion.chunk",
       "model": "scarecrow",
       "choices": [{"index": 0, "finish_reason": "stop",
                    "delta": {"role": "assistant", "content": <full content>}}]}

data: [DONE]
```

Headers: `Content-Type: text/event-stream`, `Cache-Control: no-store`,
`Connection: close`; `X-Fork-Lane` included; connection forced closed
afterwards (L842-849). Empty/missing upstream content becomes `""`.
There are no intermediate chunks, no keepalives, no token-by-token
streaming.

---

## 8. Operational runbook

### 8.1 Change lanes WITHOUT downtime (preferred)

The admin CRUD is the zero-downtime path — no restart, no dropped
requests:

1. Add or replace a lane: `POST /api/lanes` with the full object.
   Replace-in-place keeps the lane's stats (keyed by name, L223-234).
2. Remove a lane: `POST /api/lanes/delete {"name": ...}`.
3. Edited the file out-of-band (ssh edit of `/app/data/lanes.json`)?
   Either wait — the next chat dispatch or `/api/*` call picks it up
   by mtime (§2.3) — or force it: `POST /api/lanes/reload`.
4. Verify: `GET /api/lanes` (ladder + per-lane stats), spot-check with
   `POST /api/lanes/probe {"name": ...}`.

CAVEAT (Appendix A.2): an out-of-band file edit drops any
`timeout_s`/`extra_body`/`thinking` values from the loaded lanes.
Prefer the admin API for lanes that need those fields.

In-flight requests are never interrupted by a registry change: the
old lane objects finish their calls; new dispatches build fresh
lanes on the next request.

### 8.2 Restart

Run: `python forks/nvapi/balancer.py` (foreground, stderr logs, L43).
Container path: the image runs the same script; registry at
`/app/data/lanes.json` (volume — survives restart), admin UI at
`/app/static/admin.html`.

A restart resets ALL in-memory state:

| State | Restart effect |
|---|---|
| `avg_wall` / last-20 walls (`_STATS`) | **Reset to empty** — every lane reads 0.0 until 20 fresh successes accumulate; ladder tie-break falls back to `(+inf, name)` order |
| per-lane request/error counters | Reset to 0 |
| cooldowns + `issues` counters | Reset to 0 — every lane starts clean |
| fleet attribution (`_INSTANCES`) + ring (`_RING`) | Reset — instance histories and the recent-requests list are empty |
| round-robin index `_RR` | Reset to 0 — rotation restarts at ladder head |
| registry file | **Survives** (it is on disk / the docker volume) |

In-flight requests die with the process — clients see connection
errors, not 5xx from the balancer. On boot: seed registry if missing
(L1022), load lanes (L1023), warn about keyless lanes (L1024-1027 —
misleading for bridge lanes, A.3), start the probe daemon (L1028-1029),
bind and serve (L1030-1035); the boot line prints host/port, ladder,
registry path, probe interval, and whether the token gate is on.

### 8.3 Port / key setup

1. Port: set `FORK_BALANCER_PORT` (default 8486). Bind scope via
   `FORK_BALANCER_HOST` (default all interfaces).
2. Keys: put each lane's key in the env var its `key_env` names —
   container env (`docker run -e NVIDIA_API_KEY=...`) or the fork-dir
   `env.local` file (startup-only, §1). Keys are never written to
   `lanes.json`.
3. Gate: set `FORK_BALANCER_TOKEN` or the API is open to anyone who
   can reach the port — including registry mutation. Clients then pass
   the same token as their OpenAI API key.
4. Registry location: set `FORK_LANES_PATH` to override; otherwise
   `/app/data/lanes.json` (volume) then the baked-in copy (§2.4).

---

## Appendix A — code/docstring discrepancies (documented per the code)

- **A.1 Cooldown docstring is stale.** L21-23 claims "429 → Retry-After
  honored / 30s, 5xx or network → 15s". No 30/15 constants exist; the
  implemented scheme is the adaptive one of §3.5 (verified by import:
  `STEP=5.0 DECAY=1 RESET=10800.0 MAX=600.0`).
- **A.2 Registry loader drops `timeout_s`, `extra_body`, `thinking`.**
  `_normalize` (L129-157) emits only the 7 core fields; only admin
  upsert (`validate_lane_obj`) preserves the optional three. Verified
  by import: `_normalize` on the shipped `config/lanes.json` (whose
  four `bridge-*` lanes carry `timeout_s: 360.0`) yields specs without
  it — those lanes run with the 240 s `CALL_TIMEOUT_S`, not 360 s,
  after any file load/restart. Values written via `POST /api/lanes`
  persist in the file and stay honored for the current process, then
  revert on the next disk re-read.
- **A.3 Keyless-lane messages are misleading but still emitted.**
  Health does not require a key (L366-371), yet startup still warns
  "NO KEY ... will never dispatch" (L1024-1027) and the empty-pool
  error says "disabled or missing key" (L654). Keyless bridge lanes DO
  dispatch.
- **A.4 "Admin (all token-gated)" (L31) overstates the gate.** Only
  `/api/*` and chat are gated; `/admin`, `/health`, `/v1/models` are
  open (§5.1).
- **A.5 Weight "display-only" (L656) needs the precise reading of
  §3.2:** it fixes cycle position, not offer frequency.
