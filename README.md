# micro-agent

A tiny terminal AI agent that calls LLM providers and drives a real Chromium browser over the Chrome DevTools Protocol (CDP).

## Features

- **Multi-provider** — GLM 5.2 (Z.ai), DeepSeek, OpenRouter, OpenCode, Ollama
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
PROVIDER=deepseek            # zai | deepseek | openrouter | opencode | ollama
DEEPSEEK_KEY=sk-...
# Per-provider overrides:  <NAME>_URL / <NAME>_MODEL / <NAME>_KEY
```

Override the config dir with `MAGENT_CONFIG_DIR=/path`.

## Run

```bash
micro-agent                  # default provider from .env
micro-agent -zai             # pick provider via flag
micro-agent -ollama          # local Ollama
```

## Project Layout

| File                  | Purpose                                     |
| --------------------- | ------------------------------------------- |
| `micro.py`            | Main agent loop, tools, providers           |
| `browser_action.py`   | CDP browser primitives                      |
| `cdp_fetch.py`        | JS-rendered webpage fetcher                 |
| `build-deb.sh`        | Builds a Debian `.deb` package              |
| `.env`                | Your keys/config (not shipped)              |

## License

MIT
