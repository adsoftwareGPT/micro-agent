#!/usr/bin/env python3
"""GLM-5.2 self-test via z.ai endpoint — measures token throughput."""
import os, sys, time, json, requests
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.getenv("ZAI_KEY")
MODEL   = os.getenv("ZAI_MODEL", "glm-5.2")
URL     = "https://api.z.ai/api/coding/paas/v4/chat/completions"

HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
PAYLOAD = {
    "model": MODEL,
    "messages": [{"role": "user", "content":
        "In one short sentence, confirm you are operational."}],
    "max_tokens": 1024,        # reasoning models need headroom
    "temperature": 0.3,
    "stream": True,
}

print(f"🧪 GLM-5.2 Self-Test")
print(f"   endpoint: {URL}")
print(f"   model:    {MODEL}\n")

resp = requests.post(URL, headers=HEADERS, json=PAYLOAD, stream=True, timeout=60)
resp.raise_for_status()

reasoning, content = "", ""
t0 = time.perf_counter()
first_tok_t = None
tok_count = 0

print("─" * 60)
for line in resp.iter_lines():
    if not line:
        continue
    line = line.decode("utf-8")
    if not line.startswith("data:"):
        continue
    data = line[5:].strip()
    if data == "[DONE]":
        break
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        continue
    delta = chunk["choices"][0]["delta"]
    r = delta.get("reasoning_content", "")
    c = delta.get("content", "")
    if r or c:
        if first_tok_t is None:
            first_tok_t = time.perf_counter() - t0
        if r:
            reasoning += r
            tok_count += 1
            print(f"\r💭 thinking... ({tok_count} tok)", end="", flush=True)
        if c:
            content += c
            tok_count += 1
            print(c, end="", flush=True)
    u = chunk.get("usage")
    if u:
        tok_count = u.get("completion_tokens", tok_count)

elapsed = time.perf_counter() - t0
print("\n" + "─" * 60)

print(f"\n📊 Results:")
print(f"   Answer:           {content.strip()!r}")
print(f"   Reasoning tok:    {len(reasoning)} chars")
print(f"   Time to 1st tok:  {first_tok_t:.3f}s" if first_tok_t else "   Time to 1st tok: N/A")
print(f"   Total time:       {elapsed:.3f}s")
print(f"   Tokens generated: {tok_count}")
if elapsed > 0 and tok_count:
    print(f"   Token speed:      {tok_count / elapsed:.1f} tok/s")
