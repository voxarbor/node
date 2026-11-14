"""One-shot model setup on a worker node (run by the `init` service of deploy/worker/compose.yaml):
waits for the STT service and asks it to download the Whisper model (speaches API); idempotent."""
import json, os, sys, time, urllib.request

TOKEN = os.environ["API_KEY"]
STT = os.environ.get("STT_URL", "http://stt:8000")
MODEL = os.environ.get("STT_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")


def call(method, path):
    req = urllib.request.Request(f"{STT}{path}", method=method, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=3600) as r:
        return r.status, r.read()


for _ in range(180):
    try:
        call("GET", "/health")  # speaches wants the token even on /health
        break
    except Exception:
        time.sleep(5)
else:
    sys.exit("stt did not come up")
try:
    models = json.loads(call("GET", "/v1/models")[1]).get("data", [])
    if any(m.get("id") == MODEL for m in models):
        print("whisper model already present:", MODEL)
        sys.exit(0)
except Exception as exc:
    print("cannot list models:", exc)
print("downloading whisper model", MODEL, "(~1.6 GB) ...", flush=True)
print(call("POST", f"/v1/models/{MODEL}"))
