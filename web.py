#!/usr/bin/env python3
"""Basic web UI for the micro agent — a ChatGPT/Gemini-style single page.

Runs on port 5555 by default. Uses only the Python standard library for the
HTTP layer (no new dependency). Reuses micro.py's agent core verbatim:
SYSTEM_PROMPT, TOOLS, TOOL_EXECUTORS, summarize, the tool-call arg parsing,
the untrusted-provenance banner, and the provider config.

The one new capability vs. the CLI is real token streaming via SSE
(Server-Sent Events): the LLM response is streamed with stream=True and
each delta is forwarded to the browser, which renders it incrementally —
exactly like the website/phone apps of the big providers.

Run:
    python3 web.py                  # default provider, port 5555
    python3 web.py --port 5555
    python3 web.py --provider ollama
"""

import argparse
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue, Empty

import requests

# Reuse the agent core — all pure functions/data, no terminal coupling except
# three print() calls inside handle_tool_calls/_loop which we bypass by
# inlining our own tool execution here so we control what the UI sees.
import micro
from micro import (
    SYSTEM_PROMPT,
    TOOLS,
    TOOL_EXECUTORS,
    summarize,
    _normalize_tool_args,
    _UNTRUSTED_PROVENANCE_BANNER,
    _UNTRUSTED_PROVENANCE_FOOTER,
    MAX_TOOL_LOOPS,
    MAX_TOKENS,
    THINKING_ENABLED,
    API_CONNECT_TIMEOUT,
    API_READ_TIMEOUT,
    get_provider,
)

HOST = "0.0.0.0"
DEFAULT_PORT = 5555


# ── Per-session state ───────────────────────────────────────────────────────
# Maps session_id -> {messages, subs, lock}. The CLI keeps `messages` as a
# local in main(); here we hoist one history list per browser session.
SESSIONS: dict[str, dict] = {}
_SESSION_LOCK = threading.Lock()


def _system_message() -> dict:
    import datetime

    return {
        "role": "system",
        "content": SYSTEM_PROMPT.format(
            current_date=datetime.datetime.now().strftime("%A, %Y-%m-%d"),
            history_dir=micro.LOG_DIR,
        ),
    }


def get_session(sid: str) -> dict:
    with _SESSION_LOCK:
        s = SESSIONS.get(sid)
        if s is None:
            s = {"messages": [_system_message()], "subs": [], "lock": threading.Lock()}
            SESSIONS[sid] = s
        return s


def reset_session(sid: str) -> dict:
    with _SESSION_LOCK:
        s = {"messages": [_system_message()], "subs": [], "lock": threading.Lock()}
        SESSIONS[sid] = s
        return s


def broadcast(sid: str, event: dict) -> None:
    """Push an event to every open SSE subscriber for this session."""
    s = get_session(sid)
    for q in list(s["subs"]):
        q.put(event)


# ── Streaming LLM call ──────────────────────────────────────────────────────
def call_llm_stream(messages, tools, tool_choice):
    """Stream an OpenAI-compatible chat completion, yielding each delta.

    Every provider in micro.PROVIDERS speaks the OpenAI SSE format
    (`data: {"choices":[{"delta":{...}}]}` ... `data: [DONE]`), so we parse
    that wire format once and yield raw delta dicts to the caller.
    """
    p = get_provider()
    headers = {"Authorization": f"Bearer {p['key']}", "Content-Type": "application/json"}
    body = {
        "model": p["model"],
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "max_tokens": MAX_TOKENS,
        "stream": True,
    }
    # The "thinking" field is a Z.ai/GLM extension; Mistral and other strict
    # providers 422 on unknown top-level fields. Send it only when supported.
    if p.get("supports_thinking"):
        body["thinking"] = {"type": "enabled" if THINKING_ENABLED else "disabled"}
    resp = requests.post(
        p["url"], headers=headers, json=body, stream=True,
        timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT),
    )
    resp.raise_for_status()
    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", "replace")
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = obj.get("choices") or []
        if not choices:
            continue
        yield choices[0].get("delta", {})


def _merge_tool_calls(acc, deltas):
    """Accumulate streamed tool_call deltas indexed by their `index` field.

    Streaming tool calls arrive as fragments: the first chunk for an index
    carries the `id` + `function.name`; subsequent chunks carry
    `function.arguments` substrings that must be concatenated.
    """
    for d in deltas:
        idx = d.get("index", 0)
        slot = acc.setdefault(idx, {"id": None, "name": "", "arguments": ""})
        if d.get("id"):
            slot["id"] = d["id"]
        fn = d.get("function") or {}
        if fn.get("name"):
            slot["name"] += fn["name"]
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]


def _finalize_tool_calls(acc):
    return [
        {
            "id": v["id"] or f"call_{i}",
            "type": "function",
            "function": {"name": v["name"], "arguments": v["arguments"]},
        }
        for i, v in sorted(acc.items())
    ]


def _parse_tool_args(name, raw):
    """Mirror micro.handle_tool_calls' robust arg parsing.

    Models occasionally emit slightly malformed JSON (unescaped newlines,
    a dangling quote). Try the lenient fixes micro uses before giving up.
    """
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        pass
    args = {}
    for fix in (lambda s: s.replace("\n", "\\n"), lambda s: s + '"' if s.count('"') % 2 else s):
        try:
            args = json.loads(fix(raw), strict=False)
            break
        except Exception:
            pass
    if not args:
        m = re.search(r'"(cmd|command)"\s*:\s*"', raw)
        if name == "shell" and m:
            p, pe = m.end(), m.end()
            while pe < len(raw) and not (raw[pe] == '"' and (pe == 0 or raw[pe - 1] != "\\")):
                pe += 1
            args = {m.group(1): raw[p:pe]}
    return _normalize_tool_args(name, args)


def run_tool(name, args, messages):
    """Execute one tool — same dispatch + provenance-wrapping as the CLI."""
    fn = TOOL_EXECUTORS.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return fn(**args)
    except TypeError as e:
        return f"Error: bad arguments for tool '{name}': {e} (received {summarize(name, args)})"
    except Exception as e:
        return f"Error: tool '{name}' failed: {e}"


# ── The agent turn (replaces micro._loop, but emits UI events) ──────────────
def run_agent_turn(messages):
    """Drive one full agent turn, yielding UI events.

    Mirrors micro._loop: it loops (up to MAX_TOOL_LOOPS) calling the model,
    executing any tool calls, until the model returns a plain message. The
    only difference from _loop is that instead of print() it yields events:
      {"type":"token",    "text":...}   — streamed assistant text delta
      {"type":"demote"}                 — prior tokens were "thinking", not final
      {"type":"step",     "name":..., "summary":..., "output":...}  — tool call
      {"type":"done"}                   — turn finished, re-enable the input
    """
    tool_loop_count = 0
    while True:
        tool_loop_count += 1
        tc = "none" if tool_loop_count > MAX_TOOL_LOOPS else "auto"

        content_parts = []
        tc_acc: dict = {}
        try:
            for delta in call_llm_stream(messages, TOOLS, tc):
                if delta.get("content"):
                    content_parts.append(delta["content"])
                    yield {"type": "token", "text": delta["content"]}
                if delta.get("tool_calls"):
                    _merge_tool_calls(tc_acc, delta["tool_calls"])
        except Exception as e:
            micro.get_logger().log_system(f"LLM call failed: {e}")
            yield {"type": "error", "message": f"LLM call failed: {e}"}
            return

        content = "".join(content_parts)
        tool_calls = _finalize_tool_calls(tc_acc) if tc_acc else None

        choice = {"role": "assistant", "content": content if content else None}
        if tool_calls:
            choice["tool_calls"] = tool_calls
        messages.append(choice)

        if tool_calls:
            # The tokens we just streamed were intermediate "thinking" text,
            # not the final answer — tell the UI to demote that bubble.
            if content.strip():
                micro.get_logger().log_agent(f"[thinking] {content.strip()}")
                yield {"type": "demote"}
            for tco in tool_calls:
                name = tco["function"]["name"]
                args = _parse_tool_args(name, tco["function"]["arguments"])
                yield {
                    "type": "step",
                    "name": name,
                    "summary": summarize(name, args),
                }
                output = run_tool(name, args, messages)
                micro.get_logger().log_system(f"tool: {name} — {summarize(name, args)}")
                wrapped = _UNTRUSTED_PROVENANCE_BANNER + output + _UNTRUSTED_PROVENANCE_FOOTER
                messages.append({"role": "tool", "tool_call_id": tco["id"], "content": wrapped})
            continue

        # No tool calls → final answer. (Tokens were already streamed.)
        if content.strip():
            micro.get_logger().log_agent(content)
        yield {"type": "done"}
        return


def start_turn(sid: str, user_text: str):
    """Spawn the background worker that drives one agent turn for a session."""
    s = get_session(sid)

    def worker():
        if not s["lock"].acquire(blocking=False):
            broadcast(sid, {"type": "error", "message": "A turn is already running."})
            broadcast(sid, {"type": "done"})
            return
        try:
            s["messages"].append({"role": "user", "content": user_text})
            micro.get_logger().log_user(user_text)
            for ev in run_agent_turn(s["messages"]):
                broadcast(sid, ev)
        except Exception as e:
            broadcast(sid, {"type": "error", "message": str(e)})
            # Defensive: drop a trailing failed user message so history stays sane.
            if len(s["messages"]) > 1 and s["messages"][-1].get("role") == "user":
                s["messages"].pop()
        finally:
            broadcast(sid, {"type": "done"})
            s["lock"].release()

    threading.Thread(target=worker, daemon=True).start()


# ── The page ────────────────────────────────────────────────────────────────
HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>micro agent</title>
<style>
  :root{
    --bg:#f7f7f8; --panel:#ffffff; --ink:#1f2328; --muted:#6b7280;
    --user:#eef4ff; --user-ink:#1c3b6e; --agent:#ffffff;
    --accent:#5b8def; --step:#f1f3f5; --step-ink:#475569;
    --thought:#faf7ee; --thought-ink:#8a7434; --thought-border:#ecdcb0;
    --code-bg:#0f172a; --code-ink:#e2e8f0; --border:#e5e7eb; --err:#b91c1c;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{
    background:var(--bg); color:var(--ink);
    font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,Roboto,Helvetica,Arial,sans-serif;
    display:flex; flex-direction:column;
  }
  header{
    display:flex; align-items:center; gap:12px;
    padding:12px 20px; background:var(--panel); border-bottom:1px solid var(--border);
    position:sticky; top:0; z-index:5;
  }
  .brand{font-weight:700; font-size:17px; letter-spacing:-.2px}
  .brand .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent);margin-right:7px;vertical-align:middle}
  #meta{color:var(--muted); font-size:12.5px; flex:1}
  button{font:inherit; cursor:pointer; border:1px solid var(--border); background:#fff; color:var(--ink);
         border-radius:8px; padding:6px 12px}
  button:hover{background:#f3f4f6}
  button:disabled{opacity:.5; cursor:not-allowed}

  .chat{flex:1; min-height:0; position:relative; display:flex; flex-direction:column}
  main{flex:1; overflow-y:auto; padding:24px 16px 8px}
  .wrap{max-width:768px; margin:0 auto; display:flex; flex-direction:column; gap:18px}
  #jmp{position:absolute; right:24px; bottom:16px; z-index:6; width:38px; height:38px;
       border-radius:50%; padding:0; display:none; box-shadow:0 2px 8px rgba(0,0,0,.18);
       font-size:18px; line-height:1; background:var(--panel)}
  #jmp.show{display:flex; align-items:center; justify-content:center}

  .msg{display:flex; flex-direction:column; gap:2px; animation:fade .18s ease}
  @keyframes fade{from{opacity:0; transform:translateY(4px)} to{opacity:1}}
  .who{font-size:12px; color:var(--muted); padding:0 4px}
  .bubble{
    padding:11px 15px; border-radius:14px; max-width:100%;
    word-wrap:break-word; overflow-wrap:anywhere;
  }
  .user{align-self:flex-end; align-items:flex-end; max-width:82%}
  .user .bubble{background:var(--user); color:var(--user-ink); border-bottom-right-radius:4px}
  .agent{align-self:stretch}
  .agent .bubble{background:var(--agent); border:1px solid var(--border); border-bottom-left-radius:4px}
  .agent.thoughts .bubble{background:var(--thought); color:var(--thought-ink); border:1px dashed var(--thought-border); font-size:13.5px}
  .agent.thoughts .who::after{content:" · thoughts"}

  .step{
    align-self:stretch; background:var(--step); border:1px solid var(--border);
    border-radius:10px; padding:8px 12px; font-size:12.5px; color:var(--step-ink);
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    display:flex; align-items:center; gap:8px;
  }
  .step .arr{color:var(--accent); font-weight:700}
  .step .nm{font-weight:700; color:#334155}
  .step details{margin-left:auto; max-width:70%}
  .step summary{cursor:pointer; color:var(--muted); font-family:inherit; list-style:none}
  .step summary::-webkit-details-marker{display:none}
  .step summary::after{content:" ▾ output"}
  .step details[open] summary::after{content:" ▴ hide"}
  .step pre{margin:8px 0 0; white-space:pre-wrap; word-break:break-word; max-height:280px; overflow:auto;
            background:#fff; border:1px solid var(--border); border-radius:6px; padding:8px; color:#334155}

  .bubble p{margin:.35em 0}
  .bubble p:first-child{margin-top:0}
  .bubble p:last-child{margin-bottom:0}
  .bubble h1,.bubble h2,.bubble h3{margin:.6em 0 .3em; line-height:1.25}
  .bubble h1{font-size:1.3em} .bubble h2{font-size:1.15em} .bubble h3{font-size:1.04em}
  .bubble code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
               background:#f1f3f5; padding:.1em .35em; border-radius:4px; font-size:.9em}
  .bubble pre.code{background:var(--code-bg); color:var(--code-ink); padding:12px; border-radius:8px;
                   overflow:auto; margin:.5em 0}
  .bubble pre.code code{background:none; color:inherit; padding:0}
  .bubble ul,.bubble ol{margin:.35em 0; padding-left:1.5em}
  .bubble li{margin:.15em 0}
  .bubble a{color:var(--accent)}
  .bubble table{border-collapse:collapse; width:auto; max-width:100%; margin:.6em 0; font-size:.94em; display:block; overflow-x:auto}
  .bubble th,.bubble td{border:1px solid var(--border); padding:5px 10px; text-align:left; vertical-align:top}
  .bubble th{background:#f6f7f9; font-weight:600}
  .bubble tbody tr:nth-child(even){background:#fafbfc}
  .bubble blockquote{margin:.4em 0; padding:.3em .9em; border-left:3px solid var(--accent); color:var(--muted)}

  .err .bubble{background:#fdecec; color:var(--err); border:1px solid #f5c2c2}

  .typing{align-self:stretch; color:var(--muted); font-size:13px; padding:2px 4px}
  .typing .d{display:inline-block; width:6px; height:6px; margin-right:3px; border-radius:50%; background:var(--muted);
             animation:blink 1.1s infinite}
  .typing .d:nth-child(2){animation-delay:.2s} .typing .d:nth-child(3){animation-delay:.4s}
  @keyframes blink{0%,60%,100%{opacity:.25} 30%{opacity:1}}

  footer{
    background:var(--panel); border-top:1px solid var(--border); padding:12px 16px 16px;
    position:sticky; bottom:0;
  }
  form{max-width:768px; margin:0 auto; display:flex; gap:8px; align-items:flex-end;
       background:#fff; border:1px solid var(--border); border-radius:14px; padding:8px 10px}
  form:focus-within{border-color:var(--accent); box-shadow:0 0 0 3px rgba(91,141,239,.15)}
  textarea{flex:1; border:none; resize:none; outline:none; font:inherit; color:inherit;
           max-height:180px; padding:6px 4px; background:transparent}
  #send{border:none; background:var(--accent); color:#fff; border-radius:10px; padding:9px 16px; font-weight:600}
  #send:hover{filter:brightness(1.07)}
  .hint{text-align:center; color:var(--muted); font-size:11.5px; margin-top:6px}
</style>
</head>
<body>
<header>
  <div class="brand"><span class="dot"></span>micro</div>
  <div id="meta"></div>
  <button id="clear">Clear</button>
</header>
<div class="chat">
<main><div class="wrap" id="log"></div></main>
<button id="jmp" title="Jump to latest">&#8595;</button>
</div>
<footer>
  <form id="f" autocomplete="off">
    <textarea id="msg" rows="1" placeholder="Send a message…  (Enter = send, Shift+Enter = newline)"></textarea>
    <button id="send" type="submit">Send</button>
  </form>
  <div class="hint">micro can run shell commands, drive a browser, and fetch web pages. Review actions before trusting them.</div>
</footer>

<script>
const sid = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2)+Date.now());
const log = document.getElementById('log');
const ta  = document.getElementById('msg');
const form= document.getElementById('f');
const sendBtn = document.getElementById('send');
const meta = document.getElementById('meta');
const clearBtn = document.getElementById('clear');
const scroller = document.querySelector('main');
const jmp = document.getElementById('jmp');
let pinned = true;                       // keep latest content in view while true
const PIN_GAP = 90;                      // px within this of the bottom still counts as "pinned"

function reallyScroll(){ scroller.scrollTop = scroller.scrollHeight; }
// While the user reads older messages, stop yanking the view; show a jump button instead.
scroller.addEventListener('scroll', ()=>{
  const near = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < PIN_GAP;
  pinned = near;
  jmp.classList.toggle('show', !near);
}, {passive:true});
jmp.addEventListener('click', ()=>{ pinned=true; reallyScroll(); jmp.classList.remove('show'); });

let busy = false;          // a turn is in progress
let curAgent = null;       // current agent bubble element (receives streamed tokens)
let curBuf = "";           // accumulated raw text for curAgent
let typingEl = null;       // three-dots indicator

function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// Markdown renderer (line-based: headings, lists, tables, blockquotes, code
// blocks, inline bold/italic/code/links). Correctly handles multi-char list
// markers without eating content characters.
function md(src){
  if(!src) return '';

  // 1) Pull fenced code blocks out FIRST so nothing inside them gets mangled.
  var blocks = [];
  src = src.replace(/\r\n/g,'\n');
  src = src.replace(/```(\w*)\n?([\s\S]*?)```/g, function(_, lang, code){
    blocks.push('<pre class="code"><code>'+esc(code.replace(/\n$/,''))+'</code></pre>');
    return '\u0001B'+(blocks.length-1)+'\u0001';
  });

  // 2) Escape everything remaining.
  src = esc(src);

  // 3) Inline parser — applied per-line below so block Markdown can't leak in.
  function inline(txt){
    return txt
      .replace(/!\[([^\]]*)\]\((https?:[^)]+)\)/g, '<img src="$2" alt="$1">')
      .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g, '$1<em>$2</em>')
      .replace(/(^|[^`])`([^`\n]+?)`(?![`])/g, '$1<code>$2</code>')
      .replace(/^\*\*([^*]+?)\*\*/, '<strong>$1</strong>'); // bold at line start
  }

  // 4) Walk line-by-line, grouping consecutive list/table rows into blocks.
  var out = [];
  var lines = src.split('\n');
  var i = 0;
  while(i < lines.length){
    var line = lines[i];

    // Fenced code placeholder left from step 1? Emit it as-is (a block).
    var cm = line.match(/^\u0001B(\d+)\u0001$/);
    if(cm){ out.push(blocks[+cm[1]]); i++; continue; }

    // Blank line
    if(/^\s*$/.test(line)){ i++; continue; }

    // Heading
    var hm = line.match(/^(#{1,6})\s+(.*)$/);
    if(hm){ var lvl = hm[1].length; out.push('<h'+lvl+'>'+inline(hm[2].trim())+'</h'+lvl+'>'); i++; continue; }

    // Horizontal rule
    if(/^\s*([-*_])\1{2,}\s*$/.test(line)){ out.push('<hr>'); i++; continue; }

    // Blockquote (group consecutive)
    if(/^&gt;\s?/.test(line)){
      var quote = [];
      while(i < lines.length && /^&gt;\s?/.test(lines[i])){ quote.push(inline(lines[i].replace(/^&gt;\s?/,''))); i++; }
      out.push('<blockquote>'+quote.join('<br>')+'</blockquote>');
      continue;
    }

    // Table — a header row, a separator row (|---|), then data rows.
    var lookTable = lines.slice(i, i+2);
    if(/^[ \t]*\|.*\|[ \t]*$/.test(lookTable[0]) && /^[ \t]*\|[\s:|-]+\|[ \t]*$/.test(lookTable[1]||'')){
      // header
      var head = lookTable[0].replace(/^[ \t]*\||\|[ \t]*$/g,'').split('|').map(function(c){return c.trim();});
      var rows = [];
      var j = i+2;
      while(j < lines.length && /^[ \t]*\|.*\|[ \t]*$/.test(lines[j])){
        rows.push(lines[j].replace(/^[ \t]*\||\|[ \t]*$/g,'').split('|').map(function(c){return c.trim();}));
        j++;
      }
      var cells = head.map(function(c){return '<th>'+inline(c)+'</th>';}).join('');
      var body = rows.map(function(r){
        return '<tr>'+head.map(function(_,k){return '<td>'+inline(r[k]||'')+'</td>';}).join('')+'</tr>';
      }).join('');
      out.push('<table><thead><tr>'+cells+'</tr></thead><tbody>'+body+'</tbody></table>');
      i = j; continue;
    }

    // List — group consecutive items (unordered or ordered, mixed markers ok).
    var lm = line.match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
    if(lm){
      var items = [];
      while(i < lines.length){
        var im = lines[i].match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
        if(!im) break;
        items.push('<li>'+inline(im[3])+'</li>');
        i++;
      }
      // Use <ul> for - * +, <ol> for N. — detect from the first marker here.
      out.push((line.match(/^\s*\d+\./) ? '<ol>' : '<ul>') + items.join('') + (line.match(/^\s*\d+\./) ? '</ol>' : '</ul>'));
      continue;
    }

    // Paragraph — gather consecutive plain lines until a blank or block-syntax line.
    var para = [line];
    i++;
    while(i < lines.length){
      var nxt = lines[i];
      if(/^\s*$/.test(nxt)) break;
      if(/^(#{1,6})\s/.test(nxt)) break;
      if(/^&gt;\s?/.test(nxt)) break;
      if(/^[ \t]*\|.*\|[ \t]*$/.test(nxt)) break;
      if(/^\u0001B\d+\u0001$/.test(nxt)) break;
      if(/^(\s*)([-*+]|\d+\.)\s+/.test(nxt)) break;
      para.push(nxt); i++;
    }
    out.push('<p>'+inline(para.join(' '))+'</p>');
  }

  return out.join('');
}

// Auto-scroll only when the user is already following the conversation.
function scrollDown(){ if(pinned) reallyScroll(); }

function addMsg(cls, who){
  const m = document.createElement('div');
  m.className = 'msg '+cls;
  if(who){
    const w = document.createElement('div'); w.className='who'; w.textContent=who; m.appendChild(w);
  }
  const b = document.createElement('div'); b.className='bubble'; m.appendChild(b);
  log.appendChild(m); scrollDown();
  return b;
}

function addUser(text){
  const b = addMsg('user','You');
  b.textContent = text;
}
function newAgent(){
  curAgent = document.createElement('div');
  curAgent.className='msg agent';
  const w=document.createElement('div'); w.className='who'; w.textContent='Agent'; curAgent.appendChild(w);
  curBuf='';
  const b=document.createElement('div'); b.className='bubble'; curAgent.appendChild(b);
  log.appendChild(curAgent); scrollDown();
  return b;
}
function showTyping(){
  if(typingEl) return;
  typingEl=document.createElement('div'); typingEl.className='typing'; typingEl.innerHTML='<span class="d"></span><span class="d"></span><span class="d"></span> thinking…';
  log.appendChild(typingEl); scrollDown();
}
function hideTyping(){ if(typingEl){typingEl.remove(); typingEl=null;} }

function addStep(name, summary, output){
  const el=document.createElement('div'); el.className='step';
  let inner='<span class="arr">▸</span><span class="nm">'+esc(name)+'</span><span>'+esc(summary)+'</span>';
  if(output){
    inner += '<details><summary></summary><pre>'+esc(output)+'</pre></details>';
  }
  el.innerHTML=inner;
  log.appendChild(el); scrollDown();
}

function setBusy(b){
  busy=b; sendBtn.disabled=b; ta.disabled=false;
  if(!b){ ta.focus(); }
}

// ── SSE event handling ──────────────────────────────────────────────────
// `es` is reassigned on each reconnect, so it's `let`. All listeners live
// inside attachSSE() so they're re-bound to every fresh EventSource.
let es = null;
let esBackoff = 3000;

function attachSSE(stream){
  stream.addEventListener('open',  ()=>{ esBackoff = 3000; });          // reset backoff once connected
  stream.addEventListener('meta', e=>{
    try{ const d=JSON.parse(e.data); meta.textContent=(d.provider||'')+' · '+(d.model||'')+' · '+(d.mode||''); }catch(_){}
  });
  stream.addEventListener('token', e=>{
    hideTyping();
    if(!curAgent || curAgent.classList.contains('done')) newAgent();
    let t=''; try{ t=JSON.parse(e.data).text||''; }catch(_){}
    curBuf += t;
    curAgent.querySelector('.bubble').innerHTML = md(curBuf);
    scrollDown();
  });
  stream.addEventListener('demote', ()=>{
    if(curAgent){ curAgent.classList.add('thoughts'); curAgent.classList.remove('agent'); curAgent.classList.add('agent','thoughts');
      curAgent.querySelector('.who').textContent='Agent'; }
    curAgent=null; curBuf='';
  });
  stream.addEventListener('step', e=>{
    hideTyping();
    let d={}; try{ d=JSON.parse(e.data); }catch(_){}
    addStep(d.name||'tool', d.summary||'', d.output||'');
  });
  stream.addEventListener('error_evt', e=>{   // renamed — avoid clashing with SSE's reserved 'error' event
    hideTyping();
    let msg=''; try{ msg=JSON.parse(e.data).message||''; }catch(_){ msg=e.data; }
    const b=addMsg('msg err','micro'); b.textContent=msg;
  });
  stream.addEventListener('done', ()=>{
    hideTyping();
    if(curAgent){ curAgent.classList.add('done'); }
    curAgent=null; curBuf='';
    setBusy(false);
  });
  // SSE's built-in 'error' fires on disconnect. The browser's automatic retry
  // is way too aggressive (instantly hammers the URL), so on error we close
  // the stream, back off, and reopen one fresh EventSource. This stops the
  // ERR_CONNECTION_REFUSED console flood during a server restart.
  stream.addEventListener('error', ()=>{
    try{ stream.close(); }catch(_){}
    setTimeout(()=>{ es = new EventSource('/stream?session='+encodeURIComponent(sid)); attachSSE(es); }, esBackoff);
    esBackoff = Math.min(esBackoff * 1.5, 15000);   // cap at 15s
  });
}
es = new EventSource('/stream?session='+encodeURIComponent(sid));
attachSSE(es);

// ── Sending ─────────────────────────────────────────────────────────────
form.addEventListener('submit', ev=>{
  ev.preventDefault();
  const text=ta.value.trim();
  if(!text || busy) return;
  pinned = true; jmp.classList.remove('show');
  addUser(text);
  ta.value=''; ta.style.height='auto';
  setBusy(true);
  showTyping();
  fetch('/send',{method:'POST','headers':{'Content-Type':'application/json'},
        body:JSON.stringify({session:sid,message:text})}).catch(err=>{
    hideTyping(); const b=addMsg('msg err','micro'); b.textContent='Network error: '+err;
    setBusy(false);
  });
});

// Enter to send, Shift+Enter for newline
ta.addEventListener('keydown', ev=>{
  if(ev.key==='Enter' && !ev.shiftKey){ ev.preventDefault(); form.requestSubmit(); }
});
// auto-grow textarea
ta.addEventListener('input', ()=>{ ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight,180)+'px'; });

clearBtn.addEventListener('click', ()=>{
  if(busy) return;
  fetch('/clear',{method:'POST','headers':{'Content-Type':'application/json'},
        body:JSON.stringify({session:sid})});
  log.innerHTML=''; curAgent=null; curBuf=''; pinned=true; jmp.classList.remove('show'); ta.focus();
});

window.addEventListener('beforeunload', ()=>{ try{es.close();}catch(_){} });
</script>
</body>
</html>
"""


# ── HTTP layer ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "micro-web/1.0"

    def log_message(self, *args):
        # Quieter access log; the agent's own prints already populate stdout.
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        from urllib.parse import unquote

        if path == "/" or path == "/index.html":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/stream":
            self._stream_sse(unquote(params.get("session", "")))
            return
        if path == "/state":
            p = get_provider()
            self._json(200, {
                "provider": micro.PROVIDER,
                "model": p["model"],
                "mode": "thinking" if THINKING_ENABLED else "no-think",
            })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        sid = payload.get("session") or ""
        if path == "/send":
            msg = (payload.get("message") or "").strip()
            if not sid or not msg:
                self._json(400, {"error": "session and message required"})
                return
            get_session(sid)  # ensure it exists
            start_turn(sid, msg)
            # Send the meta right away so the header populates & typing starts.
            p = get_provider()
            broadcast(sid, {"type": "meta", "provider": micro.PROVIDER, "model": p["model"],
                            "mode": "thinking" if THINKING_ENABLED else "no-think"})
            self._json(200, {"ok": True})
            return
        if path == "/clear":
            if sid:
                reset_session(sid)
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def _stream_sse(self, sid: str):
        if not sid:
            self._json(400, {"error": "session required"})
            return
        s = get_session(sid)
        q: Queue = Queue()
        s["subs"].append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        # Greet with current provider/model so the header populates immediately.
        p = get_provider()
        self._sse("meta", {"provider": micro.PROVIDER, "model": p["model"],
                           "mode": "thinking" if THINKING_ENABLED else "no-think"})
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                except Empty:
                    self.wfile.write(b": hb\n\n")
                    self.wfile.flush()
                    continue
                self._sse(ev.get("type", "msg"), ev)
                if ev.get("type") == "done":
                    # Keep the connection open for the next turn — just a heartbeat ping.
                    # Clients stay subscribed; nothing else to do.
                    pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                s["subs"].remove(q)
            except ValueError:
                pass

    def _sse(self, event: str, data: dict):
        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()


def parse_args():
    ap = argparse.ArgumentParser(description="micro agent — web UI")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to listen on (default 5555)")
    ap.add_argument("--host", default=HOST, help="bind address (default 0.0.0.0)")
    ap.add_argument("--provider", choices=list(micro.PROVIDERS), help="select provider (overrides $PROVIDER)")
    return ap.parse_args()


def main():
    global DEFAULT_PORT
    args = parse_args()
    if args.provider:
        micro.PROVIDER = args.provider
    p = get_provider()
    mode = "thinking" if THINKING_ENABLED else "no-think"
    print(f"  micro web UI → http://localhost:{args.port}")
    print(f"  Provider: {micro.PROVIDER} | Model: {p['model']} | Mode: {mode}")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        srv.shutdown()


if __name__ == "__main__":
    main()
