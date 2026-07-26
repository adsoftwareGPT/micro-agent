#!/usr/bin/env python3
"""One-off GLM-5.2 larger run (~600 words). Not committed."""
import os, time, json, requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
HEADERS = {"Authorization": f"Bearer {os.getenv('ZAI_KEY')}", "Content-Type": "application/json"}
PAYLOAD = {
    "model": os.getenv("ZAI_MODEL", "glm-5.2"),
    "messages": [{"role": "user", "content":
        "Write a ~600 word essay on why Python became the dominant language for "
        "data science and AI. Cover: ecosystem, readability, NumPy/pandas/PyTorch, "
        "and the role of community. Use clear paragraphs."}],
    "max_tokens": 4096, "temperature": 0.4, "stream": True,
}

t0 = time.perf_counter()  # before request -> real TTFB
resp = requests.post(URL, headers=HEADERS, json=PAYLOAD, stream=True, timeout=120)
resp.raise_for_status()

reasoning = content = ""
first = None
chunks = 0
usage_tokens = None
print("─" * 60)
for line in resp.iter_lines():
    if not line:
        continue
    line = line.decode()
    if not line.startswith("data:"):
        continue
    data = line[5:].strip()
    if data == "[DONE]":
        break
    try:
        chunk = json.loads(data)
    except Exception:
        continue
    chunks += 1
    delta = chunk["choices"][0]["delta"]
    r = delta.get("reasoning_content", "")
    c = delta.get("content", "")
    if r or c:
        if first is None:
            first = time.perf_counter() - t0
        if r:
            reasoning += r
        if c:
            content += c
            print(c, end="", flush=True)
    u = chunk.get("usage")
    if u and u.get("completion_tokens"):
        usage_tokens = u["completion_tokens"]
elapsed = time.perf_counter() - t0
print("\n" + "─" * 60)
print(f"\n📊 Larger-run results:")
print(f"   Final answer chars: {len(content)}")
print(f"   Reasoning chars:    {len(reasoning)}")
print(f"   SSE chunks:         {chunks}")
print(f"   Tokens (usage API): {usage_tokens}")
print(f"   Time to 1st token:  {first:.3f}s" if first else "   no tokens")
print(f"   Total time:         {elapsed:.3f}s")
if usage_tokens and elapsed > 0:
    print(f"   Throughput:         {usage_tokens/elapsed:.1f} tok/s (via usage)")
if usage_tokens and content:
    print(f"   Approx chars/token: {len(content)/max(usage_tokens,1):.2f}")
