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

### Web UI

The same agent also runs in a browser-based chat UI (stdlib only, no extra deps):

```bash
python3 web.py               # serves on http://localhost:5555
python3 web.py --port 8080   # custom port
```

## Project Layout

| File                  | Purpose                                     |
| --------------------- | ------------------------------------------- |
| `micro.py`            | Main agent loop, tools, providers           |
| `web.py`              | Browser-based chat UI (port 5555)            |
| `browser_action.py`   | CDP browser primitives                      |
| `cdp_fetch.py`        | JS-rendered webpage fetcher                 |
| `build-deb.sh`        | Builds a Debian `.deb` package              |
| `.env`                | Your keys/config (not shipped)              |

## License

MIT
