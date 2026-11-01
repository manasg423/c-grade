import urllib.request
import json
import time

payload = {
    "model": "qwen3.5:2b",
    "messages": [
        {"role": "user", "content": "Return a JSON object: {\"message\": \"hello\"}"}
    ],
    "format": "json",
    "stream": False
}
req = urllib.request.Request("http://127.0.0.1:11434/api/chat", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
t0 = time.time()
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode("utf-8"))
    print(f"Time: {time.time()-t0:.2f}s")
    print("Content:", data["message"]["content"])

