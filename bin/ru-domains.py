#!/usr/bin/env python3
"""Управление доменами для маршрутизации через ru-туннель."""
import json, subprocess, sys
from pathlib import Path

JSON = Path("/etc/wireguard/ru-domains.json")
ROUTES = "/usr/local/bin/ru-routes.sh"

def load():
    if not JSON.exists():
        JSON.write_text("{}")
        JSON.chmod(0o600)
    return json.loads(JSON.read_text())

def save(d):
    JSON.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    JSON.chmod(0o600)

def resolve(dom):
    try:
        r = subprocess.run(["dig","+short","+time=3","+tries=2","A", dom],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return set()
    out = set()
    for line in r.stdout.split("\n"):
        line = line.strip()
        if line and line.count(".") == 3 and line.replace(".","").isdigit():
            out.add(line)
    return out

def routes_call(verb, ips):
    if not ips: return
    args = [ROUTES, verb] + [ip + "/32" for ip in ips]
    subprocess.run(args, capture_output=True)

def cmd_list():
    d = load()
    if not d: return "(пусто)"
    lines = []
    for dom, ips in sorted(d.items()):
        lines.append(f"{dom}: {len(ips)} IP")
    return "\n".join(lines)

def cmd_show(dom):
    d = load()
    if dom not in d: return f"{dom}: нет в списке"
    return f"{dom}:\n" + "\n".join(d[dom])

def cmd_add(domains):
    d = load()
    results = []
    for dom in domains:
        ips = resolve(dom)
        if not ips:
            results.append(f"❌ {dom}: не резолвится")
            continue
        old = set(d.get(dom, []))
        new = old | ips
        d[dom] = sorted(new)
        added = ips - old
        routes_call("add", sorted(added))
        results.append(f"✅ {dom}: {len(ips)} IP (+{len(added)} новых)")
    save(d)
    return "\n".join(results)

def cmd_remove(domains):
    d = load()
    results = []
    for dom in domains:
        if dom not in d:
            results.append(f"❌ {dom}: нет в списке")
            continue
        ips_to_check = set(d[dom])
        del d[dom]
        still_owned = set()
        for v in d.values(): still_owned.update(v)
        to_remove = ips_to_check - still_owned
        routes_call("remove", sorted(to_remove))
        results.append(f"✅ {dom}: убрано {len(to_remove)} IP из extra")
    save(d)
    return "\n".join(results)

def cmd_refresh():
    d = load()
    total_add = 0; total_rm = 0; failed = []
    for dom in list(d.keys()):
        new_ips = resolve(dom)
        if not new_ips:
            failed.append(dom); continue
        old = set(d[dom])
        added = new_ips - old
        removed = old - new_ips
        d[dom] = sorted(new_ips)
        # после обновления — пересобрать карту владельцев
        owners = set()
        for v in d.values(): owners.update(v)
        to_rm = sorted(removed - owners)
        routes_call("add", sorted(added))
        routes_call("remove", to_rm)
        total_add += len(added)
        total_rm += len(to_rm)
    save(d)
    msg = f"обновлено: +{total_add} / -{total_rm} IP"
    if failed: msg += f"\nне резолвятся: {', '.join(failed)}"
    return msg

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: ru-domains {list|show DOM|add DOM...|remove DOM...|refresh}"); sys.exit(1)
    cmd = sys.argv[1]; args = sys.argv[2:]
    if   cmd == "list":       print(cmd_list())
    elif cmd == "show":       print(cmd_show(args[0]) if args else "show DOM")
    elif cmd == "add":        print(cmd_add(args))
    elif cmd in ("remove","del"): print(cmd_remove(args))
    elif cmd == "refresh":    print(cmd_refresh())
    else: print(f"unknown: {cmd}"); sys.exit(1)
