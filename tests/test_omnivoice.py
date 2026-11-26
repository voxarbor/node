"""OmniVoice through node-tts: custom voice install (the recording goes from /tmp into the cache volume, another
filesystem), synthesis, re-registration after a tts-omnivoice restart, 503 instead of 500 while it restarts, delete.
Needs a running node-tts with TTS_ENGINES=holos,omnivoice and its tts-omnivoice (GPU node, or the CPU variant):
  python3 tests/test_omnivoice.py http://127.0.0.1:<tts port> <API key> <tts-omnivoice container> ref.wav "ref text"
Stdlib only; restarts the given tts-omnivoice container with docker."""
import base64
import hashlib
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

URL, KEY, OV, REF, REF_TEXT = sys.argv[1:6]
FAST = {"num_step": 4, "seed": 11}  # quality is not tested here: few steps keep the CPU variant quick


def call(method, path, body=None, timeout=900):
    req = urllib.request.Request(URL + path, json.dumps(body).encode() if body is not None else None,
                                 {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def speak(voice):
    return call("POST", "/v1/audio/speech", {"input": "Перевірка голосу.", "model": "omnivoice", "voice": voice,
                                             "settings": FAST, "response_format": "wav"})


raw = open(REF, "rb").read()
voice = {"model": "omnivoice", "name": "Тест", "gender": "female", "ref_audio": base64.b64encode(raw).decode(),
         "ref_text": REF_TEXT, "sha256": hashlib.sha256(raw).hexdigest(), "consent_ref": "test"}

st, b = call("PUT", "/v1/voices/custom/omnitest", voice)
assert st == 200 and json.loads(b)["status"] in ("created", "replaced"), (st, b[:300])
print("install ok")

st, first = speak("custom:omnitest")
assert st == 200 and first[:4] == b"RIFF", (st, first[:200])
print("speech ok")

subprocess.run(["docker", "restart", "-t", "2", OV], check=True, capture_output=True)
st, again = speak("custom:omnitest")  # the server lost its RAM registry: node-tts registers the voice again
assert st == 200, (st, again[:300])
print("re-registration after restart ok", "(same audio)" if again == first else "(audio differs)")

# a request while tts-omnivoice is starting: answered (after the node's retry) or 503, never 500
res = {}
subprocess.run(["docker", "stop", "-t", "1", OV], check=True, capture_output=True)
t = threading.Thread(target=lambda: res.update(r=speak("male")))
t.start()
time.sleep(1)
subprocess.run(["docker", "start", OV], check=True, capture_output=True)
t.join()
assert res["r"][0] in (200, 503), res["r"][0:1] + (res["r"][1][:300],)
print(f"request during restart: {res['r'][0]} ok")

for _ in range(60):  # wait until it is up again, then the 503 path with the service gone for good
    if speak("male")[0] == 200:
        break
    time.sleep(5)
subprocess.run(["docker", "stop", "-t", "1", OV], check=True, capture_output=True)
st, b = speak("male")
subprocess.run(["docker", "start", OV], check=True, capture_output=True)
assert st == 503, (st, b[:300])
print("service down: 503 ok")

st, b = call("DELETE", "/v1/voices/custom/omnitest")
assert st == 200, (st, b)
st, b = speak("custom:omnitest")
assert st == 422, (st, b[:200])
print("delete ok")
print("OMNIVOICE OK")
