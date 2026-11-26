"""drop_unused_models(): the HF cache of a disabled engine goes, shared blobs another repo uses stay. No models needed:
docker run --rm -e API_KEY=x -e TTS_ENGINES=holos -v $PWD/services/tts:/app:ro -v $PWD/tests/test_drop_unused.py:/t.py:ro \
  ghcr.io/voxarbor/node-tts:<tag> python /t.py"""
import os
import sys
import tempfile

sys.path.insert(0, "/app")
import app  # noqa: E402

hub = tempfile.mkdtemp()
blobs = os.path.join(hub, "blobs")
os.makedirs(blobs)


def repo(name, files):
    snap = os.path.join(hub, f"models--{name.replace('/', '--')}", "snapshots", "abc")
    os.makedirs(snap)
    for fname, blob, size in files:
        path = os.path.join(blobs, blob)
        if not os.path.exists(path):
            open(path, "wb").write(b"x" * size)
        os.symlink(path, os.path.join(snap, fname))


repo("patriotyk/styletts2_ukrainian_multispeaker", [("model.bin", "b1", 1000), ("config.yml", "shared", 10)])
repo("patriotyk/styletts2_ukrainian_single", [("model.bin", "b2", 500)])
repo("skypro1111/m2m100-ukr-verbalization", [("config.json", "shared", 10), ("model.bin", "b3", 300)])

freed = app.drop_unused_models(hub)
assert freed == 1500, freed
assert not os.path.exists(os.path.join(hub, "models--patriotyk--styletts2_ukrainian_single"))
assert not os.path.exists(os.path.join(blobs, "b1")) and not os.path.exists(os.path.join(blobs, "b2"))
assert os.path.exists(os.path.join(blobs, "shared")) and os.path.exists(os.path.join(blobs, "b3")), "shared blob lost"
assert app.drop_unused_models(hub) == 0  # nothing left to do
print("DROP UNUSED OK")
