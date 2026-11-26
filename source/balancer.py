#!/usr/bin/env python3
"""ScareCrow v2: sunny's cloud router + ladder.

ONE OpenAI-compatible endpoint for sunny, named ScareCrow (owner,
2026-11-09). POST /v1/chat/completions with model "scarecrow" (any
model name is accepted - the lane mapping picks the real upstream
model).

Lanes come from a hot-reloaded registry file, resolved as
$FORK_LANES_PATH, then /app/data/lanes.json (docker volume), then the
fork-dir copy beside this script. Keys NEVER live in the registry:
each lane names its env var via key_env. The registry is re-read
whenever its mtime changes (checked on every /api/* request and before
every chat dispatch); the admin CRUD writes it back atomically
(temp file + os.replace).

Routing: the LADDER is the enabled lanes ranked by
(-weight, avg wall over the last 20 successes, name). dispatch tries
the first healthy lane in ladder order and falls through to the next on
a lane error (HTTP 4xx breaks - a request-shape error fails everywhere
the same way). Lane health: bearer key present, enabled, not cooling
(429 -> Retry-After honored / 30s, 5xx or network -> 15s). Per-lane
lock + pacing still serialize and rate-limit each lane.

Auth: the router itself is bearer-gated by FORK_BALANCER_TOKEN; lane
keys are injected per request and never logged. Responses carry
X-Fork-Lane (which lane served) for evidence. Clients may send
X-Sunny-Instance (sunny sets it from $SUNNY_INSTANCE, s4.3) — the
router attributes traffic per instance for the fleet-scale admin UI.

Admin (all token-gated): GET /admin serves the single-file UI,
GET /api/lanes reports the ladder + lane state/stats + fleet
observability (per-sunny-instance attribution + the recent-requests
ring), POST /api/lanes
upserts one lane, POST /api/lanes/delete removes one, POST
/api/lanes/reload forces a re-read, POST /api/lanes/probe runs one tiny
timed call against a lane right now (a daemon thread repeats that probe
over every enabled lane every FORK_PROBE_INTERVAL_S seconds, default
1200). GET /health reports lane state + the ladder; GET /v1/models
lists scarecrow. Upstream stays non-streaming; a client that asked for
stream=true gets the reply wrapped as ONE SSE chunk.

Run:  python forks/nvapi/balancer.py            (foreground, stderr log)
Port: FORK_BALANCER_PORT (default 8486).
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORK_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_LOCAL = os.path.join(FORK_DIR, "env.local")
PORT = int(os.environ.get("FORK_BALANCER_PORT", "8486"))
HOST = os.environ.get("FORK_BALANCER_HOST", "0.0.0.0")
TOKEN = os.environ.get("FORK_BALANCER_TOKEN", "")   # optional bearer gate
CALL_TIMEOUT_S = 240
PROBE_INTERVAL_S = int(os.environ.get("FORK_PROBE_INTERVAL_S", "1200"))

CANON_MODEL = "scarecrow"

DATA_LANES = "/app/data/lanes.json"
FORK_LANES = os.path.join(FORK_DIR, "lanes.json")
ADMIN_HTML = ("/app/static/admin.html",
              os.path.join(FORK_DIR, "static", "admin.html"))
LANE_KEYS = ("name", "base_url", "model", "key_env",
             "min_interval_s", "weight", "enabled",
             "timeout_s", "extra_body", "thinking")
LANE_REQUIRED = ("name", "base_url", "model", "key_env")

PROBE_BODY = {"max_tokens": 8, "temperature": 0,
              "messages": [{"role": "user",
                            "content": "Reply with exactly: OK"}]}


def load_env_local(path):
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key:
                os.environ[key] = value
                n += 1
    return n


load_env_local(ENV_LOCAL)   # before the registry: key_env vars must exist


def _log(msg):
    sys.stderr.write("[scarecrow] %s\n" % msg)
    sys.stderr.flush()


# --------------------------------------------------------------------------
# lane registry (hot-reloaded lanes.json; keys live in env, never in the file)
# --------------------------------------------------------------------------

def _resolve_reg_path():
    """First existing candidate wins: $FORK_LANES_PATH, then the docker
    volume file, then the fork-dir copy. With none present the env
    override (if set) or the docker data dir is the write target."""
    env = os.environ.get("FORK_LANES_PATH")
    for cand in ([env] if env else []) + [DATA_LANES, FORK_LANES]:
        if os.path.isfile(cand):
            return cand
    if env:
        return env
    if os.path.isdir(os.path.dirname(DATA_LANES)):
        return DATA_LANES
    return FORK_LANES


REG_PATH = _resolve_reg_path()
_REG_LOCK = threading.Lock()
_REG = {"mtime": None, "lanes": []}    # lanes: normalized spec dicts


def _normalize(data):
    """Registry JSON -> normalized lane specs. Unknown fields dropped;
    entries without the four required strings skipped; duplicates keep
    the first. Lenient by design (the admin CRUD is the strict path)."""
    raw = data.get("lanes") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError('registry must be {"lanes": [...]}')
    specs, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        if not all(isinstance(item.get(k), str) and item.get(k).strip()
                   for k in LANE_REQUIRED):
            continue
        name = item["name"]
        if name in seen:
            continue
        seen.add(name)
        mi = item.get("min_interval_s", 1.0)
        if isinstance(mi, bool) or not isinstance(mi, (int, float)) or mi <= 0:
            mi = 1.0
        w = item.get("weight", 0)
        if isinstance(w, bool) or not isinstance(w, int):
            w = 0
        specs.append({"name": name, "base_url": item["base_url"],
                      "model": item["model"], "key_env": item["key_env"],
                      "min_interval_s": float(mi), "weight": w,
                      "enabled": bool(item.get("enabled", True))})
    return specs


def _write_registry(specs):
    """Atomically persist specs to REG_PATH (temp file + os.replace)."""
    d = os.path.dirname(REG_PATH)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = REG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"lanes": specs}, f, indent=2)
        f.write("\n")
    os.replace(tmp, REG_PATH)
    with _REG_LOCK:
        try:
            mt = os.stat(REG_PATH).st_mtime
        except OSError:
            mt = None
        _REG["mtime"], _REG["lanes"] = mt, list(specs)
    _log("registry written: %s (%d lanes)" % (REG_PATH, len(specs)))


def _seed_registry_if_missing():
    """Bootstrap a docker volume: if REG_PATH has no file yet but the
    baked-in fork-dir copy does, copy the defaults into REG_PATH."""
    if REG_PATH == FORK_LANES or os.path.isfile(REG_PATH):
        return
    if not os.path.isfile(FORK_LANES):
        return
    try:
        with open(FORK_LANES, "r", encoding="utf-8") as f:
            specs = _normalize(json.load(f))
        _write_registry(specs)
        _log("seeded registry %s from %s" % (REG_PATH, FORK_LANES))
    except Exception as e:
        _log("registry seed failed: %s" % e)


def _read_registry(force=False):
    """Re-read REG_PATH when its mtime changed (or force=True). A bad or
    unreadable file keeps the last good parse. Returns the specs list."""
    try:
        mt = os.stat(REG_PATH).st_mtime
    except OSError:
        with _REG_LOCK:
            return list(_REG["lanes"])
    with _REG_LOCK:
        if not force and _REG["mtime"] == mt:
            return list(_REG["lanes"])
    try:
        with open(REG_PATH, "r", encoding="utf-8") as f:
            specs = _normalize(json.load(f))
    except Exception as e:
        _log("registry %s bad (%s); keeping last good lanes" % (REG_PATH, e))
        with _REG_LOCK:
            _REG["mtime"] = mt     # only retry when the file changes again
            return list(_REG["lanes"])
    with _REG_LOCK:
        _REG["mtime"], _REG["lanes"] = mt, specs
        return list(specs)


# --------------------------------------------------------------------------
# lane stats (keyed by name so a registry edit never wipes history)
# --------------------------------------------------------------------------

_STATS = {}
_STATS_LOCK = threading.Lock()


def _stats_slot(name):
    """Create-on-first-use; caller must already hold _STATS_LOCK."""
    st = _STATS.get(name)
    if st is None:
        st = {"requests": 0, "errors": 0, "last_wall_s": 0.0,
              "walls": deque(maxlen=20)}
        _STATS[name] = st
    return st


def _stat_ok(name, wall):
    with _STATS_LOCK:
        st = _stats_slot(name)
        st["requests"] += 1
        st["walls"].append(wall)
        st["last_wall_s"] = wall


def _stat_err(name):
    with _STATS_LOCK:
        st = _stats_slot(name)
        st["requests"] += 1
        st["errors"] += 1


def _lane_stats(name):
    with _STATS_LOCK:
        st = _stats_slot(name)
        walls = list(st["walls"])
        return (st["requests"], st["errors"], st["last_wall_s"], walls)


# --------------------------------------------------------------------------
# fleet observability: per-instance attribution, recent-requests ring,
# per-lane queue depth (many sunny instances share this router at scale)
# --------------------------------------------------------------------------

INST_CAP = 50                 # LRU-ish cap on tracked instances
_INSTANCES = {}               # name -> {requests, last_seen, lanes_used:set}
_RING = deque(maxlen=200)     # one entry per lane dispatch completion
_FLEET_LOCK = threading.Lock()


def _instance_seen(instance):
    """One chat request arrived from `instance`: bump its counter and
    refresh last_seen. Over INST_CAP the least-recently-seen instance is
    evicted."""
    if not instance:
        return
    with _FLEET_LOCK:
        rec = _INSTANCES.get(instance)
        if rec is None:
            if len(_INSTANCES) >= INST_CAP:
                oldest = min(_INSTANCES,
                             key=lambda k: _INSTANCES[k]["last_seen"])
                del _INSTANCES[oldest]
            rec = {"requests": 0, "last_seen": 0.0, "lanes_used": set()}
            _INSTANCES[instance] = rec
        rec["requests"] += 1
        rec["last_seen"] = time.time()


def _instance_lane(instance, lane):
    """Record that `instance` traffic used `lane` (failover included)."""
    if not instance:
        return
    with _FLEET_LOCK:
        rec = _INSTANCES.get(instance)
        if rec is not None:
            rec["lanes_used"].add(lane)


def _ring_add(lane, instance, wall, status, out_tok):
    """One dispatch completion, success or failure."""
    with _FLEET_LOCK:
        _RING.append({"t": time.time(), "lane": lane,
                      "instance": instance or "-",
                      "wall_s": round(wall, 2), "status": status,
                      "out_tok": out_tok})


def _instances_report():
    """Sorted instance rows for /api/lanes (and the admin fleet strip)."""
    now = time.time()
    with _FLEET_LOCK:
        rows = [{"name": name, "requests": rec["requests"],
                 "last_seen_ago_s": round(max(0.0, now - rec["last_seen"]), 1),
                 "lanes_used": sorted(rec["lanes_used"])}
                for name, rec in _INSTANCES.items()]
    rows.sort(key=lambda r: (-r["requests"], r["name"]))
    return rows


def _ring_report(limit=50):
    """Newest-first slice of the recent-requests ring."""
    with _FLEET_LOCK:
        items = list(_RING)
    items.reverse()
    return items[:limit]


def _active_instance_count():
    return len(_INSTANCES)


# --------------------------------------------------------------------------
# lanes
# --------------------------------------------------------------------------

LANES = {}
_LANES_SIG = [""]
_LANES_LOCK = threading.Lock()


class Lane:
    def __init__(self, spec):
        self.name = spec["name"]
        self.base_url = spec["base_url"].rstrip("/")
        self.model = spec["model"]
        self.key_env = spec["key_env"]
        self.key = os.environ.get(self.key_env) or ""
        self.min_interval_s = float(spec["min_interval_s"])
        self.weight = int(spec["weight"])
        self.enabled = bool(spec["enabled"])
        self.timeout_s = float(spec.get("timeout_s") or 0) or None
        self.extra_body = dict(spec.get("extra_body") or {})
        # thinking lever (owner 2026-11-10: "test lanes with nothink low
        # to high ... set rr to no think"): per-lane override, else the
        # pool default (env SC_THINKING, default "off" — the tested
        # nothink knobs are 1.5-3x faster and stop empty-content replies)
        self.thinking = spec.get("thinking")             or os.environ.get("SC_THINKING", "off")
        self.lock = threading.Lock()
        self.next_ok_at = 0.0
        self.cooldown_until = 0.0
        self.issues = 0           # adaptive cooling: +1 per issue
        self.last_issue_at = 0.0  # for the 3h full-reset window
        self.last_err = ""
        self.waiting = 0          # requests currently queued on self.lock

    def healthy(self):
        # keyless lanes (the bridge adapter) are legal: they
        # just send no auth header — requiring a key would never rotate
        # them (the 2026-11-10 bridge-tier bug).
        return (self.enabled
                and time.monotonic() >= self.cooldown_until)

    def avg_wall(self):
        walls = _lane_stats(self.name)[3]
        return (sum(walls) / len(walls)) if walls else 0.0

    def snapshot(self):
        req, errs, last_wall, walls = _lane_stats(self.name)
        avg = (sum(walls) / len(walls)) if walls else 0.0
        cd = max(0.0, self.cooldown_until - time.monotonic())
        return {"lane": self.name, "model": self.model,
                "base_url": self.base_url, "key_env": self.key_env,
                "has_key": bool(self.key), "enabled": self.enabled,
                "weight": self.weight,
                "min_interval_s": self.min_interval_s,
                "timeout_s": self.timeout_s,
                "extra_body": self.extra_body or None,
                "thinking": self.thinking,
                "issues": self.issues,
                "cooldown_s": round(cd, 1),
                "last_err": self.last_err[:120],
                "requests": req, "errors": errs,
                "waiting": self.waiting,
                "last_wall_s": round(last_wall, 2),
                "avg_wall_s": round(avg, 2)}

    def _http_call(self, body):
        """One POST /chat/completions. Returns (resp, None, wall) on 200
        or (None, {"kind", "retry_after"}, wall) on failure; sets
        last_err. No locking, pacing, cooldowns or stats - callers own
        those. Per-lane: timeout_s overrides CALL_TIMEOUT_S; extra_body
        is merged into the request (per-lane thinking knobs)."""
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        if self.extra_body:
            body = dict(body)
            for k, v in self.extra_body.items():
                if isinstance(v, dict) and isinstance(body.get(k), dict):
                    body[k] = {**body[k], **v}
                else:
                    body[k] = v
        # thinking lever, translated per upstream dialect (tested live
        # 2026-11-10: nvapi enable_thinking=false 0.7s clean; openrouter
        # reasoning{low,exclude} 0.9s clean; defaults burn tokens on
        # reasoning and can return EMPTY content). Bridge lanes (web
        # chats) have no API knob -> no-op there.
        th = self.thinking
        if th and ":8507" not in self.base_url:
            body = dict(body)
            if "integrate.api.nvidia.com" in self.base_url:
                if th == "off":
                    ctk = dict(body.get("chat_template_kwargs") or {})
                    ctk["enable_thinking"] = False
                    body["chat_template_kwargs"] = ctk
                # low/high: nvapi ignores reasoning_effort (tested) -> default
            elif "openrouter" in self.base_url:
                r = dict(body.get("reasoning") or {})
                if th == "off":
                    r.update({"effort": "low", "exclude": True})
                elif th == "low":
                    r.update({"effort": "low"})
                elif th == "high":
                    r.update({"effort": "high"})
                body["reasoning"] = r
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"), headers=headers)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s
                                        or CALL_TIMEOUT_S) as r:
                resp = json.loads(r.read().decode("utf-8"))
            return resp, None, time.time() - t0
        except urllib.error.HTTPError as e:
            ebody = ""
            try:
                ebody = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            self.last_err = "HTTP %s %s" % (e.code, ebody)
            kind = ("429" if e.code == 429
                    else ("5xx" if e.code >= 500 else "http%d" % e.code))
            ra = None
            if kind == "429" and e.headers:
                ra = e.headers.get("Retry-After")
            return None, {"kind": kind, "retry_after": ra}, time.time() - t0
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                OSError) as e:
            self.last_err = "%s: %s" % (type(e).__name__, str(e)[:120])
            return None, {"kind": "unreachable", "retry_after": None}, \
                time.time() - t0

    def try_dispatch(self, payload, instance=None):
        """FIFO pool worker grab (owner law 2026-11-10: "fifo quewing of
        the pool worker no waiting"): NON-BLOCKING lock, no pacing
        sleep — a busy/cooling lane is simply skipped. Returns
        (resp, None) served | ("busy", None) | (None, kind) failed."""
        if not self.lock.acquire(blocking=False):
            return "busy", None
        try:
            if time.monotonic() < self.cooldown_until:
                return "busy", None
            body = dict(payload)
            body["model"] = self.model
            resp, err, wall = self._http_call(body)
            if resp is not None:
                _log("%s 200 wall=%.1fs" % (self.name, wall))
                _stat_ok(self.name, wall)
                _ring_add(self.name, instance, wall, "200",
                          (resp.get("usage")
                           or {}).get("completion_tokens") or 0)
                # adaptive cooling: clean success cools the lane down
                self.issues = max(0, self.issues - COOLDOWN_DECAY)
                self.cooldown_until = 0.0
                return resp, None
            kind = err["kind"]
            _log("%s %s wall=%.1fs" % (self.name, kind, wall))
            _stat_err(self.name)
            _ring_add(self.name, instance, wall,
                      "429" if kind == "429"
                      else (kind[4:] if kind.startswith("http")
                            else "err"), 0)
            self.last_err = str(err)[:200]
            # adaptive cooling: +COOLDOWN_STEP_S per issue, cumulative;
            # a lane clean for COOLDOWN_RESET_S starts fresh at 0
            if time.monotonic() - self.last_issue_at > COOLDOWN_RESET_S:
                self.issues = 0
            self.last_issue_at = time.monotonic()
            self.issues += 1
            cd = min(COOLDOWN_STEP_S * self.issues, COOLDOWN_MAX_S)
            if kind == "429":
                ra = float(err.get("retry_after") or 0)
                self.cooldown_until = time.monotonic() + min(max(ra, cd),
                                                            COOLDOWN_MAX_S)
            else:
                self.cooldown_until = time.monotonic() + cd
            return None, kind
        finally:
            self.lock.release()

    def dispatch(self, payload, instance=None):
        """Serialize + pace + one POST. Returns (resp, None) or
        (None, kind) with kind in 429 / 5xx / httpN / unreachable.
        The waiting counter covers lock queue pressure (incremented at
        entry, decremented after the POST completes) and every
        completion lands in the fleet ring."""
        with _FLEET_LOCK:
            self.waiting += 1
        try:
            with self.lock:
                wait = self.next_ok_at - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self.next_ok_at = time.monotonic() + self.min_interval_s
                body = dict(payload)
                body["model"] = self.model
                resp, err, wall = self._http_call(body)
                if resp is not None:
                    _log("%s 200 wall=%.1fs" % (self.name, wall))
                    _stat_ok(self.name, wall)
                    _ring_add(self.name, instance, wall, "200",
                              (resp.get("usage")
                               or {}).get("completion_tokens") or 0)
                    self.issues = max(0, self.issues - COOLDOWN_DECAY)
                    self.cooldown_until = 0.0
                    return resp, None
                kind = err["kind"]
                _log("%s %s wall=%.1fs" % (self.name, kind, wall))
                _stat_err(self.name)
                _ring_add(self.name, instance, wall,
                          "429" if kind == "429"
                          else (kind[4:] if kind.startswith("http")
                                else ("5xx" if kind == "5xx" else "err")),
                          0)
                # adaptive cooling: +COOLDOWN_STEP_S per issue, cumulative;
                # a lane clean for COOLDOWN_RESET_S starts fresh at 0
                if time.monotonic() - self.last_issue_at > COOLDOWN_RESET_S:
                    self.issues = 0
                self.last_issue_at = time.monotonic()
                self.issues += 1
                cd = min(COOLDOWN_STEP_S * self.issues, COOLDOWN_MAX_S)
                if kind == "429":
                    try:
                        ra = float(err["retry_after"])
                    except (TypeError, ValueError):
                        ra = 0.0
                    self.cooldown_until = time.monotonic() + min(max(ra, cd),
                                                                COOLDOWN_MAX_S)
                else:
                    self.cooldown_until = time.monotonic() + cd
                return None, kind
        finally:
            with _FLEET_LOCK:
                self.waiting -= 1

    def probe(self):
        """One tiny timed call right now: ignores cooldown (it IS the
        health check), takes the lane lock, updates rolling stats.
        Never raises; never touches cooldowns or pacing."""
        body = dict(PROBE_BODY)
        body["model"] = self.model
        with self.lock:
            resp, err, wall = self._http_call(body)
        if resp is not None:
            _stat_ok(self.name, wall)
            out_tok = (resp.get("usage") or {}).get("completion_tokens") or 0
            tok_s = (out_tok / wall) if wall > 0 else 0.0
            _log("probe %s ok wall=%.2fs out_tok=%s"
                 % (self.name, wall, out_tok))
            return {"ok": True, "wall_s": round(wall, 2),
                    "out_tok": out_tok, "tok_s": round(tok_s, 2)}
        _stat_err(self.name)
        head = "%s: %s" % (err["kind"], self.last_err[:100])
        _log("probe %s FAIL %s" % (self.name, head))
        return {"ok": False, "error": head}


def ensure_registry():
    """Rebuild the LANES dict when the registry file changed. Stats
    survive rebuilds (keyed by lane name)."""
    specs = _read_registry()
    sig = json.dumps(specs, sort_keys=True)
    with _LANES_LOCK:
        if sig == _LANES_SIG[0]:
            return
        lanes = {}
        for spec in specs:
            try:
                ln = Lane(spec)
            except Exception as e:
                _log("lane %r skipped: %s" % (spec.get("name"), e))
                continue
            lanes[ln.name] = ln
        LANES.clear()
        LANES.update(lanes)
        _LANES_SIG[0] = sig
        _log("lanes loaded from %s: %s" % (REG_PATH,
                                           ", ".join(lanes) or "(none)"))


def ladder_order():
    """Enabled lanes ranked by (-weight, avg wall if >0 else inf, name)."""
    ranked = sorted(LANES.values(), key=lambda ln: (-ln.weight,
                                                    ln.avg_wall()
                                                    if ln.avg_wall() > 0
                                                    else float("inf"),
                                                    ln.name))
    return [ln for ln in ranked if ln.enabled]


def ladder_names():
    return [ln.name for ln in ladder_order()]


# owner law 2026-11-10: adaptive per-lane cooling. After a request the
# lane's cooldown starts at 0s; each ISSUE adds COOLDOWN_STEP_S seconds
# (cumulative per lane); each clean success decays the count. Env-tunable.
COOLDOWN_STEP_S = float(os.environ.get("SC_COOLDOWN_STEP_S", "5"))
COOLDOWN_DECAY = int(os.environ.get("SC_COOLDOWN_DECAY", "1"))
# a lane clean for this long wipes its issue count back to 0 (owner:
# "the cooldowns just reset back to 0 after 3hrs")
COOLDOWN_RESET_S = float(os.environ.get("SC_COOLDOWN_RESET_S", "10800"))
# hard cap: no lane ever cools longer than this (owner: "max 600 secs")
COOLDOWN_MAX_S = float(os.environ.get("SC_COOLDOWN_MAX_S", "600"))

_RR = [0]   # round-robin rotation counter (owner law 2026-11-10:
            # "scarecrow is to put them all in a round robbin, all of
            # them" — every healthy lane takes its turn, all equal)


def dispatch(payload, instance=None):
    """ROUND-ROBIN over healthy lanes (rotation starts at a per-request
    advancing index), failover to the next lane when a lane errors.
    Returns (response_dict|None, err|None)."""
    ensure_registry()
    _instance_seen(instance)
    order = ladder_order()
    lanes = [ln for ln in order if ln.healthy()]
    if not lanes:
        # all cooling: try enabled lanes anyway rather than 503
        lanes = [ln for ln in order if ln.enabled]
    if not lanes:
        return None, "no dispatchable lanes (disabled or missing key)"
    # owner law 2026-11-10: CLOUD ONLY. One flat round-robin pool —
    # every lane equal, weight is display-only. No local lanes, ever.
    rot = lanes
    i = _RR[0] % len(rot)
    _RR[0] += 1
    lanes = rot[i:] + rot[:i]
    # FIFO over the pool: take the FIRST FREE worker, never wait on a
    # busy one while another is idle.
    err = None
    for ln in lanes:
        resp, kind = ln.try_dispatch(payload, instance=instance)
        if resp == "busy":
            continue
        _instance_lane(instance, ln.name)
        if resp is not None:
            resp.setdefault("fork_lane", ln.name)
            return resp, None
        err = "%s: %s" % (ln.name, kind)
        if kind.startswith("http"):
            break   # request-shape error: other lanes won't fix it
    if err is None and all(ln.lock.locked() for ln in lanes):
        # whole pool busy: FIFO — this request waits its turn at the head
        resp, kind = lanes[0].dispatch(payload, instance=instance)
        _instance_lane(instance, lanes[0].name)
        if resp is not None:
            resp.setdefault("fork_lane", lanes[0].name)
            return resp, None
        return None, "%s: %s" % (lanes[0].name, kind)
    return None, err


def validate_lane_obj(obj):
    """Strict admin validation. Returns (normalized_spec, None) or
    (None, error_string). Unknown top-level keys are rejected."""
    if not isinstance(obj, dict):
        return None, "lane must be a JSON object"
    unknown = sorted(k for k in obj if k not in LANE_KEYS)
    if unknown:
        return None, "unknown field(s): %s" % ", ".join(unknown)
    for k in LANE_REQUIRED:
        v = obj.get(k)
        if not isinstance(v, str) or not v.strip():
            return None, "%s must be a non-empty string" % k
    mi = obj.get("min_interval_s")
    if isinstance(mi, bool) or not isinstance(mi, (int, float)) or mi <= 0:
        return None, "min_interval_s must be a positive number"
    w = obj.get("weight", 0)
    if isinstance(w, bool) or not isinstance(w, int):
        return None, "weight must be an integer"
    en = obj.get("enabled", True)
    if not isinstance(en, bool):
        return None, "enabled must be a boolean"
    spec = {"name": obj["name"], "base_url": obj["base_url"],
            "model": obj["model"], "key_env": obj["key_env"],
            "min_interval_s": float(mi), "weight": w, "enabled": en}
    # per-lane call timeout (owner 2026-11-10: slow web-chat bridges need
    # a longer window than APIs) and per-lane thinking controls via
    # extra_body (whatever knob the upstream speaks: reasoning_effort,
    # chat_template_kwargs.enable_thinking, reasoning.effort, ...)
    to = obj.get("timeout_s")
    if to is not None:
        if isinstance(to, bool) or not isinstance(to, (int, float)) or to <= 0:
            return None, "timeout_s must be a positive number"
        spec["timeout_s"] = float(to)
    eb = obj.get("extra_body")
    if eb is not None:
        if not isinstance(eb, dict):
            return None, "extra_body must be a JSON object"
        spec["extra_body"] = eb
    th = obj.get("thinking")
    if th is not None:
        if th not in ("off", "low", "high"):
            return None, "thinking must be off|low|high"
        spec["thinking"] = th
    return spec, None


def _probe_loop():
    while True:
        time.sleep(PROBE_INTERVAL_S)
        try:
            ensure_registry()
            lines = []
            for ln in ladder_order():
                try:
                    r = ln.probe()
                    lines.append("%s %s" % (
                        ln.name,
                        ("ok %.2fs" % r["wall_s"]) if r["ok"]
                        else ("FAIL %s" % r.get("error", "?"))))
                except Exception as e:
                    lines.append("%s probe-exc %s: %s"
                                 % (ln.name, type(e).__name__, str(e)[:60]))
            _log("probe round (every %ds): %s"
                 % (PROBE_INTERVAL_S, " | ".join(lines)))
        except Exception as e:      # the daemon must never die
            _log("probe round crashed: %s: %s" % (type(e).__name__, e))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # quiet default access log
        pass

    def _send(self, code, obj, lane=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if lane:
            self.send_header("X-Fork-Lane", lane)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not TOKEN:
            return True
        return (self.headers.get("Authorization") or "") == "Bearer " + TOKEN

    def _deny(self):
        self.close_connection = True   # request body may be unconsumed
        self._send(401, {"error": "bad or missing bearer token"})

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")), None
        except Exception as e:
            return None, "bad json: %s" % e

    def do_GET(self):
        p = self.path.split("?", 1)[0].rstrip("/")
        if p == "/health":
            ensure_registry()
            self._send(200, {"ok": True, "model": CANON_MODEL,
                             "ladder": ladder_names(),
                             "active_instances": _active_instance_count(),
                             "lanes": [ln.snapshot()
                                       for ln in LANES.values()]})
        elif p == "/v1/models":
            self._send(200, {"object": "list", "data": [
                {"id": CANON_MODEL, "object": "model",
                 "owned_by": "fork-big"}]})
        elif p == "/api/lanes":
            if not self._authorized():
                self._deny()
                return
            ensure_registry()
            self._send(200, {"ladder": ladder_names(),
                             "instances": _instances_report(),
                             "recent": _ring_report(),
                             "lanes": [ln.snapshot()
                                       for ln in LANES.values()]})
        elif p == "/admin":
            self._serve_admin()
        else:
            self._send(404, {"error": "not found"})

    def _serve_admin(self):
        path = next((c for c in ADMIN_HTML if os.path.isfile(c)), None)
        if path is None:
            self._send(404, {"error": "admin ui not found"})
            return
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError as e:
            self._send(404, {"error": "admin ui unreadable: %s" % e})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, resp, lane):
        """The upstream call is non-streaming; a client that asked for
        stream=true gets the reply wrapped as ONE OpenAI-shaped delta
        chunk + [DONE] (sunny-chat's SSE relay parses exactly this)."""
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        chunk = {"id": resp.get("id", "scarecrow"),
                 "object": "chat.completion.chunk",
                 "model": CANON_MODEL,
                 "choices": [{"index": 0, "finish_reason": "stop",
                              "delta": {"role": "assistant",
                                        "content": msg.get("content") or ""}}]}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if lane:
            self.send_header("X-Fork-Lane", lane)
        self.end_headers()
        self.close_connection = True
        self.wfile.write(("data: %s\n\n" % json.dumps(chunk))
                         .encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _api_upsert(self, payload):
        spec, err = validate_lane_obj(payload)
        if err:
            self._send(400, {"ok": False, "error": err})
            return
        with _REG_LOCK:
            specs = [dict(s) for s in _REG["lanes"]]
        for i, s in enumerate(specs):
            if s.get("name") == spec["name"]:
                specs[i] = spec
                break
        else:
            specs.append(spec)
        try:
            _write_registry(specs)
        except Exception as e:
            self._send(500, {"ok": False, "error": "registry write failed: %s"
                             % e})
            return
        ensure_registry()
        self._send(200, {"ok": True})

    def _api_delete(self, payload):
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not name:
            self._send(400, {"ok": False, "error": "name must be provided"})
            return
        with _REG_LOCK:
            specs = [dict(s) for s in _REG["lanes"]]
        kept = [s for s in specs if s.get("name") != name]
        if len(kept) == len(specs):
            self._send(400, {"ok": False, "error": "no such lane: %s" % name})
            return
        try:
            _write_registry(kept)
        except Exception as e:
            self._send(500, {"ok": False, "error": "registry write failed: %s"
                             % e})
            return
        ensure_registry()
        self._send(200, {"ok": True})

    def _api_reload(self):
        _read_registry(force=True)
        ensure_registry()
        self._send(200, {"ok": True})

    def _api_probe(self, payload):
        ensure_registry()
        name = payload.get("name") if isinstance(payload, dict) else None
        ln = LANES.get(name) if isinstance(name, str) else None
        if ln is None:
            self._send(400, {"ok": False,
                             "error": "no such lane: %s" % name})
            return
        try:
            result = ln.probe()
        except Exception as e:      # probes never take the server down
            result = {"ok": False,
                      "error": "%s: %s" % (type(e).__name__, str(e)[:100])}
        self._send(200, result)

    def do_POST(self):
        p = self.path.split("?", 1)[0].rstrip("/")
        if p.endswith("/chat/completions"):
            self._chat()
        elif p == "/api/lanes":
            if not self._authorized():
                self._deny()
                return
            payload, err = self._read_json()
            if err:
                self._send(400, {"ok": False, "error": err})
                return
            self._api_upsert(payload)
        elif p == "/api/lanes/delete":
            if not self._authorized():
                self._deny()
                return
            payload, err = self._read_json()
            if err:
                self._send(400, {"ok": False, "error": err})
                return
            self._api_delete(payload)
        elif p == "/api/lanes/reset":
            if not self._authorized():
                self._deny()
                return
            payload, err = self._read_json()
            if err:
                self._send(400, {"ok": False, "error": err})
                return
            ensure_registry()
            ln = LANES.get(str(payload.get("name") or ""))
            if ln is None:
                self._send(404, {"ok": False, "error": "no such lane"})
                return
            ln.cooldown_until = 0.0
            ln.issues = 0
            self._send(200, {"ok": True, "lane": ln.name, "issues": 0,
                             "cooldown_s": 0.0})
        elif p == "/api/lanes/reload":
            if not self._authorized():
                self._deny()
                return
            self._api_reload()
        elif p == "/api/lanes/probe":
            if not self._authorized():
                self._deny()
                return
            payload, err = self._read_json()
            if err:
                self._send(400, {"ok": False, "error": err})
                return
            self._api_probe(payload)
        else:
            self._send(404, {"error": "not found"})

    def _chat(self):
        if not self._authorized():
            self._deny()
            return
        payload, err = self._read_json()
        if err:
            self._send(400, {"error": err})
            return
        # direct-name access: model == a lane name -> THAT lane only
        # (cloud lane pinning; the round-robin stays the default)
        requested = str(payload.get("model") or "").strip()
        ensure_registry()
        if requested in LANES:
            lane = LANES[requested]
            if not lane.enabled:
                self._send(503, {"error": {"message": "lane %s disabled"
                                                   % requested}})
                return
            resp, derr = lane.dispatch(payload)
            if resp is None:
                self._send(502, {"error": {"message": "lane %s failed"
                                                   % requested, "last": derr}})
                return
            resp.setdefault("fork_lane", lane.name)
            wants_stream = bool(payload.get("stream"))
            payload.pop("stream", None)
            lane_name = resp.pop("fork_lane", None)
            if wants_stream:
                self._send_sse(resp, lane_name)
            else:
                self._send(200, resp, lane=lane_name)
            return
        payload["model"] = CANON_MODEL     # lane mapping decides upstream
        wants_stream = bool(payload.get("stream"))
        payload.pop("stream", None)        # upstream stays non-streaming
        instance = (self.headers.get("X-Sunny-Instance") or "").strip()[:64]
        resp, derr = dispatch(payload, instance=instance or None)
        if resp is None:
            self._send(502, {"error": {"message": "all fork lanes failed",
                                       "last": derr}})
            return
        lane = resp.pop("fork_lane", None)
        if wants_stream:
            self._send_sse(resp, lane)
            return
        self._send(200, resp, lane=lane)


def main():
    _seed_registry_if_missing()
    ensure_registry()
    for ln in LANES.values():
        if not ln.key:
            _log("lane %s has NO KEY (%s unset) - it will never dispatch"
                 % (ln.name, ln.key_env))
    if PROBE_INTERVAL_S > 0:
        threading.Thread(target=_probe_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    _log("ScareCrow on http://%s:%d/v1  model=%s  lanes=%s  registry=%s  "
         "probe=%ds  token=%s"
         % (HOST, PORT, CANON_MODEL, ",".join(ladder_names()), REG_PATH,
            PROBE_INTERVAL_S, "on" if TOKEN else "off"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
