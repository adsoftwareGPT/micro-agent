#!/usr/bin/env python3

import json, os, subprocess, sys
import requests

# ── Config: load .env from the script's folder (same simple parser as micro.py)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_env_path):
    for _line in open(_env_path):
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

API_KEY  = os.environ.get("DEEPSEEK_KEY", "")
BASE_URL = os.environ.get("DEEPSEEK_URL", "https://api.deepseek.com")
MODEL    = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")  # the only model the beta supports
ENDPOINT = BASE_URL.rstrip("/") + "/responses"

# ── Tool def: Responses-API shape (function flattened to top level) ────────
SHELL_TOOL = {
    "type": "function",
    "name": "shell",
    "description": "Run a shell command on this Linux machine. Returns stdout+stderr.",
    "parameters": {
        "type": "object",
        "properties": {"cmd": {"type": "string", "description": "The command to run"}},
        "required": ["cmd"],
    },
}

def run_shell(cmd: str) -> str:
    """Execute one command, return combined output (capped, like micro.py)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        return (r.stdout + r.stderr)[:4000] or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after 60s: {cmd[:200]}"

def call_responses(input_items, instructions):
    """POST /responses and return the parsed JSON."""
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": MODEL,
        "input": input_items,
        "instructions": instructions,
        "tools": [SHELL_TOOL],
        "tool_choice": "auto",
        # Disable thinking mode so the model doesn't emit `reasoning` items.
        # When thinking is on, those items MUST be echoed back on every later
        # request or the API errors with 400:
        #   "The `reasoning_text` in the thinking mode must be passed back..."
        "thinking": {"type": "disabled"},
    }
    resp = requests.post(ENDPOINT, headers=headers, json=body,
                         timeout=(10, 120))  # (connect, read)
    resp.raise_for_status()
    return resp.json()

# ── One conversation turn: handle any chain of tool calls ───────────────────
def turn(history, instructions):
    """`history` is the full input-item list, mutated in place.

    The Responses API is stateless, so we resend the entire history each call.
    We loop until the model returns a message with NO function_call — that's
    the final answer.
    """
    while True:
        out = call_responses(history, instructions)["output"]

        text_parts, calls = [], []
        for item in out:
            t = item.get("type")
            if t == "message":
                # keep message items for continuity; extract text for display
                history.append(item)
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        text_parts.append(part.get("text", ""))
            elif t == "function_call":
                calls.append(item)
                history.append(item)   # call goes back in `input` for context
            elif t == "reasoning":
                # Thinking mode is disabled above, so this shouldn't happen —
                # but if the API ever returns reasoning, echo it back to avoid
                # the "reasoning_text must be passed back" 400 error.
                history.append(item)

        if text_parts:
            print(f"\n\033[1;32mAssistant:\033[0m {''.join(text_parts)}")

        if not calls:
            return  # no tool calls → model is done answering

        # Execute each tool call and append its output to history.
        # (The function_call item itself was already appended above.)
        for call in calls:
            name = call["name"]
            raw  = call.get("arguments", "{}")
            try:
                args = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                args = {}
            print(f"  \033[1;33m⚙ {name}\033[0m: {str(args)[:120]}", file=sys.stderr)
            result = run_shell(**args) if name == "shell" else f"unknown tool: {name}"
            print(f"     \033[2m→ {result[:200]}\033[0m", file=sys.stderr)
            history.append({
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": result,
            })
        # loop: re-send history with the tool outputs; model continues

# ── Main REPL ───────────────────────────────────────────────────────────────
def main():
    if not API_KEY:
        print("ERROR: DEEPSEEK_KEY not set. Add it to .env (next to this script) "
              "or `export DEEPSEEK_KEY=...`.", file=sys.stderr)
        sys.exit(1)

    print(f"DeepSeek Responses API (beta)  |  model={MODEL}")
    print(f"endpoint={ENDPOINT}")
    print("Type 'quit' to exit. Ask it to run commands (e.g. 'list my files').\n")

    instructions = (
        "You are a concise terminal assistant on a Linux machine. "
        "Use the `shell` tool to run commands when the user asks for action, "
        "then summarize the result in one or two lines."
    )
    history = []  # list of input items (persists for the whole session)

    while True:
        try:
            inp = input("\033[1;31mYou: \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not inp:
            continue
        if inp.lower() in ("quit", "exit", "bye"):
            print("Bye!")
            break
        history.append({"role": "user", "content": inp})
        try:
            turn(history, instructions)
        except requests.HTTPError as e:
            body = e.response.text[:500] if e.response is not None else "?"
            print(f"\n⚠ API error {e.response.status_code}: {body}", file=sys.stderr)
        except Exception as e:
            print(f"\n⚠ {type(e).__name__}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()