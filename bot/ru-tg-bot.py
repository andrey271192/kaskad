#!/usr/bin/env python3
"""TG-бот: управляет N RU-серверов и M ам. серверов через ru-servers.json."""
import base64, json, os, re, shlex, socket, subprocess, time
from pathlib import Path
from urllib import parse, request, error as urlerror

TOKEN   = os.environ["TG_BOT_TOKEN"]
ALLOWED = os.environ.get("TG_CHAT_ID", "").strip()
API     = f"https://api.telegram.org/bot{TOKEN}"

LOCAL_HOST = os.environ.get("LOCAL_HOST", "sga1")
LOCAL_IP   = os.environ.get("LOCAL_IP",   "127.0.0.1")
SERVERS_JSON = "/etc/wireguard/ru-servers.json"

CIDR_RX = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?)\b")

# --- TG ---
def tg_post(method, params):
    body = parse.urlencode(params).encode()
    try:
        with request.urlopen(request.Request(f"{API}/{method}", data=body), timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "err": str(e)}

def tg_get(method, params):
    url = f"{API}/{method}?{parse.urlencode(params)}"
    with request.urlopen(url, timeout=40) as r:
        return json.loads(r.read())

# --- shell ---
def shell(cmd, timeout=15, input=None):
    r = subprocess.run(["bash","-c",cmd], capture_output=True, text=True, timeout=timeout, input=input)
    return r.stdout.strip(), r.returncode, r.stderr.strip()

def ssh_run(host, cmd, timeout=15, port=22, user="root", key="/root/.ssh/id_ed25519", input=None):
    if host == LOCAL_IP:
        out, rc, _ = shell(cmd, timeout=timeout, input=input)
        return out, rc
    args = ["ssh","-i",key,"-p",str(port),
            "-o","StrictHostKeyChecking=no","-o","ConnectTimeout=5","-o","BatchMode=yes",
            f"{user}@{host}", cmd]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input)
    return r.stdout.strip(), r.returncode

def ssh_pw(host, port, user, password, cmd, timeout=180, sudo_pass=None):
    args = ["sshpass","-p",password,"ssh",
            "-o","StrictHostKeyChecking=accept-new","-o","ConnectTimeout=15",
            "-o","PreferredAuthentications=password","-o","PubkeyAuthentication=no",
            "-p",str(port), f"{user}@{host}", cmd]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.returncode, r.stderr

def scp_pw(host, port, user, password, files, dest, timeout=60):
    args = ["sshpass","-p",password,"scp","-P",str(port),
            "-o","StrictHostKeyChecking=accept-new","-o","ConnectTimeout=15",
            "-o","PreferredAuthentications=password","-o","PubkeyAuthentication=no"]
    if isinstance(files, str): files = [files]
    args += files + [f"{user}@{host}:{dest}"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stderr or r.stdout).strip()

def scp_key(host, port, files, dest, timeout=60, key="/root/.ssh/id_ed25519"):
    args = ["scp","-P",str(port),
            "-o","StrictHostKeyChecking=no","-o","ConnectTimeout=15",
            "-i",key,"-o","BatchMode=yes"]
    if isinstance(files, str): files = [files]
    args += files + [f"root@{host}:{dest}"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stderr or r.stdout).strip()

# --- helpers ---
def fmt_age(s):
    try: s = int(s)
    except: return "?"
    if s >= 86400: return f"{s//86400}d"
    if s >= 3600:  return f"{s//3600}h"
    if s >= 60:    return f"{s//60}m"
    return f"{s}s"

def load_data():
    try: return json.loads(Path(SERVERS_JSON).read_text())
    except: return {"servers": [], "ams_servers": []}

def save_and_distribute(data):
    js = json.dumps(data, indent=2, ensure_ascii=False)
    Path(SERVERS_JSON).write_text(js)
    fail = []
    for a in data.get("ams_servers", []):
        if a.get("is_local"): continue
        proc = subprocess.run(
            ["ssh","-i","/root/.ssh/id_ed25519","-p",str(a.get("ssh_port",22)),
             "-o","StrictHostKeyChecking=no","-o","ConnectTimeout=5","-o","BatchMode=yes",
             f"root@{a['host']}", f"cat > {SERVERS_JSON} && chmod 600 {SERVERS_JSON}"],
            input=js, text=True, capture_output=True, timeout=10)
        if proc.returncode != 0: fail.append(f"{a['id']}: {proc.stderr.strip()}")
    return (len(fail) == 0), ("; ".join(fail) if fail else "OK")

def ams_list_data(): return load_data().get("ams_servers", [])
def ru_list_data():  return sorted(load_data().get("servers", []), key=lambda x: x["priority"])

def ssh_ams(a, cmd, timeout=15):
    if a.get("is_local") or a["host"] == LOCAL_IP:
        return shell(cmd, timeout=timeout)[:2]
    return ssh_run(a["host"], cmd, timeout=timeout, port=a.get("ssh_port",22))

def ssh_ru(s, cmd, timeout=15):
    return ssh_run(s["host"], cmd, timeout=timeout,
                   port=s.get("ssh_port", 22), user=s.get("ssh_user", "root"))

def label_for(ep):
    for s in ru_list_data():
        if s["endpoint"] == ep or s["host"] in ep:
            return f"{s['id']} ({s['label']})"
    return ep

QUERY_CMD = (
    "ep=$(grep ^Endpoint /etc/wireguard/ru.conf | awk '{print $3}'); "
    "hs=$(wg show ru latest-handshakes 2>/dev/null | head -1 | awk '{print $2}'); "
    "now=$(date +%s); age=$((now-${hs:-0})); "
    "[ \"${hs:-0}\" -eq 0 ] && age=999999; "
    "echo \"$ep|$age\""
)

def status():
    ams = ams_list_data()
    if not ams: return "Нет ам. серверов в конфиге"
    lines = ["📊 Туннели:"]
    for a in ams:
        out, rc = ssh_ams(a, QUERY_CMD)
        if rc != 0:
            lines.append(f"❌ {a['id']}: недоступен")
            continue
        try:
            ep, age = out.split("|")
            label = label_for(ep)
            icon = "🟢" if "primary" in label else ("🟡" if "backup" in label else "🔵")
            lines.append(f"{icon} {a['id']}: {label}, hs {fmt_age(age)}")
        except Exception:
            lines.append(f"• {a['id']}: {out}")
    return "\n".join(lines)

def force_all(server_id):
    ru = next((s for s in ru_list_data() if s["id"] == server_id), None)
    if not ru:
        ids = ", ".join(s["id"] for s in ru_list_data())
        return f"❌ нет RU-сервера '{server_id}'. Доступны: {ids}"
    lines = [f"⚙️ Все ам. на {ru['id']} ({ru['label']}):"]
    for a in ams_list_data():
        out, rc = ssh_ams(a, f"/usr/local/bin/ru-set.sh {shlex.quote(server_id)}")
        lines.append(f"• {a['id']}: {'✅' if rc == 0 else '❌'} {out}")
    return "\n".join(lines)

# --- RU servers ---
def server_list():
    rs = ru_list_data()
    if not rs: return "❌ нет RU"
    lines = ["🌐 RU-серверы (по приоритету):"]
    for s in rs:
        lines.append(f"  [{s['priority']}] {s['id']} — {s['label']} {s['endpoint']} probe:{s['probe_port']} ssh:{s.get('ssh_user','?')}@{s['host']}:{s.get('ssh_port','?')}")
    return "\n".join(lines)

def parse_kv(parts):
    out = {}; positional = []
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1); out[k] = v
        else:
            positional.append(p)
    return positional, out

def get_bot_pubkey():
    out, _, _ = shell("cat /root/.ssh/id_ed25519.pub")
    return out

def server_add(body):
    parts = body.split()
    pos, kv = parse_kv(parts)
    if len(pos) < 4:
        return ("Использование:\n"
                "/server-add <host> <user> <ssh_port> <id> [prio] [password=PW] [label=...] [listen_port=1939] [probe_port=ssh_port]")
    host, user, ssh_port, sid = pos[:4]
    priority = int(pos[4]) if len(pos) > 4 and pos[4].isdigit() else 99
    label = kv.get("label", host)
    listen_port = int(kv.get("listen_port", 1939))
    probe_port  = int(kv.get("probe_port", ssh_port))
    password    = kv.get("password")

    data = load_data()
    if any(s["id"] == sid for s in data["servers"]):
        return f"❌ id '{sid}' уже есть"

    bot_key_b64 = base64.b64encode(get_bot_pubkey().encode()).decode()
    helper = "/usr/local/bin/add-ru-helper.sh"
    if not Path(helper).exists(): return f"❌ нет {helper}"

    # scp
    if password:
        ok, err = scp_pw(host, ssh_port, user, password, helper, "/tmp/add-ru-helper.sh")
    else:
        ok, err = scp_key(host, ssh_port, helper, "/tmp/add-ru-helper.sh")
    if not ok: return f"❌ scp: {err[:500]}"

    # подготовить аргументы хелпера: ams pubkeys + tunnel IPs
    args = [bot_key_b64, str(listen_port)]
    for a in sorted(data.get("ams_servers", []), key=lambda x: x["tunnel_ip"]):
        args += [a["pubkey"], a["tunnel_ip"]]
    arg_str = " ".join(shlex.quote(x) for x in args)
    sudo_p = ""
    if user != "root":
        sudo_p = f"echo {shlex.quote(password or '')} | sudo -S -p '' " if password else "sudo -n "
    cmd = f"chmod +x /tmp/add-ru-helper.sh && {sudo_p}bash /tmp/add-ru-helper.sh {arg_str}"

    if password:
        out, rc, err = ssh_pw(host, ssh_port, user, password, cmd, timeout=240)
    else:
        out, rc = ssh_run(host, cmd, timeout=240, port=ssh_port, user=user); err = ""
    full = (out or "") + (err or "")
    if "----RESULT----" not in full:
        return f"❌ helper не отработал:\n{full[-1500:]}"
    res = {}
    in_b = False
    for line in full.splitlines():
        if line == "----RESULT----": in_b = True; continue
        if line == "----END----":    in_b = False; continue
        if in_b and "=" in line:
            k, v = line.split("=", 1); res[k] = v.strip()
    pubkey = res.get("PUBKEY")
    if not pubkey: return f"❌ pubkey не получен:\n{full[-800:]}"

    new = {
        "id": sid, "host": host, "endpoint": f"{host}:{listen_port}",
        "pubkey": pubkey, "probe_port": probe_port, "priority": priority, "label": label,
        "ssh_user": "root", "ssh_port": int(ssh_port),
        "wg_iface": res.get("IFACE", "ens18"),
    }
    data["servers"].append(new)
    ok, err = save_and_distribute(data)
    if not ok: return f"❌ JSON sync: {err}"

    warn = "" if probe_tcp(host, probe_port) else f"\n⚠ TCP {host}:{probe_port} закрыт — failover не сможет проверять"
    return (f"✅ RU '{sid}' ({label}) добавлен\n"
            f"  endpoint: {host}:{listen_port}\n"
            f"  pubkey: {pubkey}\n"
            f"  priority: {priority}{warn}")

def server_remove(arg):
    arg = arg.strip()
    if not arg: return "Использование: /server-remove <id|host>"
    data = load_data()
    before = len(data["servers"])
    data["servers"] = [s for s in data["servers"] if s["id"] != arg and s["host"] != arg]
    if len(data["servers"]) == before: return f"❌ '{arg}' не найден"
    if not data["servers"]: return "❌ это последний RU, отказ"
    ok, err = save_and_distribute(data)
    return f"✅ '{arg}' удалён" if ok else f"❌ sync: {err}"

# --- AMS servers ---
def ams_list():
    a = ams_list_data()
    if not a: return "❌ нет ам. серверов"
    lines = ["🛰 Ам. серверы:"]
    for x in a:
        local = " (local)" if x.get("is_local") else ""
        lines.append(f"  {x['id']}{local} — {x['host']}:{x.get('ssh_port',22)} tunnel:{x['tunnel_ip']}")
    return "\n".join(lines)

def probe_tcp(host, port, timeout=3):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout): return True
    except Exception: return False

def add_peer_to_ru(ru, peer_id, peer_pk, tunnel_ip):
    """SSH в RU, дописать [Peer] в wg_ru.conf и syncconf."""
    block = f"\n[Peer]\n# {peer_id}\nPublicKey = {peer_pk}\nAllowedIPs = {tunnel_ip}/32\n"
    cmd = f"""
if grep -qF '{peer_pk}' /etc/wireguard/wg_ru.conf; then
  echo 'peer already present'
else
  printf '%s' {shlex.quote(block)} >> /etc/wireguard/wg_ru.conf
fi
wg syncconf wg_ru <(wg-quick strip wg_ru) 2>&1
"""
    return ssh_ru(ru, cmd, timeout=20)

def remove_peer_from_ru(ru, peer_pk):
    """Удалить [Peer] секцию по pubkey."""
    cmd = f"""
python3 - <<'PY'
import pathlib, re
p = pathlib.Path('/etc/wireguard/wg_ru.conf')
t = p.read_text()
# делим на блоки по [Peer], удаляем тот, где совпадает pubkey
parts = re.split(r'(\\[Peer\\])', t)
result = parts[0]
i = 1
while i < len(parts):
    block = parts[i] + (parts[i+1] if i+1 < len(parts) else '')
    if {peer_pk!r} in block:
        i += 2; continue
    result += block
    i += 2
p.write_text(result)
PY
wg syncconf wg_ru <(wg-quick strip wg_ru) 2>&1
"""
    return ssh_ru(ru, cmd, timeout=20)

def ams_add(body):
    parts = body.split()
    pos, kv = parse_kv(parts)
    if len(pos) < 4:
        return ("Использование:\n"
                "/ams-add <host> <user> <ssh_port> <id> [tunnel_ip=auto] [xray_iface=amn0] [password=PW]\n\n"
                "Что делает: ставит WG-клиент, копирует скрипты failover/routes, добавляет пира на ВСЕ RU.")
    host, user, ssh_port, sid = pos[:4]
    xray_iface = kv.get("xray_iface", "amn0")
    password = kv.get("password")

    data = load_data()
    if any(a["id"] == sid or a["host"] == host for a in data.get("ams_servers", [])):
        return f"❌ id '{sid}' или host '{host}' уже есть"

    used = {a["tunnel_ip"] for a in data.get("ams_servers", [])} | {"10.0.0.1"}
    if "tunnel_ip" in kv:
        tunnel_ip = kv["tunnel_ip"]
        if tunnel_ip in used: return f"❌ {tunnel_ip} занят"
    else:
        tunnel_ip = next((f"10.0.0.{i}" for i in range(2, 255) if f"10.0.0.{i}" not in used), None)
        if not tunnel_ip: return "❌ нет свободных tunnel IP"

    rus = ru_list_data()
    if not rus: return "❌ нет RU-серверов"
    primary = rus[0]

    # 1. scp + run helper
    bot_key_b64 = base64.b64encode(get_bot_pubkey().encode()).decode()
    helper_local = "/usr/local/bin/add-ams-helper.sh"
    if password:
        ok, err = scp_pw(host, ssh_port, user, password, helper_local, "/tmp/add-ams-helper.sh")
    else:
        ok, err = scp_key(host, ssh_port, helper_local, "/tmp/add-ams-helper.sh")
    if not ok: return f"❌ scp helper: {err[:500]}"

    sudo_p = ""
    if user != "root":
        sudo_p = f"echo {shlex.quote(password or '')} | sudo -S -p '' " if password else "sudo -n "
    helper_cmd = f"chmod +x /tmp/add-ams-helper.sh && {sudo_p}bash /tmp/add-ams-helper.sh {shlex.quote(bot_key_b64)}"
    if password:
        out, rc, err = ssh_pw(host, ssh_port, user, password, helper_cmd, timeout=180)
    else:
        out, rc = ssh_run(host, helper_cmd, timeout=180, port=ssh_port, user=user); err = ""
    full = (out or "") + (err or "")
    if "----RESULT----" not in full:
        return f"❌ helper не отработал:\n{full[-1200:]}"

    # 2. Сгенерировать ключи на новом ам. через ssh с key auth (теперь должно работать)
    out_pk, rc = ssh_run(host, "test -f /etc/wireguard/ru_private.key || (umask 077 && wg genkey | tee /etc/wireguard/ru_private.key | wg pubkey > /etc/wireguard/ru_public.key); cat /etc/wireguard/ru_public.key", timeout=15, port=ssh_port)
    if rc != 0: return f"❌ key gen: {out_pk}"
    new_pubkey = out_pk.strip()

    # 3. Скопировать скрипты + конфиг
    files = ["/usr/local/bin/ru-failover.py", "/usr/local/bin/ru-set.sh", "/usr/local/bin/ru-routes.sh", "/etc/wireguard/notify.env", "/etc/wireguard/ru-servers.json"]
    # JSON ещё без нового ам. — обновим в конце
    ok, err = scp_key(host, ssh_port, files, "/tmp/", timeout=30)
    if not ok: return f"❌ scp scripts: {err[:500]}"

    # 4. Готовим ru.conf на новом ам. (берём sga1 как шаблон, меняем Address/PublicKey/Endpoint и pubkey пира)
    sga1_conf, _, _ = shell("cat /etc/wireguard/ru.conf")
    sga1_base, _, _ = shell("cat /etc/wireguard/ru-base.aips 2>/dev/null || true")

    # шаблон: заменим Address и Peer-секцию (Endpoint, PublicKey)
    new_conf = re.sub(r'(?m)^Address *=.*$',  f'Address = {tunnel_ip}/32', sga1_conf, count=1)
    new_conf = re.sub(r'(?m)^PublicKey *=.*$', f'PublicKey = {primary["pubkey"]}', new_conf, count=1)
    new_conf = re.sub(r'(?m)^Endpoint *=.*$',  f'Endpoint = {primary["endpoint"]}', new_conf, count=1)
    # PostUp использует amn0 — заменим на xray_iface если другой
    if xray_iface != "amn0":
        new_conf = new_conf.replace("amn0", xray_iface)

    # Положить конфиг и активировать на новом ам.
    proc = subprocess.run(
        ["ssh","-i","/root/.ssh/id_ed25519","-p",str(ssh_port),
         "-o","StrictHostKeyChecking=no","-o","ConnectTimeout=10","-o","BatchMode=yes",
         f"root@{host}",
         f"cat > /etc/wireguard/ru.conf && chmod 600 /etc/wireguard/ru.conf"],
        input=new_conf, text=True, capture_output=True, timeout=15)
    if proc.returncode != 0: return f"❌ write ru.conf: {proc.stderr.strip()}"

    # base.aips
    if sga1_base:
        proc = subprocess.run(
            ["ssh","-i","/root/.ssh/id_ed25519","-p",str(ssh_port),
             "-o","StrictHostKeyChecking=no","-o","ConnectTimeout=10","-o","BatchMode=yes",
             f"root@{host}",
             "cat > /etc/wireguard/ru-base.aips && chmod 600 /etc/wireguard/ru-base.aips"],
            input=sga1_base, text=True, capture_output=True, timeout=10)

    # установить скрипты, ru-extra, cron, поднять туннель
    install_cmd = """
install -m 755 /tmp/ru-failover.py /usr/local/bin/ru-failover.py
install -m 755 /tmp/ru-set.sh      /usr/local/bin/ru-set.sh
install -m 755 /tmp/ru-routes.sh   /usr/local/bin/ru-routes.sh
install -m 600 /tmp/notify.env     /etc/wireguard/notify.env
install -m 600 /tmp/ru-servers.json /etc/wireguard/ru-servers.json
touch /etc/wireguard/ru-extra.list && chmod 600 /etc/wireguard/ru-extra.list
( crontab -l 2>/dev/null | grep -v ru-failover ; echo '* * * * * /usr/local/bin/ru-failover.py' ) | crontab -
wg-quick down ru 2>/dev/null || true
wg-quick up ru 2>&1 | tail -5
systemctl enable wg-quick@ru 2>&1 | tail -1
"""
    out_inst, rc = ssh_run(host, install_cmd, timeout=60, port=ssh_port)
    if rc != 0: return f"❌ install: {out_inst[-500:]}"

    # 5. Добавить пира на КАЖДОМ RU
    peer_results = []
    for ru in rus:
        out_pr, rc_pr = add_peer_to_ru(ru, sid, new_pubkey, tunnel_ip)
        peer_results.append(f"  {ru['id']}: {'✅' if rc_pr == 0 else '❌'} {out_pr.splitlines()[-1] if out_pr else ''}")

    # 6. Обновить JSON ams_servers
    data["ams_servers"].append({
        "id": sid, "host": host, "ssh_port": int(ssh_port),
        "tunnel_ip": tunnel_ip, "pubkey": new_pubkey, "xray_iface": xray_iface,
    })
    ok, err = save_and_distribute(data)

    return (f"✅ ам. сервер '{sid}' добавлен\n"
            f"  host: {host}, tunnel: {tunnel_ip}\n"
            f"  pubkey: {new_pubkey}\n"
            f"Пиры на RU:\n" + "\n".join(peer_results) + "\n\n"
            f"X-ray на этом сервере настраивай сам (туннель уже работает на {primary['label']}).")

def ams_remove(body):
    arg = body.strip()
    if not arg: return "Использование: /ams-remove <id|host>"
    data = load_data()
    target = next((a for a in data.get("ams_servers", []) if a["id"] == arg or a["host"] == arg), None)
    if not target: return f"❌ '{arg}' не найден"
    if target.get("is_local"): return f"❌ '{arg}' — local (бот сам тут живёт), удалить нельзя"

    rus = ru_list_data()
    peer_results = []
    for ru in rus:
        out_pr, rc_pr = remove_peer_from_ru(ru, target["pubkey"])
        peer_results.append(f"  {ru['id']}: {'✅' if rc_pr == 0 else '❌'} {out_pr.splitlines()[-1] if out_pr else ''}")

    data["ams_servers"] = [a for a in data["ams_servers"] if a["id"] != target["id"]]
    save_and_distribute(data)
    return (f"✅ '{target['id']}' ({target['host']}) удалён.\n"
            f"Пиры сняты с RU:\n" + "\n".join(peer_results) + "\n\n"
            f"⚠ Сам сервер не выключен. Если он больше не нужен — отключи руками.")

# --- routes ---
def routes_list():
    out, rc, _ = shell("/usr/local/bin/ru-routes.sh list")
    if rc != 0: return f"❌ {out}"
    return "📜 Доп. маршруты:\n" + out if out.strip() != "(пусто)" else "📜 Доп. маршрутов нет."

def routes_run_all(verb, nets):
    args = " ".join(shlex.quote(n) for n in nets)
    cmd = f"/usr/local/bin/ru-routes.sh {verb} {args}"
    lines = []
    for a in ams_list_data():
        out, rc = ssh_ams(a, cmd, timeout=20)
        lines.append(f"• {a['id']}: {'✅' if rc == 0 else '❌'} {out}")
    return "\n".join(lines)

def cmd_add_routes(body):
    nets = CIDR_RX.findall(body)
    if not nets: return "Не нашёл IP/CIDR"
    return f"➕ Добавляю {len(nets)}:\n" + "\n".join(nets) + "\n\n" + routes_run_all("add", nets)

def cmd_remove_routes(body):
    nets = CIDR_RX.findall(body)
    if not nets: return "Не нашёл IP/CIDR"
    return f"➖ Удаляю {len(nets)}:\n" + "\n".join(nets) + "\n\n" + routes_run_all("remove", nets)

def cmd_clear_routes():
    lines = ["🧹 Очищаю доп. маршруты:"]
    for a in ams_list_data():
        out, rc = ssh_ams(a, "/usr/local/bin/ru-routes.sh clear")
        lines.append(f"• {a['id']}: {'✅' if rc == 0 else '❌'} {out}")
    return "\n".join(lines)

def bot_key():
    out, _, _ = shell("cat /root/.ssh/id_ed25519.pub")
    return ("🔑 Публичный SSH-ключ бота. Добавь в `/root/.ssh/authorized_keys` на новом сервере, чтобы /server-add или /ams-add не требовали пароля:\n\n"
            f"```\nmkdir -p ~/.ssh && echo '{out}' >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys\n```")

def domains_list():
    out, rc, _ = shell("/usr/local/bin/ru-domains.py list")
    if rc != 0: return f"❌ {out}"
    return "🌐 Домены:\n" + out if out.strip() != "(пусто)" else "🌐 Доменов нет."

def domains_show(arg):
    arg = arg.strip()
    if not arg: return "Использование: /show-domain <domain>"
    out, rc, _ = shell(f"/usr/local/bin/ru-domains.py show {shlex.quote(arg)}")
    return out

DOMAIN_RX = re.compile(r"\b[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)+\b")

def parse_domains(body):
    res = []
    for m in DOMAIN_RX.finditer(body):
        d = m.group(0).lower().strip(".")
        # filter pure-IP
        if all(p.isdigit() for p in d.split(".")): continue
        if d not in res: res.append(d)
    return res

def domains_run_all(verb, doms):
    args = " ".join(shlex.quote(d) for d in doms)
    cmd = f"/usr/local/bin/ru-domains.py {verb} {args}"
    lines = []
    for a in ams_list_data():
        out, rc = ssh_ams(a, cmd, timeout=30)
        first_line = out.split("\n", 1)[0] if out else ""
        lines.append(f"• {a[chr(39)+'id'+chr(39)] if False else a['id']}: {'✅' if rc == 0 else '❌'} {first_line}")
    # переходим в простую версию ниже
    return "\n".join(lines)

def cmd_add_domains(body):
    doms = parse_domains(body)
    if not doms: return "Не нашёл домены. Пример: /add-domain vk.com ozon.ru"
    head = f"➕ Добавляю {len(doms)} доменов параллельно на 4 ам. (~3 сек/домен)..."
    args = " ".join(shlex.quote(d) for d in doms)
    cmd = f"/usr/local/bin/ru-domains.py add {args}"
    timeout = max(60, len(doms) * 5)
    import concurrent.futures as cf
    results = {}
    def run(a): return a["id"], ssh_ams(a, cmd, timeout=timeout)
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for fut in cf.as_completed([ex.submit(run, a) for a in ams_list_data()]):
            try:
                aid, (out, rc) = fut.result()
                ok = sum(1 for l in (out or "").splitlines() if l.startswith("✅"))
                bad = sum(1 for l in (out or "").splitlines() if l.startswith("❌"))
                results[aid] = f"{aid}: ✅{ok} ❌{bad}" + (("\n  не резолв: " + ", ".join(l.split(":",1)[0].replace("❌","").strip() for l in (out or "").splitlines() if l.startswith("❌"))) if bad else "")
            except Exception as e:
                results[aid] = f"{aid}: ERR {e}"
    return head + "\n\n" + "\n".join(results.values())

def cmd_remove_domains(body):
    doms = parse_domains(body)
    if not doms: return "Нет доменов для удаления"
    args = " ".join(shlex.quote(d) for d in doms)
    cmd = f"/usr/local/bin/ru-domains.py remove {args}"
    lines = [f"➖ Удаляю {len(doms)} доменов:"]
    for a in ams_list_data():
        out, rc = ssh_ams(a, cmd, timeout=30)
        lines.append(f"--- {a['id']} ---\n" + (out or "..."))
    return "\n\n".join(lines)

def cmd_refresh_domains():
    lines = ["🔄 Refresh доменов:"]
    for a in ams_list_data():
        out, rc = ssh_ams(a, "/usr/local/bin/ru-domains.py refresh", timeout=600)
        lines.append(f"• {a['id']}: {'✅' if rc == 0 else '❌'} {out}")
    return "\n".join(lines)

def cmd_all_ips():
    base, _, _ = shell("cat /etc/wireguard/ru-base.aips 2>/dev/null || true")
    extra, _, _ = shell("/usr/local/bin/ru-routes.sh list")
    base_lines = [x.strip() for x in (base or "").split(",") if x.strip()]
    extra_lines = [] if extra.strip() == "(пусто)" else [x.strip() for x in extra.splitlines() if x.strip()]
    msg = "📋 Все маршруты через ru:\n\n"
    msg += f"🔹 Базовые ({len(base_lines)}):\n" + ("\n".join(base_lines) or "(нет)") + "\n\n"
    msg += f"🔸 Доп. ({len(extra_lines)}):\n" + ("\n".join(extra_lines) or "(нет)")
    return msg

HELP = (
    "📡 *Туннели:*\n"
    "  /status — состояние\n"
    "  /use <id> — все ам. на сервер id\n"
    "  /primary, /backup — алиасы\n"
    "\n"
    "🌐 *RU-серверы:*\n"
    "  /server-list\n"
    "  /server-add <host> <user> <port> <id> [prio] [password=PW]\n"
    "  /server-remove <id|host>\n"
    "\n"
    "🛰 *Ам. серверы:*\n"
    "  /ams-list\n"
    "  /ams-add <host> <user> <port> <id> [tunnel_ip=auto] [xray_iface=amn0] [password=PW]\n"
    "  /ams-remove <id|host>\n"
    "\n"
    "🛣 *Доп. маршруты:*\n"
    "  /ips — все маршруты (база + доп)\n"
    "  /list, /add <IP> | /remove <IP> | /clear\n"
    "  /list-domains, /add-domain <DOM ...>, /remove-domain <DOM ...>, /refresh-domains\n"
    "  /show-domain <DOM> — IP конкретного домена\n"
    "  /list, /add <IP ...>, /remove <IP ...>, /clear\n"
    "\n"
    "🔑 /bot-key — SSH-ключ бота\n"
    "❓ /help"
)

def handle(msg):
    chat_id = msg.get("chat", {}).get("id")
    if ALLOWED and str(chat_id) != ALLOWED: return
    text = (msg.get("text") or "").strip()
    if not text: return
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower(); body = parts[1] if len(parts) > 1 else ""
    reply = None

    if   cmd in ("/status","/start","статус"):  reply = status()
    elif cmd in ("/primary","/failback"):        reply = force_all("primary")
    elif cmd in ("/backup","/failover"):         reply = force_all("backup")
    elif cmd == "/use":                          reply = force_all(body.strip())
    elif cmd in ("/server-list","/servers"):     reply = server_list()
    elif cmd == "/server-add":                   reply = server_add(body)
    elif cmd == "/server-remove":                reply = server_remove(body)
    elif cmd in ("/ams-list","/ams"):            reply = ams_list()
    elif cmd == "/ams-add":                      reply = ams_add(body)
    elif cmd == "/ams-remove":                   reply = ams_remove(body)
    elif cmd == "/bot-key":                      reply = bot_key()
    elif cmd == "/ips":                          reply = cmd_all_ips()
    elif cmd == "/list":                         reply = routes_list()
    elif cmd == "/add":                          reply = cmd_add_routes(body)
    elif cmd in ("/remove","/del","/rm"):        reply = cmd_remove_routes(body)
    elif cmd == "/clear":                        reply = cmd_clear_routes()
    elif cmd in ("/help","помощь"):              reply = HELP

    if reply is not None:
        for i in range(0, len(reply), 4000):
            params = {"chat_id": chat_id, "text": reply[i:i+4000]}
            if "*" in reply or "```" in reply: params["parse_mode"] = "Markdown"
            tg_post("sendMessage", params)

    if "password=" in text:
        msg_id = msg.get("message_id")
        if msg_id: tg_post("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})

def main():
    offset = 0
    while True:
        try:
            data = tg_get("getUpdates", {"offset": offset, "timeout": 30})
            if not data.get("ok"): time.sleep(5); continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if msg: handle(msg)
        except (urlerror.URLError, urlerror.HTTPError, TimeoutError, json.JSONDecodeError):
            time.sleep(5)
        except Exception as e:
            print(f"err: {e}", flush=True); time.sleep(5)

if __name__ == "__main__":
    main()
