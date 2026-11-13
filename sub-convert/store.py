"""JSON file storage for sub-convert. No DB server needed."""
import fcntl
import json
import os
import threading
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
SUBS_DIR = os.path.join(DATA_DIR, "subs")
CONV_DIR = os.path.join(DATA_DIR, "converted")
LOCK_PATH = os.path.join(DATA_DIR, ".test.lock")
_lock = threading.Lock()


def init():
    os.makedirs(SUBS_DIR, exist_ok=True)
    os.makedirs(CONV_DIR, exist_ok=True)
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


def get_sub_meta(sub_id):
    for m in list_subs():
        if m["id"] == sub_id:
            return m
    return None


def save_sub(meta, nodes):
    with _lock:
        subs = _read_json(_subs_path(), [])
        subs = [s for s in subs if s["id"] != meta["id"]]
        subs.append(meta)
        _write_json(_subs_path(), subs)
        _write_json(os.path.join(SUBS_DIR, meta["id"] + ".json"), nodes)
    return meta


def update_sub_meta(sub_id, **fields):
    with _lock:
        subs = _read_json(_subs_path(), [])
        for s in subs:
            if s["id"] == sub_id:
                s.update(fields)
                break
        else:
            return None
        _write_json(_subs_path(), subs)
        return next((s for s in subs if s["id"] == sub_id), None)


def get_sub_nodes(sub_id):
    return _read_json(os.path.join(SUBS_DIR, sub_id + ".json"), None)


def save_sub_raw(sub_id, raw_text):
    """Store the raw subscription text (for preserving the source's
    original proxy-groups/rules/etc. when converting)."""
    with open(os.path.join(SUBS_DIR, sub_id + ".raw"), "w",
              encoding="utf-8") as f:
        f.write(raw_text or "")


def get_sub_raw(sub_id):
    """Raw subscription text, or None if not stored (older entries)."""
    try:
        with open(os.path.join(SUBS_DIR, sub_id + ".raw"),
                  encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def delete_sub(sub_id):
    with _lock:
        subs = _read_json(_subs_path(), [])
        subs = [s for s in subs if s["id"] != sub_id]
        _write_json(_subs_path(), subs)
        for p in (os.path.join(SUBS_DIR, sub_id + ".json"),
                  os.path.join(SUBS_DIR, sub_id + ".raw"),
                  os.path.join(CONV_DIR, sub_id + ".yaml")):
            try:
                os.remove(p)
            except OSError:
                pass


def converted_path(sub_id):
    return os.path.join(CONV_DIR, sub_id + ".yaml")


def find_sub_by_token(token):
    for m in list_subs():
        if m.get("token") == token:
            return m
    return None


def new_id():
    return uuid.uuid4().hex[:12]


def new_token():
    return uuid.uuid4().hex  # 32 hex chars


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class TestLock:
    """Cross-process exclusive lock (file + fcntl) shared by the app and
    the hourly cron script, so a manual test and a cron run never overlap
    (and never start two mihomo instances on the same ports)."""

    def __init__(self):
        self._f = None

    def __enter__(self):
        self._f = open(LOCK_PATH, "w")
        fcntl.flock(self._f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._f, fcntl.LOCK_UN)
            self._f.close()
        except Exception:
            pass
        self._f = None
        return False


def lock_held():
    """True if another process currently holds the test lock."""
    try:
        f = open(LOCK_PATH, "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
        return False
    except OSError:
        return False
