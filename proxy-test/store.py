"""JSON file storage for proxy-test. No DB server needed."""
import json
import os
import threading
import time
import uuid

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SUBS_DIR = os.path.join(DATA_DIR, "subs")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
_lock = threading.Lock()


def init():
    os.makedirs(SUBS_DIR, exist_ok=True)
    os.makedirs(RUNS_DIR, exist_ok=True)
    if not os.path.exists(os.path.join(DATA_DIR, "subs.json")):
        _write_json(os.path.join(DATA_DIR, "subs.json"), [])


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _subs_path():
    return os.path.join(DATA_DIR, "subs.json")


def list_subs():
    with _lock:
        return _read_json(_subs_path(), [])


def save_sub(meta, nodes):
    with _lock:
        subs = _read_json(_subs_path(), [])
        subs = [s for s in subs if s["id"] != meta["id"]]
        subs.append(meta)
        _write_json(_subs_path(), subs)
        _write_json(os.path.join(SUBS_DIR, meta["id"] + ".json"), nodes)
    return meta


def get_sub_nodes(sub_id):
    return _read_json(os.path.join(SUBS_DIR, sub_id + ".json"), None)


def delete_sub(sub_id):
    with _lock:
        subs = _read_json(_subs_path(), [])
        subs = [s for s in subs if s["id"] != sub_id]
        _write_json(_subs_path(), subs)
        try:
            os.remove(os.path.join(SUBS_DIR, sub_id + ".json"))
        except OSError:
            pass


def new_id():
    return uuid.uuid4().hex[:12]


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def save_run(run):
    with _lock:
        _write_json(os.path.join(RUNS_DIR, run["id"] + ".json"), run)
    return run


def get_run(run_id):
    return _read_json(os.path.join(RUNS_DIR, run_id + ".json"), None)


def list_runs():
    out = []
    try:
        for fn in sorted(os.listdir(RUNS_DIR), reverse=True):
            if fn.endswith(".json"):
                r = _read_json(os.path.join(RUNS_DIR, fn), None)
                if r:
                    out.append({k: v for k, v in r.items()
                                if k != "results"})
    except OSError:
        pass
    return out
