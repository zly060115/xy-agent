# -*- coding: utf-8 -*-
"""上传本地文件到运行中的 XY 并入库，然后打印结果。"""
import sys

import httpx

BASE = "http://127.0.0.1:7860"
src = sys.argv[1]
name = sys.argv[2] if len(sys.argv) > 2 else src.split("/")[-1]

c = httpx.Client(trust_env=False, timeout=300)

with open(src, "rb") as f:
    r = c.post(f"{BASE}/gradio_api/upload", files={"files": (name, f, "application/octet-stream")})
r.raise_for_status()
path = r.json()[0]
print("uploaded:", path)

body = {"data": [{"path": path, "meta": {"_type": "gradio.FileData"}, "orig_name": name}]}
r = c.post(f"{BASE}/gradio_api/call/ingest", json=body)
print("call:", r.status_code, r.text[:150])
if r.status_code != 200:
    sys.exit(1)

eid = r.json()["event_id"]
with c.stream("GET", f"{BASE}/gradio_api/call/ingest/{eid}") as s:
    for line in s.iter_lines():
        if line.startswith("data:"):
            last = line[5:].strip()
print()
print(last.replace("\\n", "\n"))
