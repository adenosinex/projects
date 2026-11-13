#!/usr/bin/env python3
"""One-off backfill: fetch and store the raw subscription text for
subscriptions saved before raw-text storage existed, so the converter
can preserve their original proxy-groups / rules / dns.

- url: sources are re-fetched (the stored source is the full URL).
- file: sources are read from /Users/zin/sub-test/<filename> if present.
Run once: /Users/zin/sub-convert/venv/bin/python backfill_raw.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parser as subparser  # noqa: E402
import store  # noqa: E402


def main():
    store.init()
    for meta in store.list_subs():
        sub_id = meta["id"]
        if store.get_sub_raw(sub_id):
            print(sub_id, meta.get("name"), "already has raw, skip")
            continue
        src = meta.get("source", "")
        raw = None
        try:
            if src.startswith("url:"):
                url = src[len("url:"):]
                raw = subparser.fetch_url(url)
            elif src.startswith("file:"):
                fname = src[len("file:"):]
                for cand in (os.path.join("/Users/zin/sub-test", fname),
                             os.path.join("/Users/zin/sub-convert", fname)):
                    if os.path.exists(cand):
                        with open(cand, encoding="utf-8") as f:
                            raw = f.read()
                        break
        except Exception as e:
            print(sub_id, meta.get("name"), "fetch failed:", e)
            continue
        if raw:
            store.save_sub_raw(sub_id, raw)
            doc = subparser.extract_yaml_doc(raw)
            groups = ""
            if doc and isinstance(doc.get("proxy-groups"), list):
                groups = " groups=" + ",".join(
                    str(g.get("name", "")) for g in doc["proxy-groups"])
            print(sub_id, meta.get("name"), "saved raw (%d bytes)%s"
                  % (len(raw), groups))
        else:
            print(sub_id, meta.get("name"), "no raw found, left as-is")
    return 0


if __name__ == "__main__":
    sys.exit(main())
