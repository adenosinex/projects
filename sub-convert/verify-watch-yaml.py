#!/usr/bin/env python3
"""Verify a converted vps YAML: template-cleansing checks + refs."""
import sys

sys.path.insert(0, "/Users/zin/sub-convert")
import yaml  # noqa: E402
import parser as subparser  # noqa: E402

path = sys.argv[1] if len(sys.argv) > 1 else (
    "/Users/zin/sub-convert/data/converted/419387b4d5dc.yaml")
new = yaml.safe_load(open(path, encoding="utf-8"))
tdoc = subparser.extract_yaml_doc(
    open("/Users/zin/sub-convert/data/subs/419387b4d5dc.raw",
         encoding="utf-8").read())

print("精选:", [g["proxies"] for g in new["proxy-groups"]
                if g["name"] == "精选"])
print("精选中继:", [g["proxies"] for g in new["proxy-groups"]
                    if g["name"] == "精选中继"])
print("groups:", [g["name"] for g in new["proxy-groups"]])
print("rules==template:", new.get("rules") == tdoc.get("rules"))
print("dns==template:", new.get("dns") == tdoc.get("dns"))
print("rule-providers==template:",
      new.get("rule-providers") == tdoc.get("rule-providers"))
print("geox-url==template:", new.get("geox-url") == tdoc.get("geox-url"))
print("mixed-port==template:",
      new.get("mixed-port") == tdoc.get("mixed-port"))
print("mode==template:", new.get("mode") == tdoc.get("mode"))
names = {p["name"] for p in new["proxies"]}
gnames = {g["name"] for g in new["proxy-groups"]}
dang_group = [(g["name"], m) for g in new["proxy-groups"]
              for m in (g.get("proxies") or [])
              if m not in names and m not in gnames]
dang_dp = [(p["name"], p.get("dialer-proxy")) for p in new["proxies"]
           if isinstance(p.get("dialer-proxy"), str)
           and p["dialer-proxy"] not in names]
rule_refs = set()
for r in new.get("rules") or []:
    parts = str(r).split(",")
    if len(parts) >= 3:
        rule_refs.add(parts[2].strip())
print("dangling group members:", dang_group)
print("dangling dialer-proxy:", dang_dp)
print("rule refs missing from groups:",
      sorted(rule_refs - gnames - {"DIRECT", "REJECT"}))
print("empty groups:", [g["name"] for g in new["proxy-groups"]
                        if not g.get("proxies")])
print("proxies:", len(new["proxies"]))
ok = (new.get("rules") == tdoc.get("rules")
      and new.get("dns") == tdoc.get("dns")
      and not dang_group and not dang_dp
      and not (rule_refs - gnames - {"DIRECT", "REJECT"})
      and not [g for g in new["proxy-groups"] if not g.get("proxies")])
print("ALL-CHECKS-PASS" if ok else "CHECKS-FAILED")
sys.exit(0 if ok else 1)
