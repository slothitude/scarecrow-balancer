# ScareCrow Balancer — Control GUI Guide

The balancer ships a single-page control GUI (`static/admin.html`) served by the
balancer process itself. It is the zero-downtime operator surface: add, edit,
delete and probe lanes, watch the ladder and the recent-requests ring — no
restart, no file surgery. This guide covers every control and what it calls.
Behavior reference: `docs/BALANCER_INTERNALS.md` (§5 admin surface).

## 1. What it is and how to open it

The GUI is one HTML file served by the balancer at:

```
GET /admin
```

Default deployment: `http://<host>:8486/admin` (port is `FORK_BALANCER_PORT`,
default 8486). Opening the page needs no token — `/admin` and `/health` are
ungated; only `/api/*` calls and chat completions check the bearer token.

If `/admin` returns a 404 JSON error, neither `/app/static/admin.html` nor
`<fork-dir>/static/admin.html` exists in that deployment — the UI file was not
shipped into the image/container.

## 2. First use — the token

Every data call the GUI makes (`/api/lanes` and friends) must carry
`Authorization: Bearer <FORK_BALANCER_TOKEN>` — exact match against the value
set on the server.

- On the first `/api/*` call that comes back **401**, the GUI prompts for the
  token. Enter the exact `FORK_BALANCER_TOKEN` value (from the deployment's
  `.env` / container environment).
- The entered value is stored in the browser's **localStorage** for the
  balancer origin. It survives page reloads and browser restarts; you enter it
  once per browser.
- If the token is wrong, every call returns 401
  (`{"error": "bad or missing bearer token"}`) and the GUI re-prompts — typing
  a new value at the prompt overwrites the stored one.
- To fully clear the stored token, clear site data / localStorage for the
  balancer origin in your browser.
- If the server runs with `FORK_BALANCER_TOKEN` empty, auth is off entirely:
  calls pass without a token and no prompt appears.

## 3. The interface

### 3.1 Header — health and ladder, auto-refresh

The header polls `GET /health` every **5 seconds** and shows:

- health flag and the canonical model (`scarecrow`),
- the **ladder** — enabled lanes in serving order (weight desc, then avg wall,
  then name),
- active instance count.

A pause control stops the polling. Pause it while you are mid-edit in the lane
editor so the view doesn't shift under you; unpause to resume live updates.

The ladder order is the round-robin cycle order, not a frequency ranking:
weight fixes a lane's position in the cycle, every healthy lane still gets an
equal share of turns.

### 3.2 Lane table

One row per lane, fed by `GET /api/lanes` (`lanes` array — the same snapshots
as `/health`). Columns:

| Column | Snapshot field | Meaning |
|---|---|---|
| Lane | `lane` | Lane id — also the pinning target (§4). |
| Model | `model` | Upstream model id this lane substitutes into requests. |
| Enabled | `enabled` | Disabled lanes leave the ladder; pinning one returns 503. |
| Cooldown | `cooldown_s` | Seconds of adaptive cooldown remaining, rounded. Lane is skipped by rotation until it expires. |
| Issues | `issues` | Consecutive-failure counter feeding the cooldown (5 s × issues, capped 600 s; decays one per clean success). |
| Requests | `requests` | Dispatch count on this lane since process start (resets on restart). |
| Errors | `errors` | Failure count since process start. |
| Avg wall | `avg_wall_s` | Mean of the last 20 successful wall times. `0.0` = no successes recorded yet (fresh lane or after a restart). |
| Last error | `last_err` | Last failure text, first 120 characters. |
| Actions | — | Probe now / Reset cooldown / Delete / Edit (§3.3). |

Rows are colored by state bucket: **disabled** (out of the ladder), **cooling**
(`cooldown_s > 0`), **issue-carrying** (`issues > 0`, cooldown expired) and
**clean/enabled** (participating normally).

The snapshot carries more than the table shows (`base_url`, `key_env`,
`has_key`, `weight`, `min_interval_s`, `timeout_s`, `extra_body`, `thinking`,
`waiting`, `last_wall_s`) — open **Edit** to see the full lane.

### 3.3 Per-lane actions

| Button | Effect (endpoint + payload) |
|---|---|
| **Probe now** | `POST /api/lanes/probe` with `{"name": "<lane>"}`. Sends one tiny upstream call (`max_tokens=8`, temperature 0, "Reply with exactly: OK"). Success shows `wall_s`, `out_tok`, `tok_s`; failure shows `ok: false` with `"<kind>: <last_err>"` (kinds: `429`, `5xx`, `httpN`, `unreachable`). Probes ignore and never touch cooldowns or pacing, but they do feed the rolling stats — probe walls nudge `avg_wall` and therefore ladder order. |
| **Reset cooldown** | `POST /api/lanes/reset` with `{"name": "<lane>"}`. Zeroes the lane's `issues` counter and cooldown; the lane rejoins the healthy pool immediately. |
| **Delete** | Confirm dialog, then `POST /api/lanes/delete` with `{"name": "<lane>"}`. Immediate and permanent — see §5. |
| **Edit** | Opens the lane editor (§3.4) prefilled with the lane's current values. Saving performs an upsert (§3.4). |

### 3.4 Lane editor (add / edit)

One form for both cases. Fields:

| Field | Type / default | Notes |
|---|---|---|
| `name` | string, required | Lane id, unique. **Same name replaces in place** (stats kept, keyed by lane name); a new name appends a new lane. |
| `base_url` | string, required | Upstream root. Trailing `/` stripped; `/chat/completions` is appended per call. Bridge lanes: `http://<bridge-host>:8507/v1`. |
| `model` | string, required | Upstream model id substituted into every request body. For browser-bridge lanes this is the bridge brain selector (`chatgpt`, `deepseek`, …) — see §4. |
| `key_env` | string, required | Name of the env var holding the bearer key. Cloud lanes: `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`. Bridge lanes: keep the sentinel `BRIDGE_ADAPTER_NO_KEY` — keyless lanes are legal and send no `Authorization` header. |
| `min_interval_s` | number > 0, default `1.0` | Pacing floor between dispatches on the lane. |
| `weight` | integer, default `0` | Ladder sort key only — fixes cycle position, not offer frequency. |
| `enabled` | bool, default `true` | Off = lane leaves the ladder; pinning it returns 503. |

Save calls `POST /api/lanes` with the full lane object (strict server-side
validation: unknown fields are rejected with 400; all four string fields must
be non-empty).

**Documented warning shown in the editor:** `timeout_s`, `extra_body` and
`thinking` are accepted by the admin API and written into the registry file,
but the registry file loader silently drops them on every disk re-read. After
a restart or an out-of-band file edit, lanes run with defaults — 240 s call
timeout, no extra body, thinking from the `SC_THINKING` env (default `off`).
The GUI form therefore only exposes the seven core fields; don't build lane
configuration on the optional three. Details: internals Appendix A.2.

### 3.5 Force reload

`POST /api/lanes/reload` (body ignored) — forces a full registry re-read and
lane rebuild, no mtime check. Use it after editing `/app/data/lanes.json` or
restoring a backup directly on disk; otherwise the next chat dispatch or any
`/api/*` call picks the file up by mtime anyway.

Consequences: per-lane request/error counters and `avg_wall` survive a rebuild
(stats are keyed by lane name), but **cooldowns and issue counts do not** —
rebuilding a lane is also an implicit cooldown clear for it. In-flight requests
are never interrupted; old lane objects finish their calls.

### 3.6 Probe all

Fires one probe call per enabled lane — the same `POST /api/lanes/probe` as
**Probe now**, once per lane, with each result shown. See §5 for why this is
deliberately not automated in this deployment.

### 3.7 Recent-requests ring

The ring viewer lists the last **50 dispatch completions, newest first** (the
backend ring holds 200; the API trims to 50). One row per dispatch, from
`GET /api/lanes` (`recent` array):

- `t` — completion time,
- `lane` — lane that served,
- `instance` — client `X-Sunny-Instance` name, `-` if none,
- `wall_s` — wall time, 2 dp,
- `status` — `"200"`, `"429"`, `"5xx"`, the numeric code for other HTTP
  statuses, or `"err"` for network failures. Quirk: a 5xx on the normal
  try-path is recorded as `"err"`, only the blocking path records `"5xx"`.
- `out_tok` — output tokens.

Probes do **not** create ring entries — the ring is real traffic only.

### 3.8 Bridge-brain callout

The GUI labels the two lane families differently, because they are different
kinds of upstream:

- **Browser-session brains** (`bridge-*` lanes) — each lane drives a logged-in
  chat session through the browser bridge on :8507. Requests land in a real
  browser conversation; there is no API key and no rate limit in the HTTP
  sense.
- **Cloud lanes** — conventional API providers with bearer keys
  (`NVIDIA_API_KEY`, `OPENROUTER_API_KEY`).

### 3.9 Status line

An inline status line shows the last error the GUI itself hit — a failed save,
a 401, a probe failure message. It clears on the next successful action. When
something misbehaves, read this line first; it carries the balancer's own
error text.

## 4. Lane pinning quick reference

**Setting `model` in a request to a lane name pins that brain** — no
round-robin, no failover. Any other model string (including `scarecrow` or a
typo) is discarded and the request goes to the round-robin pool; the upstream
model is whatever the chosen lane's `model` field says.

| `model` value | Brain | Family |
|---|---|---|
| `bridge-chatgpt` | **GPT-6** (OpenAI) | browser session |
| `bridge-deepseek` | DeepSeek | browser session |
| `bridge-mistral` | Mistral | browser session |
| `bridge-qwen` | Qwen | browser session |
| `bridge-gemini` | Gemini | browser session |
| `bridge-grok` | Grok | browser session |
| `bridge-kimi` | Kimi | browser session |
| `nvapi` | nemotron-3.5-super (NVIDIA cloud) | cloud API |
| `openrouter` | free rotating pool (OpenRouter) | cloud API |
| `scarecrow` (or anything else) | round-robin over the healthy pool | — |

Pinned-path facts worth knowing:

- No fail-through: any failure kind on the pinned lane ends the request
  (502 `lane <name> failed`). A disabled pinned lane returns 503.
- The pinned path uses the blocking dispatch — paced by `min_interval_s` and
  queued, not skipped when busy.
- Pinned requests are not fleet-attributed (`X-Sunny-Instance` is ignored).
- Successful responses carry `X-Fork-Lane` (which lane served) and the
  upstream `model` field names the brain — always report it.

## 5. Safety notes

- **Delete is immediate.** There is no trash, no undo. `POST
  /api/lanes/delete` writes the registry file back (atomic tmp-then-replace)
  and the change hot-reloads into the running process on the spot.
- **GUI edits live in the container's rw layer.** Lane edits made through the
  admin CRUD write into `/app/lanes.json` / `/app/data/lanes.json` inside the
  container — **not** into the repo's `config/lanes.json`. That layer dies
  with `docker rm`. Back up the registry before recreating the container
  (restore procedure: §6).
- **Probe-all sends real upstream calls.** One tiny call per lane
  (`max_tokens=8`, "Reply with exactly: OK"). Through the browser bridge a
  probe **lands in the browser chat session** — the chatgpt session sees the
  canary spam. That is why the periodic probe daemon ships **disabled**
  (`FORK_PROBE_INTERVAL_S=0` in this deployment); use Probe all / Probe now
  deliberately, and only enable the daemon if you accept the bridge traffic.
- **Probes nudge the ladder.** Probe successes feed the same rolling wall
  stats as real traffic, so probing changes `avg_wall` and therefore ladder
  order. Probes never set or clear cooldowns.
- **The `timeout_s` loader limitation.** `timeout_s`, `extra_body` and
  `thinking` survive an admin-API save (they are written to the registry
  file) but are dropped by the loader on every disk re-read — after a restart
  or an out-of-band edit the lane reverts to defaults (240 s timeout, no
  extra body, thinking per `SC_THINKING`). Don't depend on the optional
  three; if a lane needs them, that is a known loader gap, not a GUI bug.
- **The GUI page itself is unauthenticated.** Anyone who can reach the port
  can load `/admin`; the token gates the data and mutations. Keep the port
  off the open internet, and set `FORK_BALANCER_TOKEN` — with it empty,
  registry mutation is open to anyone who can reach the port.

## 6. Troubleshooting

### 401 loop (wrong token)

Symptom: every action bounces — the status line shows
`bad or missing bearer token`, and the GUI keeps re-prompting.

- The token must match `FORK_BALANCER_TOKEN` **exactly** — no quotes, no
  trailing whitespace. Get the live value from the deployment's `.env` or the
  container environment.
- Enter the correct value at the prompt; it overwrites the stored one. If the
  prompt keeps reappearing, the stored value still mismatches — clear
  localStorage for the balancer origin and re-enter.
- If the server genuinely runs tokenless (`FORK_BALANCER_TOKEN` unset), auth
  is off and no 401 should occur at all — a 401 means the server has a token
  and yours doesn't match it.

### Lanes missing after container recreate

Symptom: after `docker rm` + recreate, the lane table is missing lanes you
added or edited through the GUI.

Cause: admin-CRUD edits write into the container's rw layer, which dies with
the container. The repo copy is untouched.

Fix (per README):

1. `docker cp config/lanes.json <container>:/app/data/lanes.json` (the repo's
   `config/lanes.json` is the extracted full registry baseline).
2. Trigger a hot-reload: click **Force reload**, or make any `/api/*` request,
   or send any chat dispatch — the file is re-read by mtime.
3. Verify the ladder in the header, then re-apply any post-extract lane edits
   through the editor.

### Empty answers

Symptom: `200` responses with empty or near-empty `content`, or probe "OK"
results that look meaningless.

- Reasoning upstreams can spend the entire token budget thinking and return
  no visible content — **raise `max_tokens` on the client side** (the README
  example uses 6000 for bridge consults).
- Probe calls are tiny **by design** (`max_tokens=8`): a probe measures
  reachability and wall time, not answer quality. `ok: true` with a small
  `out_tok` means the lane answered, nothing more. Judge answers with a real
  pinned consult, not a probe.
- Empty upstream content is returned to clients as `""` (wrapped in the SSE
  chunk if streaming was requested) — check the ring's `out_tok` column to
  distinguish "upstream said nothing" from "wrong lane".
