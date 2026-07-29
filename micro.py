#!/usr/bin/env python3

import argparse, json, os, re, select, subprocess, sys, termios, time, random
from io import BytesIO
from typing import Optional
import requests
from PIL import Image

# ── Load .env (so keys stay out of source but local runs "just work") ───────
# python-dotenv if available, otherwise a minimal fallback parser.
# Search order (first match wins): $MAGENT_CONFIG_DIR →
# $XDG_CONFIG_HOME/micro-agent → ~/.config/micro-agent →
# <script dir> (fallback for source-tree / git-checkout runs).
def _candidate_env_dirs():
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = []
    override = os.environ.get("MAGENT_CONFIG_DIR")
    if override:
        dirs.append(os.path.expanduser(override))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        dirs.append(os.path.join(os.path.expanduser(xdg), "micro-agent"))
    if home := os.path.expanduser("~"):
        dirs.append(os.path.join(home, ".config", "micro-agent"))
    dirs.append(here)  # always last: alongside script (legacy / dev / deb fallback)
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d); out.append(d)
    return out

def _find_resource(name: str):
    """Return first existing path to `name` across candidate dirs, else None."""
    for d in _candidate_env_dirs():
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None

def _desired_resource(name: str):
    """Where a NEW resource should be written (first writable user dir, else script dir)."""
    for d in _candidate_env_dirs():
        try:
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, name)
        except OSError:
            continue
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)

def _load_dotenv_anywhere():
    """Load .env from the first candidate dir that has one.

    Also ensures XDG user dir exists for first-time `micro-agent` runs.
    """
    p = _find_resource(".env")
    if not p:
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(p)
    except ImportError:
        with open(p) as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

_load_dotenv_anywhere()

# ── Config ──────────────────────────────────────────────────────────────────
# Set PROVIDER to "zai" or "deepseek" or "openrouter" or "opencode" or "ollama"
# Override via the PROVIDER=... line in your .env or the CLI flag (-zai, -ollama, …).
PROVIDER = os.environ.get("PROVIDER", "zai")

ZAI_KEY = os.environ.get("ZAI_KEY", "")
ZAI_MODEL = os.environ.get("ZAI_MODEL", "glm-5.2")
ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "xiaomi/mimo-v2.5")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

OPENCODE_KEY = os.environ.get("OPENCODE_KEY", "")
OPENCODE_MODEL = os.environ.get("OPENCODE_MODEL", "deepseek-v4-flash-free")
OPENCODE_URL = "https://opencode.ai/zen/v1/chat/completions"

OLLAMA_KEY = os.environ.get("OLLAMA_KEY", "")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "glm-5.2:cloud")
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"

MAX_TOKENS = 38192
TOOL_OUTPUT_MAX_CHARS = 350000
MAX_TOOL_LOOPS = 2500
MAX_API_RETRIES = 2
API_CONNECT_TIMEOUT = 30
API_READ_TIMEOUT = 120
SHELL_TIMEOUT = 120

VISION_API_URL = "https://openrouter.ai/api/v1/chat/completions"
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen/qwen2.5-vl-72b-instruct")
VISION_MAX_WIDTH = 1024
VISION_JPEG_QUALITY = 75
VISION_TIMEOUT = 120

THINKING_ENABLED = False
BREAK_KEY_ENABLED = True
_break_requested = False
_http_session: Optional[requests.Session] = None

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

SYSTEM_PROMPT = "- You are an AI expert having full linux at hand\n- current date: {current_date}\n- Chat history (user & agent messages) is logged to chat.*.log.txt in the project dir (format: chat.YYYYMMDD_HHMMSS.log.txt, newest by filename sort)."

# ── Chat Logger ─────────────────────────────────────────────────────────────
# Write logs to the first writable user dir (XDG), falling back to the script
# directory for source-tree / dev runs. Prevents permission errors when
# installed via .deb to /usr/share/micro-agent (read-only for normal users).
LOG_DIR = os.path.dirname(_desired_resource("chat.log"))
LOG_BASE = os.path.join(LOG_DIR, "chat")
LOG_KEEP = 3  # keep up to 3 timestamped log files

def _ts_path(suffix: str = "") -> str:
    """Generate a chat log path with timestamp, e.g. chat.20260718_193713.txt"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    base = f"{LOG_BASE}.{ts}"
    return base if not suffix else f"{base}.{suffix}.txt"

class ChatLogger:
    """Logs user and agent messages (not tool outputs) to timestamped rolling files."""
    def __init__(self):
        self._file = None
        self._path = None
        self._clean_old_logs()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._path = _ts_path("log")
        self._file = open(self._path, "a", encoding="utf-8")
        self._write("=" * 60)
        self._write(f"Chat started at {ts}")
        self._write("=" * 60)

    def _clean_old_logs(self):
        """Remove excess old logs, keep only LOG_KEEP most recent."""
        import glob
        pattern = os.path.join(LOG_DIR, "chat.*.log.txt")
        files = sorted(glob.glob(pattern))
        while len(files) > LOG_KEEP:
            oldest = files.pop(0)
            try:
                os.remove(oldest)
            except OSError:
                pass

    def _write(self, text: str):
        if self._file and not self._file.closed:
            try:
                self._file.write(text + "\n")
                self._file.flush()
            except Exception:
                pass

    def log_user(self, content: str):
        ts = time.strftime("%H:%M:%S")
        display = content[:2000] + ("..." if len(content) > 2000 else "")
        for line in display.split("\n"):
            self._write(f"[{ts}] USER: {line}")

    def log_agent(self, content: str):
        ts = time.strftime("%H:%M:%S")
        for line in content.split("\n"):
            self._write(f"[{ts}] AGENT: {line}")

    def log_system(self, content: str):
        ts = time.strftime("%H:%M:%S")
        self._write(f"[{ts}] SYSTEM: {content}")

    def close(self):
        if self._file and not self._file.closed:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self._write(f"--- Chat ended at {ts} ---")
            self._file.close()

_chat_logger: Optional[ChatLogger] = None
_chat_logger: Optional[ChatLogger] = None
def get_logger() -> ChatLogger:
    global _chat_logger
    if _chat_logger is None:
        _chat_logger = ChatLogger()
    return _chat_logger

# ── HTTP Session ────────────────────────────────────────────────────────────
def _get_session() -> requests.Session:
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        _http_session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br", "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1", "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
        })
    _http_session.headers.update({"User-Agent": random.choice(_USER_AGENTS)})
    return _http_session

# ── Tool Definitions ────────────────────────────────────────────────────────
TOOLS = [
    {"type": "function", "function": {
        "name": "shell", "description": "Run any shell command",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {
        "name": "vision",
        "description": "Analyze what is visible on screen by taking a screenshot, compressing it to ~67KB JPEG, and sending it to a vision AI on OpenRouter. Returns a text description of windows, applications, UI elements, and any visible content.",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "Optional specific question about what to look for"},
            "path": {"type": "string", "description": "Optional path to an existing image file"},
            "delay": {"type": "integer", "description": "Optional delay in seconds before capturing (default: 0)"}
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "ddg_search",
        "description": "Search DuckDuckGo from the terminal. Returns up to 30 results with titles, URLs, and snippets. Use when you need to find information, documentation, tutorials, or any web content.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "The search query"}
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_webpage",
        "description": "Fetch a webpage and return its content as plain text. Uses headless Chromium to render JavaScript, so it works on dynamic sites (SPAs, React, Vue). Strips HTML tags and extracts readable text. Handles JS-rendered content that plain HTTP cannot. Use when you need the full content of a web page.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "The full URL to fetch, including scheme (e.g. https://example.com/page)"}
        }, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "browser_action",
        "description": "Control the browser interactively: click elements, type text, navigate, scroll, take screenshots, wait for conditions, check browser state, and LOG IN to websites using Google OAuth. Actions: navigate, click, type, scroll, wait, screenshot, get_state, login_google. The login_google action handles the full Google Identity Services flow automatically (FedCM + popup + redirect).",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["navigate", "click", "type", "scroll", "wait", "screenshot", "get_state", "login_google"],
                       "description": "The browser action to perform"},
            "url": {"type": "string", "description": "URL for navigate/login_google actions"},
            "selector": {"type": "string", "description": "CSS selector for click/type actions"},
            "text": {"type": "string", "description": "Text to match for click action, or text to type for type action"},
            "x": {"type": "integer", "description": "X coordinate for click action"},
            "y": {"type": "integer", "description": "Y coordinate for click action"},
            "email": {"type": "string", "description": "Google account email for login_google (optional, auto-selects first if omitted)"},
            "login_url": {"type": "string", "description": "Direct login URL for login_google (optional, defaults to url+/login)"},
            "btn_text": {"type": "string", "description": "Text of the Google login button (default: 'Continue with Google')"},
            "seconds": {"type": "integer", "description": "Seconds to wait (for wait action)"},
            "url_contains": {"type": "string", "description": "Wait until URL contains this string"},
            "text_contains": {"type": "string", "description": "Wait until page text contains this string"},
            "path": {"type": "string", "description": "File path for screenshot (default: /tmp/browser_screenshot.jpg)"},
            "wait": {"type": "integer", "description": "Wait time in seconds (for navigate/login_google)"},
            "iframe_selector": {"type": "string", "description": "CSS selector for iframe to click (for GIS buttons)"}
        }, "required": ["action"]}}},
]

# ── Provider Registry ──────────────────────────────────────────────────────
PROVIDERS = {
    "zai": {"url": ZAI_URL, "key": ZAI_KEY, "model": ZAI_MODEL},
    "deepseek": {"url": DEEPSEEK_URL, "key": DEEPSEEK_KEY, "model": DEEPSEEK_MODEL},
    "openrouter": {"url": OPENROUTER_URL, "key": OPENROUTER_KEY, "model": OPENROUTER_MODEL},
    "opencode": {"url": OPENCODE_URL, "key": OPENCODE_KEY, "model": OPENCODE_MODEL},
    "ollama": {"url": OLLAMA_URL, "key": OLLAMA_KEY, "model": OLLAMA_MODEL},
}

def get_provider():
    """Return the active provider config dict based on PROVIDER."""
    p = PROVIDERS.get(PROVIDER)
    if not p:
        raise ValueError(f"Unknown PROVIDER '{PROVIDER}'. Choose from: {', '.join(PROVIDERS)}")
    return p

# ── LLM Call ────────────────────────────────────────────────────────────────
def call_llm(messages, tools=None, tool_choice="auto", model=None, api_key=None):
    p = get_provider()
    url = p["url"]
    if model is None:
        model = p["model"]
    if api_key is None:
        api_key = p["key"]
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    data = {"model": model, "messages": messages, "tools": tools,
            "tool_choice": tool_choice, "max_tokens": MAX_TOKENS}
    data["thinking"] = {"type": "enabled" if THINKING_ENABLED else "disabled"}
    last_error = None
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers,
                                 json=data, timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT))
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
            if attempt < MAX_API_RETRIES:
                print(f"  ⏳ API retry {attempt}/{MAX_API_RETRIES}...", file=sys.stderr)
                time.sleep(2 ** attempt)
    raise last_error or RuntimeError("API call failed")

# ── Shell Tool ──────────────────────────────────────────────────────────────
def execute_shell(cmd: str) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=SHELL_TIMEOUT)
        return (r.stdout + r.stderr)[:TOOL_OUTPUT_MAX_CHARS]
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {SHELL_TIMEOUT}s: {cmd[:200]}"
    except Exception as e:
        return f"Error: {e}"

# ── Vision Tool ─────────────────────────────────────────────────────────────
def execute_vision(prompt: str = '', path: str = '', delay: int = 0) -> str:
    # Reuse the OpenRouter key from config (set via OPENROUTER_KEY env var).
    api_key = OPENROUTER_KEY

    if not path:
        ts = int(time.time())
        path = f'/tmp/vision_screenshot_{ts}.png'
        delay_args = ['-d', str(delay)] if delay > 0 else []
        try:
            subprocess.run(['gnome-screenshot', '-f', path] + delay_args, capture_output=True, timeout=15)
        except BaseException:
            try:
                subprocess.run(['scrot'] + (['-d', str(delay)] if delay else []) + [path], capture_output=True, timeout=15)
            except BaseException:
                return "Error: could not take screenshot"
        for _ in range(20):
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                break
            time.sleep(0.1)
        if not os.path.exists(path) or os.path.getsize(path) < 1000:
            return "Error: screenshot file missing or too small"

    try:
        img = Image.open(path).convert("RGB")
    except Exception as e:
        return f"Error opening image: {e}"
    orig_w, orig_h = img.size
    if orig_w > VISION_MAX_WIDTH:
        ratio = VISION_MAX_WIDTH / orig_w
        img = img.resize((VISION_MAX_WIDTH, int(orig_h * ratio)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=VISION_JPEG_QUALITY)
    img_bytes, img_w, img_h = buf.getvalue(), *img.size
    img_kb = len(img_bytes) / 1024

    prompt = prompt or "Describe what you see on this desktop screenshot. List any visible windows, applications, and key UI elements."
    b64 = __import__('base64').b64encode(img_bytes).decode()
    payload = {"model": VISION_MODEL, "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
    ]}], "max_tokens": 500}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://github.com/adsoftware", "X-Title": "Micro Vision"}
    try:
        resp = requests.post(VISION_API_URL, json=payload, headers=headers, timeout=VISION_TIMEOUT)
        if resp.status_code != 200:
            return f"Vision API error {resp.status_code}: {resp.text[:300]}"
        rj = resp.json()
        content = rj["choices"][0]["message"]["content"]
        usage = rj.get("usage", {})
        us = f" [tokens: {usage.get('total_tokens', '?')}, cost: ${usage.get('cost', 0):.6f}]" if usage else ""
        return f"--- Vision Result{us} ---\nImage: {orig_w}x{orig_h} -> {img_w}x{img_h} ({img_kb:.0f} KB)\nPrompt: {prompt[:100]}\n\n{content}\n--------------------------"
    except requests.exceptions.Timeout:
        return f"Vision API timed out after {VISION_TIMEOUT}s"
    except Exception as e:
        return f"Vision API error: {e}"

# ── Fetch Webpage Tool ─────────────────────────────────────────────────────
def execute_fetch_webpage(url: str) -> str:
    """Fetch a URL using Chromium CDP (JS rendering) with HTTP fallback."""
    _mod_dir = os.path.dirname(os.path.abspath(__file__))
    if _mod_dir not in sys.path:
        sys.path.insert(0, _mod_dir)
    try:
        from cdp_fetch import fetch_webpage as _cdp_fetch
        return _cdp_fetch(url)[:TOOL_OUTPUT_MAX_CHARS]
    except ImportError:
        return _legacy_http_fetch(url)[:TOOL_OUTPUT_MAX_CHARS]


def _legacy_http_fetch(url: str) -> str:
    """Fallback: plain HTTP fetch with HTML stripping (no JS rendering)."""
    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self._skip = False

        def handle_starttag(self, tag, attrs):
            if tag.lower() in ('script', 'style'):
                self._skip = True

        def handle_endtag(self, tag):
            if tag.lower() in ('script', 'style'):
                self._skip = False

        def handle_data(self, data):
            if not self._skip:
                t = data.strip()
                if t:
                    self.parts.append(t)

        def get_text(self):
            return '\n'.join(self.parts)

    try:
        resp = _get_session().get(url, timeout=30)
        resp.raise_for_status()
        ct = resp.headers.get('content-type', '').lower()
        if 'html' in ct:
            ext = _TextExtractor()
            ext.feed(resp.text)
            text = ext.get_text()
        else:
            text = resp.text
        return text
    except Exception as e:
        return f"Error fetching webpage: {e}"


# ── DuckDuckGo Search Tool ──────────────────────────────────────────────────
def execute_ddg_search(query: str) -> str:
    """Search DuckDuckGo using the ddgs library, returns up to 30 results."""
    try:
        # Optional: use a sibling project's venv if present; otherwise rely on the
        # current interpreter's installed packages.
        _alt_site = os.path.join(os.path.expanduser('~'), 'browser-robot-portable',
                                 '.venv', 'lib', 'python3.12', 'site-packages')
        if os.path.isdir(_alt_site):
            sys.path.insert(0, _alt_site)
        from ddgs import DDGS
    except ImportError:
        return "Error: ddgs library not found. Install with: pip install ddgs"
    try:
        results = DDGS().text(query, max_results=30)
    except Exception as e:
        return f"Error searching DuckDuckGo: {e}"
    if not results:
        return "No results found."
    out = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "").strip()
        href = r.get("href", "").strip()
        body = r.get("body", "").strip()
        if body:
            body = body[:120] + ("..." if len(body) > 120 else "")
            out.append(f"{i:2d}. {title}\n    {href}\n    {body}")
        else:
            out.append(f"{i:2d}. {title}\n    {href}")
    return "\n\n".join(out)



# ── Browser Action Tool ────────────────────────────────────────────────────
def execute_browser_action(action: str, **kwargs) -> str:
    """Interactive browser control via CDP (click, type, login, etc.)."""
    _mod_dir = os.path.dirname(os.path.abspath(__file__))
    if _mod_dir not in sys.path:
        sys.path.insert(0, _mod_dir)
    try:
        from browser_action import browser_action as _ba
        result = _ba(action, **kwargs)
        return result[:TOOL_OUTPUT_MAX_CHARS]
    except ImportError:
        return "Error: browser_action module not found"
    except Exception as e:
        return f"Error: {e}"


# ── Executor Registry ───────────────────────────────────────────────────────
TOOL_EXECUTORS = {
    "shell": execute_shell,
    "vision": execute_vision,
    "fetch_webpage": execute_fetch_webpage,
    "browser_action": execute_browser_action,
    "ddg_search": execute_ddg_search,
}

# ── Tool Call Handler ───────────────────────────────────────────────────────
def summarize(name: str, args: dict) -> str:
    key = {"shell": "cmd", "vision": "prompt", "fetch_webpage": "url", "ddg_search": "query", "browser_action": "action"}.get(name, "")
    raw = args.get(key, json.dumps(args, ensure_ascii=True)) if key else json.dumps(args, ensure_ascii=True)
    s = " ".join(raw.split())
    return s[:100] + "..." if len(s) > 100 else s or "(no details)"

def handle_tool_calls(tool_calls: list, messages: list) -> list:
    for tc in tool_calls:
        name, raw = tc["function"]["name"], tc["function"]["arguments"]
        try:
            args = json.loads(raw, strict=False)
        except json.JSONDecodeError:
            args = {}
            for fix in [lambda s: s.replace('\n','\\n'), lambda s: s + '"' if s.count('"')%2 else s]:
                try: args = json.loads(fix(raw), strict=False); break
                except: pass
            if not args:
                m = re.search(r'"cmd"\s*:\s*"', raw)
                if name == "shell" and m:
                    p, p_end = m.end(), m.end()
                    while p_end < len(raw) and not (raw[p_end]=='"' and (p_end==0 or raw[p_end-1]!='\\')): p_end+=1
                    args = {"cmd": raw[p:p_end]}
        print(f"  Step: {name}: {summarize(name, args)}")
        fn = TOOL_EXECUTORS.get(name)
        output = fn(**args) if fn else f"Unknown tool: {name}"
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": output})
    return messages

# ── Break Key ───────────────────────────────────────────────────────────────
def _check_break_key() -> bool:
    global _break_requested
    if not BREAK_KEY_ENABLED or _break_requested or not os.isatty(sys.stdin.fileno()):
        return _break_requested
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except (termios.error, OSError):
        return False
    try:
        new = list(old)
        new[3] = new[3] & ~(termios.ICANON | termios.ECHO | termios.ISIG)
        new[6][termios.VMIN] = 0
        new[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        was_blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
        try:
            ready, _, _ = select.select([fd], [], [], 0)
            if ready and b'\x01' in os.read(fd, 4096):
                _break_requested = True
                while os.read(fd, 4096): pass
        finally:
            os.set_blocking(fd, was_blocking)
    except (ValueError, BlockingIOError, OSError, termios.error):
        pass
    finally:
        try: termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except: pass
    return _break_requested

# ── Main Loop ───────────────────────────────────────────────────────────────
def _loop(messages: list):
    global _break_requested
    tool_loop_count = 0
    logger = get_logger()
    while True:
        tool_loop_count += 1
        tc = "none" if tool_loop_count > MAX_TOOL_LOOPS else "auto"
        response = call_llm(messages, TOOLS, tool_choice=tc)
        choice = response["choices"][0]["message"]
        messages.append(choice)

        if choice.get("tool_calls"):
            text = choice.get("content") or ""
            if text.strip():
                print(f"\nAgent step: {text.strip()}")
                logger.log_agent(f"[thinking] {text.strip()}")
            messages = handle_tool_calls(choice["tool_calls"], messages)
            if _check_break_key(): break
            continue

        content = choice.get("content") or ""
        if content.strip():
            #print("  ✅ AI response received", file=sys.stderr)
            print("\nAgent:", content, "\n")
            logger.log_agent(content)
        break

def parse_args():
    """Parse CLI args. Provider flags like -zai / -ollama select the provider."""
    parser = argparse.ArgumentParser(
        description="micro - a tiny terminal coding agent",
        add_help=True,
    )
    # Add a -<provider> flag for each known provider (e.g. -zai, -ollama)
    for name in PROVIDERS:
        parser.add_argument(
            f"-{name}", action="store_const", const=name, dest="provider",
            help=f"use the {name} provider",
        )
    # Long form aliases for readability (-zai == --zai)
    for name in PROVIDERS:
        parser.add_argument(
            f"--{name}", action="store_const", const=name, dest="provider",
            help=argparse.SUPPRESS,
        )
    # Also allow explicit --provider <name>
    parser.add_argument(
        "--provider", dest="provider_name", choices=list(PROVIDERS),
        help="select provider by name (e.g. --provider ollama)",
    )
    args = parser.parse_args()
    return args


def main():
    global _break_requested, _chat_logger, PROVIDER

    # Parse CLI args and override PROVIDER if a flag was given
    args = parse_args()
    chosen = args.provider or args.provider_name
    if chosen and chosen in PROVIDERS:
        PROVIDER = chosen

    # Initialize logger (rotates logs on startup)
    logger = get_logger()

    mode = "thinking" if THINKING_ENABLED else "no-think"
    p = get_provider()
    #print(f"Agent ready. Type your message (or 'quit' to exit).")
    #print(f"  Press Ctrl+A during tool execution to interrupt.")
    #print(f"  Press Ctrl+C to exit.")
    print(f"  Provider: {PROVIDER} | Model: {p['model']} | Mode: {mode}")

    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(current_date=__import__("datetime").datetime.now().strftime("%A, %Y-%m-%d"))}]
    try:
        while True:
            try:
                inp = input("\033[1;31mYou: ").strip()
                print("\033[0m", end="")  # reset color after user input
                if inp.lower() in ('quit', 'exit', 'bye'):
                    print("Goodbye!")
                    logger.log_system("User exited")
                    break
                if not inp: continue
                messages.append({"role": "user", "content": inp})
                logger.log_user(inp)
                #print("  → Sending request to DeepSeek AI...", file=sys.stderr)
                _loop(messages)

                if _break_requested:
                    _break_requested = False
                    inject = input("\n  ✋ You interrupted. Inject your message (or press Enter): ").strip()
                    if inject:
                        messages.append({"role": "system", "content": "(User interrupted and injected this message)"})
                        messages.append({"role": "user", "content": inject})
                        logger.log_system("User interrupted and injected message")
                        logger.log_user(inject)
                        _loop(messages)
            except KeyboardInterrupt:
                print("\nGoodbye!")
                logger.log_system("User pressed Ctrl+C")
                break
            except requests.exceptions.Timeout:
                print("\n⚠️ DeepSeek API timed out. Try rephrasing.\n", file=sys.stderr)
                logger.log_system("DeepSeek API timed out")
            except Exception as e:
                print(f"\n⚠️ Error: {e}\n", file=sys.stderr)
                logger.log_system(f"Error: {e}")
                # Remove last user message to recover from bad state
                if len(messages) > 1 and messages[-1]["role"] == "user":
                    messages.pop()
    finally:
        logger.close()
        # Stop headless Chromium if we started it (frees ~380 MB)
        try:
            from cdp_fetch import shutdown_chromium
            shutdown_chromium()
        except Exception:
            pass


if __name__ == "__main__":
    main()
