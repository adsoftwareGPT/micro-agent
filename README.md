# micro-agent

A small, self-contained **terminal AI agent**. You chat with it in your shell; it
can think, run commands on your machine, and drive a **real Chromium browser** to
read pages, click buttons, fill forms, log in, and scrape content — including
sites that require a logged-in session.

It works with several LLM providers (Z.ai/GLM, DeepSeek, Mistral, OpenRouter,
OpenCode, local Ollama) and ships with a tiny optional browser-based chat UI.

> **In one sentence:** point micro at a problem in your terminal, and it will
> use shell + a real browser + web search + vision to solve it for you.

---

## What can it do?

micro gives the model a small set of tools and then steps back. Whatever you can
do with these tools, the agent can do for you:

- **`shell`** — run any terminal command on your machine.
- **`browser_action`** — drive Chromium: navigate, click, type, scroll, run
  JavaScript, take screenshots, log in with Google, and dismiss cookie banners.
- **`fetch_webpage`** — fetch the readable text of a JS-rendered page.
- **`ddg_search`** — search the web via DuckDuckGo (optional dependency).
- **`vision`** — analyze a desktop screenshot or image file with a vision model.

Because the browser is a **real, persistent Chromium** (not a fresh incognito
instance every time), you can log in once and the agent keeps using that
logged-in session. See [Scraping with logged-in sessions](#scraping-with-logged-in-sessions)
below.

---

## Requirements

- **Python ≥ 3.10**
- **Chromium / Google Chrome** (for the browser tools; micro can install it
  automatically on first use)
- An **API key** for at least one LLM provider

---

## Quick start (run it from the terminal)

You don't need to install anything globally — the fastest way to try micro is to
just run the script directly:

```bash
# 1. From the project folder, copy the example config and add your API key
cp .env.example .env
#   then edit .env and put your key in, e.g.:
#       PROVIDER=zai
#       ZAI_KEY=your-key-here

# 2. Install the Python dependencies (requests + pillow)
pip install requests pillow

# 3. Start the agent — that's it
python3 micro.py
```

You'll get a `You:` prompt. Type a request, press Enter, and watch the agent
work. Type `quit` (or press `Ctrl+C`) to exit.

### Pick a provider from the command line

The provider in `.env` is the default, but you can override it per run with a
flag — one for each provider:

```bash
python3 micro.py               # uses PROVIDER from .env (default: deepseek)
python3 micro.py -zai          # use Z.ai / GLM
python3 micro.py -mistral      # use Mistral
python3 micro.py -deepseek     # use DeepSeek
python3 micro.py -openrouter   # use OpenRouter
python3 micro.py -opencode     # use OpenCode free tier
python3 micro.py -ollama       # use a local Ollama server
```

### Optional: install as a command

If you'd rather have a `micro-agent` command on your `PATH`:

```bash
pip install .                   # gives you the `micro-agent` command
micro-agent -zai                # run from anywhere

# or build a Debian package:
./build-deb.sh
```

### Optional: web UI

The same agent also runs in a simple browser-based chat UI (Python standard
library only — no extra dependencies):

```bash
python3 web.py                  # serves on http://localhost:5555
python3 web.py --port 8080      # custom port
```

---

## Configure

Configuration lives in **`.env`**, in the same folder as `micro.py`. micro loads
it automatically — you never need to `source` it.

```ini
# Pick the provider to use by default
PROVIDER=zai            # zai | deepseek | openrouter | opencode | ollama | mistral

# API key for each provider (fill in the one(s) you use)
ZAI_KEY=
DEEPSEEK_KEY=
OPENROUTER_KEY=
OPENCODE_KEY=
MISTRAL_KEY=

# Optional per-provider overrides (defaults shown)
# ZAI_MODEL=glm-5.2
# DEEPSEEK_MODEL=deepseek-v4-flash
# OPENROUTER_MODEL=xiaomi/mimo-v2.5
# OPENCODE_MODEL=deepseek-v4-flash-free
# MISTRAL_MODEL=mistral-small-latest
# VISION_MODEL=qwen/qwen2.5-vl-72b-instruct   # used by the `vision` tool
```

Want to keep `.env` somewhere else? Point micro at a different config folder:

```bash
MAGENT_CONFIG_DIR=/path/to/config python3 micro.py
```

### Available providers

| Provider     | Default model            | Key env var       | Notes                                                                  |
| ------------ | ------------------------ | ----------------- | ---------------------------------------------------------------------- |
| `zai`        | `glm-5.2`                | `ZAI_KEY`         | Z.ai / GLM; supports the `thinking` field                              |
| `mistral`    | `mistral-small-latest`   | `MISTRAL_KEY`     | Strict API — returns 422 on unknown top-level fields (e.g. `thinking`) |
| `deepseek`   | `deepseek-v4-flash`      | `DEEPSEEK_KEY`    | Default if `PROVIDER` is unset                                         |
| `openrouter` | `xiaomi/mimo-v2.5`       | `OPENROUTER_KEY`  | Also powers the `vision` tool                                          |
| `opencode`   | `deepseek-v4-flash-free` | `OPENCODE_KEY`    | Free tier via opencode.ai                                              |
| `ollama`     | `glm-5.2:cloud`          | — (local)         | Local Ollama at `http://localhost:11434`                               |

Every provider can be overridden individually with `<NAME>_URL`, `<NAME>_MODEL`,
and `<NAME>_KEY`. Only `<NAME>_SUPPORTS_THINKING=1` opts a provider into the
Z.ai/GLM `thinking` field (default off for everyone except `zai`).

---

## Browser & scraping

micro drives a **real Chromium** over the Chrome DevTools Protocol (CDP), on
`localhost:9222`. On first use, micro looks for an installed Chromium and, if
none is found, will try to install one for you (snap / apt / dnf / yum / pacman /
apk). You can also force a check/install:

```bash
python3 cdp_fetch.py --install-chromium
```

The browser tools are exposed to the model as the `browser_action` tool, with
actions: **`navigate`**, **`click`**, **`type`**, **`scroll`**, **`eval`** (run
JS), **`wait`**, **`screenshot`**, **`get_state`**, **`login_google`**, and
**`accept_cookies`**.

### Scraping with logged-in sessions

This is the headline feature for scraping: **because Chromium is persistent, the
agent can use your logged-in sessions.** Log in once — by hand or via the
`login_google` action — and from then on the agent can navigate, read, and scrape
**anything your account can see**, across requests and across sessions. There is
no need to re-authenticate, paste tokens, or fight login flows every run.

In practice this means:

- **Private/protected content is in scope.** Logged-in dashboards, your inbox,
  paid news sites, internal tools, social feeds behind login — if *you* can see
  it in the browser, the agent can read it.
- **One login lasts.** Cookies are stored in the Chromium profile and survive
  service restarts, so a single login keeps working for hours, days, or longer.
- **Google login is automated.** Point the agent at a "Continue with Google"
  site and it can complete the full Google OAuth flow itself (FedCM + popup +
  redirect) via the `login_google` action — just be in front of the screen the
  first time so you can approve any 2FA / device prompts.
- **Aggressive pop-ups handled.** Call `accept_cookies` right after `navigate`
  to dismiss consent banners (English + German, common consent SDKs) so they
  don't block scraping.

So: tell the agent where to log in once, and after that it can scrape just about
everything you have access to. Useful for personal data exports, monitoring
private dashboards, gathering research from paywalled sources you subscribe to,
etc.

> **Heads-up:** you are responsible for respecting the terms of service of the
> sites you access and for only scraping data you're allowed to. micro gives you
> the capability; the judgement is yours.

---

## Privacy & safety

- micro runs **locally** as a normal process. It only talks to the LLM provider
  you configured and to the websites you ask it to touch.
- All tool output is treated as **untrusted data**: micro is hardened against
  prompt injection, so a malicious webpage or command output can't quietly
  change its instructions or delete your files. Suspicious directives inside
  tool output are surfaced to you rather than acted on.
- Chat history is logged to `history/chat.YYYYMMDD_HHMMSS.log.txt` next to
  `micro.py` (up to 50 rolling files). The agent itself can read prior sessions
  so it has context across runs.

---

## Project layout

| File                  | Purpose                                              |
| --------------------- | ---------------------------------------------------- |
| `micro.py`            | Main agent loop, tools, and provider plumbing        |
| `web.py`              | Optional browser-based chat UI (port 5555)           |
| `browser_action.py`   | CDP browser primitives (click, type, login, ...)     |
| `cdp_fetch.py`        | JS-rendered webpage fetcher + Chromium launcher      |
| `build-deb.sh`        | Builds a Debian `.deb` package                       |
| `.env.example`        | Template for your `.env`                             |
| `.env`                | Your actual keys/config (gitignored, never shipped)  |

---

## License

MIT
