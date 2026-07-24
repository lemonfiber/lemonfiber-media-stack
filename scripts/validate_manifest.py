#!/usr/bin/env python3
"""Validate stack.toml against the contract. See spec
20-architecture/contracts/stack-manifest.md. Reports every violation at once.
"""
import sys, tomllib, collections
d = tomllib.load(open("stack.toml", "rb"))
profiles = {p["id"] for p in d["profile"]}
services = d["service"]
prof_of = {s["id"]: s["profile"] for s in services}
errs = []
for s in services:
    if s["profile"] not in profiles:
        errs.append(f"{s['id']}: unknown profile {s['profile']}")
    if str(s.get("tag", "")).endswith("latest"):
        errs.append(f"{s['id']}: floating tag not allowed (E1-R1)")
    if "port" in s and "bind" not in s:
        errs.append(f"{s['id']}: port without bind (C6)")
    if s.get("bind") not in (None, "loopback", "lan"):
        errs.append(f"{s['id']}: bind must be loopback|lan")
    for dep in s.get("depends_on", []):
        if prof_of.get(dep) != s["profile"]:
            errs.append(f"{s['id']}: cross-profile depends_on {dep} (B1-R14)")
for f in d["form"]:
    for p in f["profiles"]:
        if p not in profiles:
            errs.append(f"form {f['id']}: unknown profile {p}")
for i, c in collections.Counter(s["id"] for s in services).items():
    if c > 1:
        errs.append(f"duplicate service id: {i}")
if errs:
    print("\n".join(f"::error::{e}" for e in errs))
    sys.exit(1)
print(f"manifest valid: {len(profiles)} profiles, {len(d['form'])} forms, {len(services)} services")
