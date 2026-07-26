# Micro

A compact, terminal-based **AI agent** (agentic CLI chatbot) written in a single Python file (`micro.py`, ~500 lines). It runs an autonomous tool-using loop powered by OpenAI-compatible chat-completion APIs, giving the model full access to a Linux shell, screen vision, web search, and webpage fetching.

> **⚠️ Heads up:** API keys are hardcoded near the top of `micro.py` (Z.ai, DeepSeek, OpenRouter, OpenCode). Rotate/replace them before sharing the file, or move them to environment variables.

---

## ✨ Features

| Capability | Description |
|---|---|
| 🤖 **Agentic tool loop** | The model keeps calling tools until the task is done (up to `MAX_TOOL_LOOPS = 2500` steps), then replies. |
| 🔀 **Multi-provider** | Switch providers by editing one constant: `zai` (GLM 5.2, default), `deepseek`, `openrouter`, or `opencode`. All use the OpenAI chat-completions schema. |
| 💻 **Shell tool** | Execute arbitrary shell commands (`subprocess`, 120s timeout, 350KB output cap). |
| 👁️ **Vision tool** | Take a desktop screenshot (`gnome-screenshot`/`scrot`), downscale to ≤1024px JPEG q75 (~tens of KB), and send to a vision LLM (default `qwen/qwen2.5-vl-72b-instruct`) for a textual description. Can also analyze an existing image path. |
| 🔎 **DuckDuckGo search** | Up to 30 results (title, URL, snippet) via the `ddgs` library. |
| 🌐 **Fetch webpage** | Pull a URL and get clean plain text (scripts/styles/HTML tags stripped). Randomized browser `User-Agent` + realistic headers for anti-bot resilience. |
| 💾 **Rolling chat logs** | User + agent messages written to `chat.YYYYMMDD_HHMMSS.log.txt`; oldest pruned to keep the 3 most recent. |
| ⏸️ **Interrupt key** | Press `Ctrl+A` during tool execution to stop the loop and inject a follow-up message. |
| 🔁 **API retries** | Network errors / timeouts retried with exponential backoff (2 attempts). |
| 💭 **Optional thinking** | Toggle `THINKING_ENABLED` to enable/disable the provider's reasoning mode. |

---

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- A Linux desktop (GNOME or with `scrot`) — only needed for the `vision` tool
- API key for at least one provider

### Install dependencies

```bash
pip install requests pillow ddgs
```

> Note: `micro.py` looks for `ddgs` inside the venv at
> `/home/adsoftware/browser-robot-portable/.venv/lib/python3.12/site-packages`,
> so either keep that path writable or `pip install ddgs` globally.

### Run

```bash
cd /home/adsoftware/microtest
./micro.py        # or: python3 micro.py
```

You'll see a prompt like:

```
  Provider: deepseek | Model: deepseek-v4-flash | Mode: no-think
You: _
```

Type a message (e.g. *"list the largest files in this folder"*) and the agent will call `shell`, read the output, and answer. Type `quit` / `exit` / `bye`, or press `Ctrl+C`, to leave.

---

## ⚙️ Configuration

All config lives at the top of `micro.py`.

### Provider selection

```python
PROVIDER = "zai"   # "zai" | "deepseek" | "openrouter" | "opencode"
```

Each provider block defines `URL`, `KEY`, and `MODEL`. Add your own and register it in the `PROVIDERS` dict:

```python
PROVIDERS = {
    "zai":        {"url": ZAI_URL,        "key": ZAI_KEY,        "model": ZAI_MODEL},
    "deepseek":   {"url": DEEPSEEK_URL,   "key": DEEPSEEK_KEY,   "model": DEEPSEEK_MODEL},
    "openrouter": {"url": OPENROUTER_URL, "key": OPENROUTER_KEY, "model": OPENROUTER_MODEL},
    "opencode":   {"url": OPENCODE_URL,   "key": OPENCODE_KEY,   "model": OPENCODE_MODEL},
}
```

| Provider | Default model | Endpoint |
|---|---|---|
| `zai` | `glm-5.2` | `https://api.z.ai/api/coding/paas/v4/chat/completions` |
| `deepseek` | `deepseek-v4-flash` | `https://api.deepseek.com/chat/completions` |
| `openrouter` | `xiaomi/mimo-v2.5` | `https://openrouter.ai/api/v1/chat/completions` |
| `opencode` | `deepseek-v4-flash-free` | `https://opencode.ai/zen/v1/chat/completions` |

### Tunable constants

| Constant | Default | Purpose |
|---|---|---|
| `MAX_TOKENS` | 38192 | Max response tokens per LLM call. |
| `TOOL_OUTPUT_MAX_CHARS` | 350000 | Cap on tool output fed back to the model. |
| `MAX_TOOL_LOOPS` | 2500 | Max agentic steps before forcing a final answer. |
| `MAX_API_RETRIES` | 2 | Retries on connection/timeout errors. |
| `API_CONNECT_TIMEOUT` / `API_READ_TIMEOUT` | 30 / 120 | HTTP timeouts (seconds). |
| `SHELL_TIMEOUT` | 120 | Shell command timeout. |
| `VISION_MODEL` | `qwen/qwen2.5-vl-72b-instruct` | Vision LLM (overridable via `VISION_MODEL` env var). |
| `VISION_MAX_WIDTH` / `VISION_JPEG_QUALITY` | 1024 / 75 | Screenshot compression. |
| `THINKING_ENABLED` | False | Toggle reasoning mode. |
| `BREAK_KEY_ENABLED` | True | Enable Ctrl+A interrupt. |
| `LOG_KEEP` | 3 | How many chat log files to retain. |

### System prompt

```python
SYSTEM_PROMPT = (
    "- You are an AI expert having full linux at hand\n"
    "- current date: {current_date}\n"
    "- Chat history (user & agent messages) is logged to "
    "/home/adsoftware/chat.*.log.txt ..."
)
```

The `{current_date}` placeholder is filled in at startup.

---

## 🧠 How It Works

```mermaid
flowchart TD
    A[User input] --> B[call_llm with TOOLS]
    B --> C{Response has tool_calls?}
    C -- yes --> D[handle_tool_calls]
    D --> E[Execute each tool: shell / vision / ddg_search / fetch_webpage]
    E --> F{Ctrl+A pressed?}
    F -- yes --> Z[Stop, allow injection]
    F -- no --> B
    C -- no --> G[Print agent reply → back to prompt]
```

### The agentic loop (`_loop`)

1. Sends the full conversation + tool definitions to the LLM.
2. If the model emits `tool_calls`, each call is dispatched via `TOOL_EXECUTORS` and its output is appended as a `tool` role message.
3. The loop repeats (polling for the break key between steps) until the model returns plain content with no tool calls — that's its final answer.

### Tool dispatch

```python
TOOL_EXECUTORS = {
    "shell":        execute_shell,
    "vision":       execute_vision,
    "fetch_webpage":execute_fetch_webpage,
    "ddg_search":   execute_ddg_search,
}
```

`handle_tool_calls` includes lenient JSON parsing for malformed model arguments (newline-escaping, unbalanced-quote repair, regex extraction for `cmd`), so a stray `"` from the model won't crash the run.

---

## 🛠️ Tools in Detail

### `shell(cmd)`
Runs through `subprocess.run(..., shell=True)`, returns `stdout + stderr` truncated to 350 KB. Times out at 120 s.

### `vision(prompt?, path?, delay?)`
- If no `path`, captures via `gnome-screenshot` (falls back to `scrot`), waiting for the file to appear.
- Opens with Pillow, downscales to ≤1024 px wide, re-encodes JPEG q75.
- Sends `{text prompt, image_url(data: URI)}` to OpenRouter's vision endpoint.
- Returns the model's description plus image dimensions and token usage.

### `ddg_search(query)`
Returns up to 30 DuckDuckGo results, numbered, with title/URL/snippet.

### `fetch_webpage(url)`
GETs the URL with browser-like headers via a shared `requests.Session`, strips HTML using the stdlib `html.parser`, and returns readable text (or raw body for non-HTML).

---

## 📝 Chat Logs

Each session appends to `chat.YYYYMMDD_HHMMSS.log.txt` in the script directory. Only **user** and **agent** messages (plus a few `SYSTEM` events) are recorded — tool I/O is excluded to keep logs readable. On startup, all but the 3 newest log files are deleted.

Example entry:

```
[13:57:58] USER: which tools do you have
[13:58:00] AGENT: I have the following tools available to me: ...
```

---

## ⌨️ Controls

| Key | Action |
|---|---|
| Type `quit` / `exit` / `bye` | Exit cleanly (logs closed). |
| `Ctrl+C` | Force exit. |
| `Ctrl+A` | **During a tool loop** — interrupt and optionally inject a follow-up message. |

---

## 📁 Project Structure

```
microtest/
├── micro.py                          # The entire agent (~500 LOC)
├── README.md                         # This file
└── chat.YYYYMMDD_HHMMSS.log.txt      # Rolling chat logs (auto-managed)
```

---

## 🔒 Security Notes

- The shell tool runs **anything** the model asks for — `rm -rf`, `curl | sh`, etc. Run in a container or VM for untrusted prompts.
- API keys are committed in plaintext at the top of `micro.py`. Move them to env vars or a secrets manager before publishing.
- Vision results may contain descriptions of sensitive on-screen content.
