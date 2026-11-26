# scarecrow-balancer

> **Standalone, full-control deployment of ScareCrow** — the round-robin model
> balancer that fronts ten consultation brains (GPT-6, DeepSeek, Mistral, Qwen,
> Gemini, Grok, Kimi, nemotron, openrouter) behind one OpenAI-compatible endpoint.

Extracted verbatim from the running `sunny-scarecrow` docker container (Lappy,
2026-11-26) and rebuilt for **full control**: source and lane registry are
bind-mounted, so every edit — code, lanes, weights, pins — is a host-file edit
plus a restart, never image surgery. The lane registry even hot-reloads by mtime.

## The one thing to know

**Lane pinning.** Set `model` to a lane name and THAT brain answers, exclusively
(`balancer.py:981`). Everything else is round-robin, nvapi-weighted 100/50/10.

| Lane | Brain |
|---|---|
| `bridge-chatgpt` | **GPT-6** (OpenAI, via browser bridge :8507) |
| `bridge-deepseek` / `bridge-mistral` / `bridge-qwen` | DeepSeek / Mistral / Qwen |
| `bridge-gemini` / `bridge-grok` / `bridge-kimi` | Gemini / Grok / Kimi |
| `nvapi` | nemotron-3.5-super-120b (NVIDIA cloud) |
| `openrouter` | free rotating pool |
| `scarecrow` | round-robin default |

```bash
curl -H "Authorization: Bearer $FORK_BALANCER_TOKEN" -H "Content-Type: application/json" \
  http://<host>:8486/v1/chat/completions \
  -d '{"model": "bridge-chatgpt", "messages": [{"role":"user","content":"..."}], "max_tokens": 6000}'
```

Responses carry `X-Fork-Lane` — which lane served — and the upstream `model` field
names the brain. Always report it.

## Deploy (standalone)

```bash
cp .env.example .env          # fill in keys (the running container holds live values)
cd deploy
docker compose up -d          # bind-mounts ../source and ../config — full control
curl http://127.0.0.1:8486/health
```

**Cutover from sunny-scarecrow is owner-gated:** shadow on port 8487 first
(`FORK_BALANCER_PORT=8487`), verify `/health` + a pinned consult, then flip the port
and stop the old container. Never silently.

**Shadow-validated 2026-11-26:** extracted source ran standalone on a second machine —
health ok, full ladder, pinned `bridge-chatgpt` consult returned the exact canary with
upstream `chatgpt`.

## Full control

- `config/lanes.json` — lanes, weights, pins, enable/disable. **Hot-reloaded by mtime**:
  edit and save, no restart.
- `source/balancer.py` — the router itself. Edit + restart.
- `deploy/docker-compose.yml` — port, keys (from `.env`), restart policy.
- Admin surface (token-gated): `GET /admin` (single-file UI), lane CRUD
  (`POST /api/lanes`, `/api/lanes/delete`), force reload, live probe, `GET /health`.
- Docs: `docs/BALANCER_INTERNALS.md` — the complete behavior reference.

## Keys

Never committed. `.env` is gitignored; `.env.example` is the template. Live values
live in the running container's environment.

---

*Extracted and standalone-validated 2026-11-26 by the nnfs lane, owner-directed.
Internals reference: see `docs/BALANCER_INTERNALS.md`.*
