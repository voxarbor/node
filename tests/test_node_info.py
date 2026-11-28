"""node-info /capabilities models built from service entries, no services needed:
  python3 tests/test_node_info.py
Audit 2026-11-28 #17: the remotion model carries "versions" as a list next to "settings" (the coordinator reads
models[].versions), #46: the stt model is the one the node really has."""
import importlib.util
import os

os.environ.setdefault("API_KEY", "x")
spec = importlib.util.spec_from_file_location("ni", os.path.join(os.path.dirname(__file__), "..", "services",
                                                                 "node-info", "app.py"))
ni = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ni)

gen = {"available": True, "healthy": True, "remotion": "4.0.545", "versions": ["4.0.545"], "device": "cpu",
       "busy": False, "paused": False, "bundles": ["a" * 64], "perf": {"fps": 3.1}, "slots": 1,
       "settings": {"type": "object", "properties": {"crf": {"type": "integer"}}}}
m = {x["id"]: x for x in ni.models({"gen": gen})}["remotion"]
assert m["versions"] == ["4.0.545"] and m["revision"] == "remotion@4.0.545", m
assert "versions" not in m["settings"], m["settings"]
assert m["fps"] == 3.1 and m["bundles"] == ["a" * 64] and m["slots"] == 1 and m["kind"] == "render", m
old = {k: v for k, v in gen.items() if k != "versions"}  # a gen before 0.1.x without "versions"
assert {x["id"]: x for x in ni.models({"gen": old})}["remotion"]["versions"] == ["4.0.545"]
assert "lease_s_max" not in m, m                       # an older gen: no lease offered
m = {x["id"]: x for x in ni.models({"gen": dict(gen, lease_s_max=86400)})}["remotion"]
assert m["lease_s_max"] == 86400, m
print("#17 models[].versions, #38 lease_s_max ok")
stt = {"available": True, "healthy": True, "model": "Systran/faster-whisper-small", "model_revision": "abc123"}
w = [x for x in ni.models({"stt": stt}) if x["kind"] == "stt"][0]
assert w["id"] == "whisper-small" and w["revision"] == "Systran/faster-whisper-small@abc123", w
assert [x for x in ni.models({"stt": dict(stt, model_installed=False)}) if x["kind"] == "stt"][0]["available"] is False
assert [x for x in ni.models({"stt": dict(stt, model_installed=None)}) if x["kind"] == "stt"][0]["available"] is True
print("#46 stt model = the node's own STT_MODEL, not offered when speaches lacks it ok")
print("NODE INFO OK")
