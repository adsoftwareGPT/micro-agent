# micro-agent

A tiny terminal AI agent that calls LLM providers and drives a real Chromium browser over the Chrome DevTools Protocol (CDP).

## Features

- **Multi-provider** — GLM 5.2 (Z.ai), Mistral, DeepSeek, OpenRouter, OpenCode, Ollama
- **Browser control** — drives Chromium via CDP (navigate, click, type, scroll, screenshot)
- **Shell execution** — run terminal commands from chat
- **Vision** — screenshot analysis via the LLM; requires an OpenRouter API key (`OPENROUTER_KEY` in `.env`)
- **Web fetch** — JS-rendered page extraction (`cdp_fetch`)
- **DuckDuckGo search** — optional (`pip install '.[search]'`)

## Requirements

- Python ≥ 3.10
- Chromium / Google Chrome
- An LLM provider API key

## Install

```bash
pip install .
# or build a .deb:
./build-deb.sh
```

## Configure

Copy `.env.example` → `.env` (same folder as `micro.py`) and set:

```ini
PROVIDER=deepseek            # zai | mistral | deepseek | openrouter | opencode | ollama
DEEPSEEK_KEY=sk-...
# Per-provider overrides:  <NAME>_URL / <NAME>_MODEL / <NAME>_KEY
```

### Available providers

| Provider    | Default model                | Key env var       | Notes                                            |
| ----------- | ---------------------------- | ----------------- | ------------------------------------------------ |
| `zai`       | `glm-5.2`                    | `ZAI_KEY`         | Z.ai / GLM; supports the `thinking` field         |
| `mistral`   | `mistral-small-latest`       | `MISTRAL_KEY`     | Strict API — returns 422 on unknown top-level fields (e.g. `thinking`) |
| `deepseek`  | `deepseek-v4-flash`          | `DEEPSEEK_KEY`    | Default if `PROVIDER` unset                       |
| `openrouter`| `xiaomi/mimo-v2.5`           | `OPENROUTER_KEY`  | Also powers the vision tool                       |
| `opencode`  | `deepseek-v4-flash-free`     | `OPENCODE_KEY`    | Free tier via opencode.ai                         |
| `ollama`    | `glm-5.2:cloud`              | — (local)         | Local Ollama at `http://localhost:11434`          |

Every provider can be overridden individually with `<NAME>_URL`, `<NAME>_MODEL`, and `<NAME>_KEY`. Only `<NAME>_SUPPORTS_THINKING=1` opts a provider into the Z.ai/GLM `thinking` field (default `0` for everyone except `zai`).

Override the config dir with `MAGENT_CONFIG_DIR=/path`.

## Run

```bash
micro-agent                  # default provider from .env
micro-agent -zai             # pick provider via flag
micro-agent -mistral
micro-agent -ollama          # local Ollama
```

## Project Layout

| File                  | Purpose                                     |
| --------------------- | ------------------------------------------- |
| `micro.py`            | Main agent loop, tools, providers           |
| `browser_action.py`   | CDP browser primitives                      |
| `cdp_fetch.py`        | JS-rendered webpage fetcher                 |
| `deletion_guard.py`   | Shell-tool deletion interceptor (see below) |
| `build-deb.sh`        | Builds a Debian `.deb` package              |
| `.env`                | Your keys/config (not shipped)              |

## Deletion Guard

The shell tool intercepts destructive operations *before* execution and
authorizes them on **provenance**, not on command pattern. It exists to defend
against **prompt injection**: instructions baked into a fetched webpage, a
`cat`-ed file, search results, or command stdout that try to trick the agent
into deleting data.

### The trust model

> A destructive op runs **iff the human user, in a recent genuine user
> message, named both a destructive verb AND the target** (or the target is
> a throwaway path).

Only `role: user` messages carry authority. Tool outputs (webpages, files,
command stdout, screenshots) and the agent's own reasoning **never** authorize
a deletion. `deletion_guard._recent_user_messages()` is the trust boundary,
and every tool output is wrapped in an `UNTRUSTED DATA` banner so the model is
primed to treat it as data, not instructions.

### The three tiers

When a destructive op is detected, it routes through:

1. **Tier A — throwaway path → ALLOW.** Target matches the throwaway allow-list
   (`/tmp`, `.cache/`, `__pycache__/`, `*.pyc`, `build/`, `node_modules/`,
   `.venv/`, the trash dir, etc.). Cache cleanup never needs a prompt.
2. **Tier B — user-authorized → ALLOW.** A recent `role:user` message contains
   *both* a destructive verb (`delete`/`drop`/`reset`/`truncate`/…) *and* the
   target token (`users.csv`, `users` table, `feature/old` branch, …). The
   verb and the target must appear in the **same** message — coincidental
   mentions across different messages don't count.
3. **Tier C — needs user confirmation → BLOCK.** Returns a
   `NEEDS USER CONFIRMATION` message that the agent must surface verbatim to
   the user. When the user replies naming both the verb and the target, the
   next call re-evaluates to Tier B and the op runs. After 3 identical retries
   the message escalates and tells the agent to stop retrying.

### What gets detected

- **Filesystem**: `rm`/`rmdir`/`unlink`/`shred`, `find … -delete`,
  truncate-to-empty (`> path`), `dd of=…`, `mkfs`/`wipefs`, and the common
  bypasses — `python -c "os.remove|shutil.rmtree …"`, `bash -c "… rm …"`,
  `eval "… rm …"`, `xargs rm`.
- **Databases** (via `psql`/`mysql`/`mariadb`/`sqlite3`/`redis-cli`/`mongo`,
  including heredocs): `DROP`, `TRUNCATE`, `DELETE FROM …` without `WHERE`,
  Redis `FLUSHALL`/`FLUSHDB`, Mongo `dropDatabase()`.
- **Git**: `reset --hard`, `clean -fd`, `branch -D`, force-push.

### What's allowed freely (Tier A throwaway list)

`/tmp`, `/var/tmp`, `.cache/`, `.trash/`, `__pycache__/`, `node_modules/`,
`build/`, `dist/`, `.venv/`, `venv/`, `.pytest_cache/`, `.mypy_cache/`,
`.ruff_cache/`, `.tox/`, `*.pyc`, rotated logs (`*.log.*`), and the trash dir.

### Configuration (env vars in `.env`)

| Var                    | Default                              | Effect                                       |
| ---------------------- | ------------------------------------ | -------------------------------------------- |
| `MICRO_ALLOW_DELETE`   | unset (guard **on**)                 | `=1` disables the guard entirely (human override) |
| `MICRO_TRASH_DIR`      | `$XDG_DATA_HOME/micro-agent/trash`   | Throwaway location; still in the Tier A list |

### Known limitations

The guard is high-recall on common destructive forms, **not a sandbox**.
Obfuscated deletion (`base64 | sh`, hex-escaped paths) can still slip past
detection — but those misses are mostly safe because Tier B then requires the
user to have authorized it explicitly anyway. The intercept happens in
`execute_shell`; bypassing it requires the human-only `MICRO_ALLOW_DELETE=1`
env var at launch. The intent check is a two-token heuristic, not
cryptographic; it is sized to defeat the realistic threat (injected
instructions in tool outputs) while keeping normal workflows frictionless.

## License

MIT
