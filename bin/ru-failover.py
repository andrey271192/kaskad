#!/usr/bin/env python3
"""N-серверный failover с приоритетами. Конфиг — /etc/wireguard/ru-servers.json."""
import json, os, re, socket, subprocess, time
from pathlib import Path
from urllib import parse, request, error as urlerror

CONF       = Path("/etc/wireguard/ru.conf")
SERVERS    = Path("/etc/wireguard/ru-servers.json")
STATE_DIR  = Path("/var/lib/ru-failover")
NOTIFY_ENV = Path("/etc/wireguard/notify.env")

HS_THRESHOLD = 180
COOLDOWN     = 300
FAIL_BACKOFF = 1800
TEST_TIMEOUT = 60

STATE_DIR.mkdir(parents=True, exist_ok=True)

def state(name, val=None):
    p = STATE_DIR / name
    if val is None:
        try: return int((p.read_text().strip() or "0"))
        except Exception: return 0
    p.write_text(str(val))

def load_servers():
    return sorted(json.loads(SERVERS.read_text())["servers"], key=lambda s: s["priority"])

def cur_endpoint():
    for line in CONF.read_text().splitlines():
        if line.lstrip().startswith("Endpoint"):
            return line.split("=", 1)[1].strip()
    return ""

def hs_age(now):
    try:
        out = subprocess.run(["wg","show","ru","latest-handshakes"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if not out: return 999999
        hs = int(out.split()[1])
        return 999999 if hs == 0 else now - hs
    except Exception:
        return 999999

def probe(host, port, timeout=3):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (socket.error, OSError, ValueError):
        return False

def apply_server(s):
    t = CONF.read_text()
    t = re.sub(r'(?m)^[ \t]*PublicKey *=.*$', f'PublicKey = {s["pubkey"]}', t, count=1)
    t = re.sub(r'(?m)^[ \t]*Endpoint *=.*$',  f'Endpoint = {s["endpoint"]}', t, count=1)
    CONF.write_text(t)
    subprocess.run(["bash","-c","wg syncconf ru <(wg-quick strip ru)"], check=False)
    subprocess.run(["/usr/local/bin/ru-routes.sh","apply"], capture_output=True)
    state("last_switch", int(time.time()))

def find_by_endpoint(servers, ep):
    for s in servers:
        if s["endpoint"] == ep: return s
    return None

def notify(msg):
    subprocess.run(["logger","-t","ru-failover", msg], check=False)
    if not NOTIFY_ENV.exists(): return
    env = {}
    for line in NOTIFY_ENV.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    token = env.get("TG_BOT_TOKEN", "")
    chat  = env.get("TG_CHAT_ID", "")
    if not (token and chat): return
    try:
        host = subprocess.check_output(["hostname"], text=True).strip()
        body = parse.urlencode({"chat_id": chat, "text": f"[ru-failover @ {host}] {msg}"}).encode()
        request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=body, timeout=5).read()
    except (urlerror.URLError, OSError):
        pass

def main():
    servers = load_servers()
    if not servers: return
    now = int(time.time())
    cur = find_by_endpoint(servers, cur_endpoint())
    if not cur:
        # Текущий endpoint не в списке — выставить highest-priority alive
        for s in servers:
            if probe(s["host"], s["probe_port"]):
                apply_server(s)
                notify(f"endpoint {cur_endpoint()} не найден в списке — выставил {s['label']}")
                return
        return

    age          = hs_age(now)
    last_switch  = state("last_switch")
    test_started = state("test_started")
    last_fail    = state("last_fail")

    # Phase 1: failback test
    if test_started:
        elapsed = now - test_started
        if age < 60:
            state("test_started", 0); state("last_fail", 0)
            notify(f"failback на {cur['label']} успешен за {elapsed}s")
        elif elapsed > TEST_TIMEOUT:
            others = [s for s in servers if s["id"] != cur["id"] and probe(s["host"], s["probe_port"])]
            if others:
                apply_server(others[0])
                notify(f"failback на {cur['label']} провалился — откат на {others[0]['label']}")
            state("test_started", 0); state("last_fail", now)
        return

    since_switch = now - last_switch
    since_fail   = now - last_fail

    # Failover: текущий мёртв
    if age > HS_THRESHOLD and not probe(cur["host"], cur["probe_port"]) and since_switch > COOLDOWN:
        others = [s for s in servers if s["id"] != cur["id"] and probe(s["host"], s["probe_port"])]
        if others:
            t = others[0]
            apply_server(t)
            notify(f"{cur['label']} упал (hs {age}s, probe fail) — переключился на {t['label']}")
        return

    # Failback: есть live сервер с приоритетом выше
    higher = [s for s in servers if s["priority"] < cur["priority"] and probe(s["host"], s["probe_port"])]
    if higher and since_switch > COOLDOWN and since_fail > FAIL_BACKOFF:
        t = higher[0]
        apply_server(t)
        state("test_started", now)
        notify(f"{t['label']} поднялся — пробую failback (тест {TEST_TIMEOUT}s)")

if __name__ == "__main__":
    main()
