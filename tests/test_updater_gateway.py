"""node-updater gateway_sync(): the gateway is restarted when the Caddyfile it sees differs from the node's, not
otherwise; with viewers on the node the periodic check waits. Docker is mocked, no containers needed:
  python3 tests/test_updater_gateway.py"""
import importlib.util
import os
import subprocess
import tempfile

spec = importlib.util.spec_from_file_location("updater", os.path.join(os.path.dirname(__file__), "..", "services",
                                                                      "updater", "updater.py"))
upd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upd)

wd = tempfile.mkdtemp()
open(os.path.join(wd, "Caddyfile"), "w").write(":8080 {\n\thandle_path /gen/* {\n\t}\n}\n")
state = {"seen": "", "calls": [], "gateway": True, "streams": 0}


def fake_run(cmd, timeout=3600, check=True, env=None):
    state["calls"].append(" ".join(cmd))
    out, rc = "", 0
    if cmd[:3] == ["docker", "ps", "-q"]:
        out = "gw123\n" if state["gateway"] else ""
    elif cmd[:2] == ["docker", "exec"]:
        out = state["seen"]
    return subprocess.CompletedProcess(cmd, rc, out, "")


upd.run = fake_run
upd.busy_streams = lambda: state["streams"]
upd.tts_idle = lambda: True
p = object.__new__(upd.Project)
p.name, p.wd = "voxarbor-node", wd
u = object.__new__(upd.Updater)
u.p = p
restarts = lambda: [c for c in state["calls"] if c.startswith("docker restart")]

# the gateway still sees the Caddyfile of an older release: restarted
state["seen"] = ":8080 {\n}\n"
assert u.gateway_sync() is True and restarts() == ["docker restart gw123"], state["calls"]
# the same file: nothing
state.update(seen=open(os.path.join(wd, "Caddyfile")).read(), calls=[])
assert u.gateway_sync() is False and not restarts(), state["calls"]
# periodic check with viewers on the node: waits; without them: restarts
state.update(seen="old", calls=[], streams=2)
assert u.gateway_sync(only_when_quiet=True) is False and not restarts(), state["calls"]
state.update(calls=[], streams=0)
assert u.gateway_sync(only_when_quiet=True) is True and restarts(), state["calls"]
# no gateway container (stopped node): nothing
state.update(calls=[], gateway=False)
assert u.gateway_sync() is False and not restarts(), state["calls"]
print("GATEWAY SYNC OK")
