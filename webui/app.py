#!/usr/bin/env python3
"""
Kaskad Web UI - dashboard для управления RU/ам. серверами, доменами и IP.

Шарит логику с TG-ботом: читает /etc/wireguard/ru-servers.json и шеллит
те же скрипты (ru-set.sh, ru-routes.sh, ru-domains.py).
"""
import base64, functools, json, os, re, secrets, shlex, subprocess
from datetime import timedelta
from pathlib import Path

from typing import Optional

from flask import Flask, jsonify, render_template, request, Response, session, redirect, url_for

SERVERS_JSON = Path(os.environ.get("KASKAD_SERVERS_JSON", "/etc/wireguard/ru-servers.json"))
WEBUI_ENV    = Path(os.environ.get("KASKAD_WEBUI_ENV",  "/etc/kaskad/webui.env"))
SESSION_SECRET_FILE = Path(os.environ.get("KASKAD_SESSION_SECRET_FILE", "/etc/kaskad/.session_secret"))
LOCAL_HOST = os.environ.get("LOCAL_HOST", "ams1")
LOCAL_IP = os.environ.get("LOCAL_IP", "127.0.0.1")
BOT_KEY = os.environ.get("KASKAD_SSH_KEY", "/root/.ssh/id_ed25519")
WEB_USER = os.environ.get("KASKAD_WEB_USER", "admin")
WEB_PASS = os.environ.get("KASKAD_WEB_PASS", "")  # обязательно задать в проде!

app = Flask(__name__, template_folder="templates", static_folder="static")
CIDR_RX = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?)\b")


def _session_secret_key() -> str:
    sk = os.environ.get("KASKAD_SECRET_KEY", "").strip()
    if sk:
        return sk
    try:
        if SESSION_SECRET_FILE.exists():
            return SESSION_SECRET_FILE.read_text().strip()
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        SESSION_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not SESSION_SECRET_FILE.exists():
            SESSION_SECRET_FILE.write_text(key)
            SESSION_SECRET_FILE.chmod(0o600)
        else:
            key = SESSION_SECRET_FILE.read_text().strip()
    except OSError:
        pass
    return key


app.secret_key = _session_secret_key()
app.config.update(
    SESSION_COOKIE_NAME="kaskad",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


def _safe_next(url: Optional[str]) -> str:
    if not url:
        return "/"
    url = url.split("#", 1)[0]
    if not url.startswith("/") or url.startswith("//"):
        return "/"
    return url


def _session_ok() -> bool:
    return bool(session.get("kaskad"))


# --- auth (cookie session; Basic Auth в браузере нельзя надёжно сбросить) ---
def require_auth(fn):
    @functools.wraps(fn)
    def w(*a, **kw):
        if not WEB_PASS:
            return Response("KASKAD_WEB_PASS не задан", 500)
        if _session_ok():
            return fn(*a, **kw)
        if request.path.startswith("/api/"):
            return jsonify(error="требуется вход"), 401
        return redirect(url_for("login", next=request.path))
    return w


# --- shell + ssh ---
def shell(cmd, timeout=30, input=None):
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout, input=input)
    return r.stdout.strip(), r.returncode, r.stderr.strip()


def ssh_run(host, cmd, timeout=15, port=22, user="root"):
    if host == LOCAL_IP:
        out, rc, _ = shell(cmd, timeout=timeout)
        return out, rc
    args = ["ssh", "-i", BOT_KEY, "-p", str(port),
            "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
            f"{user}@{host}", cmd]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.returncode


def ssh_pw(host, port, user, password, cmd, timeout=240):
    args = ["sshpass", "-p", password, "ssh",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
            "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
            "-p", str(port), f"{user}@{host}", cmd]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.returncode, r.stderr


def scp_pw(host, port, user, password, files, dest, timeout=60):
    args = ["sshpass", "-p", password, "scp", "-P", str(port),
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
            "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no"]
    if isinstance(files, str): files = [files]
    args += files + [f"{user}@{host}:{dest}"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def scp_key(host, port, files, dest, timeout=60):
    args = ["scp", "-P", str(port), "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15", "-i", BOT_KEY, "-o", "BatchMode=yes"]
    if isinstance(files, str): files = [files]
    args += files + [f"root@{host}:{dest}"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


# --- config ---
def load_data():
    try:
        return json.loads(SERVERS_JSON.read_text())
    except Exception:
        return {"servers": [], "ams_servers": []}


def save_and_distribute(data):
    js = json.dumps(data, indent=2, ensure_ascii=False)
    SERVERS_JSON.write_text(js)
    fail = []
    for a in data.get("ams_servers", []):
        if a.get("is_local"): continue
        proc = subprocess.run(
            ["ssh", "-i", BOT_KEY, "-p", str(a.get("ssh_port", 22)),
             "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             f"root@{a['host']}", f"cat > {SERVERS_JSON} && chmod 600 {SERVERS_JSON}"],
            input=js, text=True, capture_output=True, timeout=10)
        if proc.returncode != 0:
            fail.append(f"{a['id']}: {proc.stderr.strip()}")
    return (len(fail) == 0), ("; ".join(fail) if fail else "OK")


def ams_list_data(): return load_data().get("ams_servers", [])
def ru_list_data(): return sorted(load_data().get("servers", []), key=lambda x: x["priority"])


def ssh_ams(a, cmd, timeout=15):
    if a.get("is_local") or a["host"] == LOCAL_IP:
        return shell(cmd, timeout=timeout)[:2]
    return ssh_run(a["host"], cmd, timeout=timeout, port=a.get("ssh_port", 22))


def ssh_ru(s, cmd, timeout=15):
    return ssh_run(s["host"], cmd, timeout=timeout,
                   port=s.get("ssh_port", 22), user=s.get("ssh_user", "root"))


# --- queries ---
QUERY_CMD = (
    "ep=$(grep ^Endpoint /etc/wireguard/ru.conf | awk '{print $3}'); "
    "hs=$(wg show ru latest-handshakes 2>/dev/null | head -1 | awk '{print $2}'); "
    "now=$(date +%s); age=$((now-${hs:-0})); "
    "[ \"${hs:-0}\" -eq 0 ] && age=999999; "
    "echo \"$ep|$age\""
)


def label_for(ep):
    for s in ru_list_data():
        if s["endpoint"] == ep or s["host"] in ep:
            return f"{s['id']} ({s['label']})"
    return ep


def get_status():
    out = []
    for a in ams_list_data():
        line = {"id": a["id"], "host": a["host"], "tunnel_ip": a["tunnel_ip"]}
        o, rc = ssh_ams(a, QUERY_CMD)
        if rc != 0:
            line["state"] = "unreachable"
        else:
            try:
                ep, age = o.split("|")
                line["endpoint"] = ep
                line["label"] = label_for(ep)
                line["handshake_age"] = int(age) if int(age) < 99999 else None
                line["state"] = "ok"
            except Exception:
                line["state"] = "parse_error"
                line["raw"] = o
        out.append(line)
    return out


# --- API ---
@app.route("/api/state")
@require_auth
def api_state():
    data = load_data()
    extra_ips = []
    out, _, _ = shell("/usr/local/bin/ru-routes.sh list")
    if out and out.strip() != "(пусто)":
        extra_ips = [l.strip() for l in out.splitlines() if l.strip()]
    base_aips, _, _ = shell("cat /etc/wireguard/ru-base.aips 2>/dev/null || true")
    base = [x.strip() for x in (base_aips or "").split(",") if x.strip()]

    domains = {}
    out, rc, _ = shell("/usr/local/bin/ru-domains.py list")
    if rc == 0 and out.strip() != "(пусто)":
        for line in out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                try: domains[k.strip()] = int(v.strip().split()[0])
                except: pass

    return jsonify({
        "ru_servers": ru_list_data(),
        "ams_servers": ams_list_data(),
        "status": get_status(),
        "extra_ips": extra_ips,
        "base_ips": base,
        "domains": domains,
    })


@app.route("/api/use", methods=["POST"])
@require_auth
def api_use():
    sid = (request.json or {}).get("id", "").strip()
    if not sid: return jsonify(error="id required"), 400
    if not any(s["id"] == sid for s in ru_list_data()):
        return jsonify(error=f"unknown server: {sid}"), 404
    results = []
    for a in ams_list_data():
        out, rc = ssh_ams(a, f"/usr/local/bin/ru-set.sh {shlex.quote(sid)}")
        results.append({"ams": a["id"], "ok": rc == 0, "msg": out})
    return jsonify(results=results)


@app.route("/api/server", methods=["POST"])
@require_auth
def api_server_add():
    body = request.json or {}
    host = body.get("host"); user = body.get("user", "root"); ssh_port = body.get("ssh_port", 22)
    sid = body.get("id"); priority = int(body.get("priority", 99))
    label = body.get("label", host); listen_port = int(body.get("listen_port", 1939))
    probe_port = int(body.get("probe_port", ssh_port)); password = body.get("password")
    if not (host and sid): return jsonify(error="host и id обязательны"), 400

    data = load_data()
    if any(s["id"] == sid for s in data["servers"]):
        return jsonify(error=f"id '{sid}' уже есть"), 409

    bot_key_pub, _, _ = shell(f"cat {BOT_KEY}.pub")
    bot_key_b64 = base64.b64encode(bot_key_pub.encode()).decode()
    helper = "/usr/local/bin/add-ru-helper.sh"
    if not Path(helper).exists():
        return jsonify(error=f"нет {helper}"), 500

    if password:
        ok, err = scp_pw(host, ssh_port, user, password, helper, "/tmp/add-ru-helper.sh")
    else:
        ok, err = scp_key(host, ssh_port, helper, "/tmp/add-ru-helper.sh")
    if not ok: return jsonify(error=f"scp: {err[:500]}"), 500

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
        return jsonify(error=f"helper не отработал: {full[-1000:]}"), 500
    res = {}
    in_b = False
    for line in full.splitlines():
        if line == "----RESULT----": in_b = True; continue
        if line == "----END----": in_b = False; continue
        if in_b and "=" in line:
            k, v = line.split("=", 1); res[k] = v.strip()
    pubkey = res.get("PUBKEY")
    if not pubkey: return jsonify(error="pubkey не получен"), 500

    new = {
        "id": sid, "host": host, "endpoint": f"{host}:{listen_port}",
        "pubkey": pubkey, "probe_port": probe_port, "priority": priority, "label": label,
        "ssh_user": "root", "ssh_port": int(ssh_port),
        "wg_iface": res.get("IFACE", "ens18"),
    }
    data["servers"].append(new)
    ok, err = save_and_distribute(data)
    if not ok: return jsonify(error=f"sync: {err}"), 500
    return jsonify(server=new, public_ip=res.get("PUBLIC_IP"))


@app.route("/api/server/<sid>", methods=["DELETE"])
@require_auth
def api_server_remove(sid):
    data = load_data()
    before = len(data["servers"])
    data["servers"] = [s for s in data["servers"] if s["id"] != sid and s["host"] != sid]
    if len(data["servers"]) == before: return jsonify(error="не найден"), 404
    if not data["servers"]: return jsonify(error="последний сервер, отказ"), 400
    save_and_distribute(data)
    return jsonify(ok=True)


@app.route("/api/ams", methods=["POST"])
@require_auth
def api_ams_add():
    body = request.json or {}
    host = body.get("host"); user = body.get("user", "root")
    ssh_port = int(body.get("ssh_port", 22)); sid = body.get("id")
    xray_iface = body.get("xray_iface", "amn0"); password = body.get("password")
    requested_tunnel = body.get("tunnel_ip")
    if not (host and sid): return jsonify(error="host и id обязательны"), 400

    data = load_data()
    if any(a["id"] == sid or a["host"] == host for a in data.get("ams_servers", [])):
        return jsonify(error=f"id или host уже есть"), 409
    used = {a["tunnel_ip"] for a in data.get("ams_servers", [])} | {"10.0.0.1"}
    if requested_tunnel:
        if requested_tunnel in used: return jsonify(error=f"{requested_tunnel} занят"), 409
        tunnel_ip = requested_tunnel
    else:
        tunnel_ip = next((f"10.0.0.{i}" for i in range(2, 255) if f"10.0.0.{i}" not in used), None)
        if not tunnel_ip: return jsonify(error="нет свободных tunnel IP"), 500

    rus = ru_list_data()
    if not rus: return jsonify(error="нет RU-серверов в конфиге"), 500
    primary = rus[0]

    bot_key_pub, _, _ = shell(f"cat {BOT_KEY}.pub")
    bot_key_b64 = base64.b64encode(bot_key_pub.encode()).decode()
    helper_local = "/usr/local/bin/add-ams-helper.sh"
    if password:
        ok, err = scp_pw(host, ssh_port, user, password, helper_local, "/tmp/add-ams-helper.sh")
    else:
        ok, err = scp_key(host, ssh_port, helper_local, "/tmp/add-ams-helper.sh")
    if not ok: return jsonify(error=f"scp helper: {err[:500]}"), 500

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
        return jsonify(error=f"helper failed: {full[-1000:]}"), 500

    out_pk, rc = ssh_run(host, "test -f /etc/wireguard/ru_private.key || (umask 077 && wg genkey | tee /etc/wireguard/ru_private.key | wg pubkey > /etc/wireguard/ru_public.key); cat /etc/wireguard/ru_public.key", timeout=15, port=ssh_port)
    if rc != 0: return jsonify(error=f"key gen: {out_pk}"), 500
    new_pubkey = out_pk.strip()

    files = ["/usr/local/bin/ru-failover.py", "/usr/local/bin/ru-set.sh",
             "/usr/local/bin/ru-routes.sh", "/usr/local/bin/ru-domains.py",
             "/etc/wireguard/notify.env", str(SERVERS_JSON)]
    ok, err = scp_key(host, ssh_port, files, "/tmp/", timeout=30)
    if not ok: return jsonify(error=f"scp scripts: {err[:500]}"), 500

    sga1_conf, _, _ = shell("cat /etc/wireguard/ru.conf")
    sga1_base, _, _ = shell("cat /etc/wireguard/ru-base.aips 2>/dev/null || true")
    new_conf = re.sub(r'(?m)^Address *=.*$', f'Address = {tunnel_ip}/32', sga1_conf, count=1)
    new_conf = re.sub(r'(?m)^PublicKey *=.*$', f'PublicKey = {primary["pubkey"]}', new_conf, count=1)
    new_conf = re.sub(r'(?m)^Endpoint *=.*$', f'Endpoint = {primary["endpoint"]}', new_conf, count=1)
    if xray_iface != "amn0":
        new_conf = new_conf.replace("amn0", xray_iface)

    proc = subprocess.run(
        ["ssh", "-i", BOT_KEY, "-p", str(ssh_port),
         "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
         f"root@{host}", "cat > /etc/wireguard/ru.conf && chmod 600 /etc/wireguard/ru.conf"],
        input=new_conf, text=True, capture_output=True, timeout=15)
    if proc.returncode != 0:
        return jsonify(error=f"write ru.conf: {proc.stderr.strip()}"), 500

    if sga1_base:
        subprocess.run(
            ["ssh", "-i", BOT_KEY, "-p", str(ssh_port),
             "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
             f"root@{host}", "cat > /etc/wireguard/ru-base.aips && chmod 600 /etc/wireguard/ru-base.aips"],
            input=sga1_base, text=True, capture_output=True, timeout=10)

    install_cmd = """
install -m 755 /tmp/ru-failover.py /usr/local/bin/ru-failover.py
install -m 755 /tmp/ru-set.sh      /usr/local/bin/ru-set.sh
install -m 755 /tmp/ru-routes.sh   /usr/local/bin/ru-routes.sh
install -m 755 /tmp/ru-domains.py  /usr/local/bin/ru-domains.py
install -m 600 /tmp/notify.env     /etc/wireguard/notify.env
install -m 600 /tmp/ru-servers.json /etc/wireguard/ru-servers.json
touch /etc/wireguard/ru-extra.list && chmod 600 /etc/wireguard/ru-extra.list
touch /etc/wireguard/ru-domains.json && chmod 600 /etc/wireguard/ru-domains.json
[ -s /etc/wireguard/ru-domains.json ] || echo '{}' > /etc/wireguard/ru-domains.json
( crontab -l 2>/dev/null | grep -v 'ru-failover\\|ru-domains' ; echo '* * * * * /usr/local/bin/ru-failover.py' ; echo '17 */6 * * * /usr/local/bin/ru-domains.py refresh >> /var/log/ru-domains.log 2>&1' ) | crontab -
wg-quick down ru 2>/dev/null || true
wg-quick up ru 2>&1 | tail -5
systemctl enable wg-quick@ru 2>&1 | tail -1
"""
    out_inst, rc = ssh_run(host, install_cmd, timeout=60, port=ssh_port)
    if rc != 0: return jsonify(error=f"install: {out_inst[-500:]}"), 500

    peer_block = f"\n[Peer]\n# {sid}\nPublicKey = {new_pubkey}\nAllowedIPs = {tunnel_ip}/32\n"
    peer_results = []
    for ru in rus:
        cmd = (f"if grep -qF '{new_pubkey}' /etc/wireguard/wg_ru.conf; then echo 'already'; "
               f"else printf '%s' {shlex.quote(peer_block)} >> /etc/wireguard/wg_ru.conf; fi; "
               f"wg syncconf wg_ru <(wg-quick strip wg_ru) 2>&1")
        out_pr, rc_pr = ssh_ru(ru, cmd, timeout=20)
        peer_results.append({"ru": ru["id"], "ok": rc_pr == 0, "msg": (out_pr.splitlines()[-1] if out_pr else "")})

    data["ams_servers"].append({
        "id": sid, "host": host, "ssh_port": int(ssh_port),
        "tunnel_ip": tunnel_ip, "pubkey": new_pubkey, "xray_iface": xray_iface,
    })
    save_and_distribute(data)
    return jsonify(ams={"id": sid, "host": host, "tunnel_ip": tunnel_ip, "pubkey": new_pubkey},
                   peers=peer_results)


@app.route("/api/ams/<sid>", methods=["DELETE"])
@require_auth
def api_ams_remove(sid):
    """Удалить зарубежный (ам.) сервер из каскада.

    Шаги:
      1) на каждом RU-сервере убрать [Peer] с pubkey удаляемого ам.
      2) обновить /etc/wireguard/ru-servers.json и разлить по остальным ам.
      3) синхронизировать живой wg-интерфейс на RU через `wg set ... peer ... remove`

    ?force=1 — продолжить, даже если какой-то RU недоступен.
    """
    force = request.args.get("force") in ("1", "true", "yes")
    data = load_data()
    target = next((a for a in data.get("ams_servers", [])
                   if a["id"] == sid or a["host"] == sid), None)
    if not target:
        return jsonify(error=f"ам. сервер '{sid}' не найден"), 404
    if target.get("is_local"):
        return jsonify(error="нельзя удалить сервер, на котором запущен WebUI"), 400

    pubkey = target["pubkey"]
    rus = ru_list_data()
    peer_results, hard_fail = [], []

    for ru in rus:
        cmd = (
            f"set -e; cd /etc/wireguard; "
            f"cp -a wg_ru.conf wg_ru.conf.bak.$(date +%s); "
            f"python3 -c \"import pathlib,re,sys; "
            f"p=pathlib.Path('wg_ru.conf'); t=p.read_text(); "
            f"blocks=re.split(r'(?m)(?=^\\[Peer\\])', t); "
            f"kept=[b for b in blocks if {pubkey!r} not in b]; "
            f"p.write_text(''.join(kept))\"; "
            f"wg set wg_ru peer {shlex.quote(pubkey)} remove 2>/dev/null || true; "
            f"echo OK"
        )
        out_pr, rc_pr = ssh_ru(ru, cmd, timeout=20)
        ok = (rc_pr == 0 and "OK" in (out_pr or ""))
        peer_results.append({"ru": ru["id"], "ok": ok, "msg": (out_pr or "")[-200:]})
        if not ok:
            hard_fail.append(f"{ru['id']}: rc={rc_pr} {(out_pr or '')[-120:]}")

    if hard_fail and not force:
        return jsonify(
            error=("не удалось убрать peer на: " + "; ".join(hard_fail) +
                   ". Повторите с ?force=1 чтобы удалить запись принудительно."),
            peers=peer_results,
        ), 502

    data["ams_servers"] = [a for a in data["ams_servers"] if a["id"] != target["id"]]
    ok, err = save_and_distribute(data)
    if not ok and not force:
        return jsonify(error=f"sync конфига: {err}", peers=peer_results), 500
    return jsonify(removed=target["id"], peers=peer_results, force=force)


@app.route("/api/domains", methods=["POST"])
@require_auth
def api_domains_add():
    domains = (request.json or {}).get("domains", [])
    if not domains: return jsonify(error="domains обязателен"), 400
    args = " ".join(shlex.quote(d) for d in domains)
    cmd = f"/usr/local/bin/ru-domains.py add {args}"
    timeout = max(60, len(domains) * 5)
    import concurrent.futures as cf
    results = []
    def run(a):
        return a["id"], ssh_ams(a, cmd, timeout=timeout)
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for fut in cf.as_completed([ex.submit(run, a) for a in ams_list_data()]):
            try:
                aid, (out, rc) = fut.result()
                ok = sum(1 for l in (out or "").splitlines() if l.startswith("✅"))
                bad = sum(1 for l in (out or "").splitlines() if l.startswith("❌"))
                results.append({"ams": aid, "ok": rc == 0, "added": ok, "failed": bad, "raw": out})
            except Exception as e:
                results.append({"ams": "?", "ok": False, "msg": str(e)})
    return jsonify(results=results)


@app.route("/api/domains", methods=["DELETE"])
@require_auth
def api_domains_remove():
    domains = (request.json or {}).get("domains", [])
    if not domains: return jsonify(error="domains обязателен"), 400
    args = " ".join(shlex.quote(d) for d in domains)
    cmd = f"/usr/local/bin/ru-domains.py remove {args}"
    results = []
    for a in ams_list_data():
        out, rc = ssh_ams(a, cmd, timeout=30)
        results.append({"ams": a["id"], "ok": rc == 0, "msg": out})
    return jsonify(results=results)


@app.route("/api/domains/refresh", methods=["POST"])
@require_auth
def api_domains_refresh():
    results = []
    for a in ams_list_data():
        out, rc = ssh_ams(a, "/usr/local/bin/ru-domains.py refresh", timeout=600)
        results.append({"ams": a["id"], "ok": rc == 0, "msg": out})
    return jsonify(results=results)


@app.route("/api/ips", methods=["POST"])
@require_auth
def api_ips_add():
    raw = (request.json or {}).get("ips", [])
    if isinstance(raw, str):
        ips = CIDR_RX.findall(raw)
    else:
        ips = []
        for x in raw: ips += CIDR_RX.findall(x)
    if not ips: return jsonify(error="не нашёл IP/CIDR"), 400
    args = " ".join(shlex.quote(i) for i in ips)
    cmd = f"/usr/local/bin/ru-routes.sh add {args}"
    results = []
    for a in ams_list_data():
        out, rc = ssh_ams(a, cmd, timeout=20)
        results.append({"ams": a["id"], "ok": rc == 0, "msg": out})
    return jsonify(parsed=ips, results=results)


@app.route("/api/ips", methods=["DELETE"])
@require_auth
def api_ips_remove():
    raw = (request.json or {}).get("ips", [])
    if isinstance(raw, str):
        ips = CIDR_RX.findall(raw)
    else:
        ips = []
        for x in raw: ips += CIDR_RX.findall(x)
    if not ips: return jsonify(error="не нашёл IP/CIDR"), 400
    args = " ".join(shlex.quote(i) for i in ips)
    cmd = f"/usr/local/bin/ru-routes.sh remove {args}"
    results = []
    for a in ams_list_data():
        out, rc = ssh_ams(a, cmd, timeout=20)
        results.append({"ams": a["id"], "ok": rc == 0, "msg": out})
    return jsonify(results=results)


@app.route("/api/ips/clear", methods=["POST"])
@require_auth
def api_ips_clear():
    results = []
    for a in ams_list_data():
        out, rc = ssh_ams(a, "/usr/local/bin/ru-routes.sh clear", timeout=20)
        results.append({"ams": a["id"], "ok": rc == 0, "msg": out})
    return jsonify(results=results)


@app.route("/api/auth/whoami")
@require_auth
def api_whoami():
    return jsonify(user=WEB_USER)


@app.route("/api/auth/password", methods=["POST"])
@require_auth
def api_change_password():
    """Сменить пароль администратора.

    Перезаписывает строку KASKAD_WEB_PASS=... в /etc/kaskad/webui.env и
    обновляет переменную в памяти процесса, чтобы НЕ требовался рестарт.
    """
    global WEB_PASS
    body = request.json or {}
    current = (body.get("current") or "").strip()
    new = (body.get("new") or "").strip()
    if not new or len(new) < 8:
        return jsonify(error="новый пароль должен быть не короче 8 символов"), 400
    if current != WEB_PASS:
        return jsonify(error="текущий пароль неверный"), 403
    if new == current:
        return jsonify(error="новый пароль совпадает со старым"), 400
    if not WEBUI_ENV.exists():
        return jsonify(error=f"нет файла {WEBUI_ENV} — сменить пароль вручную невозможно"), 500

    try:
        lines = WEBUI_ENV.read_text().splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.startswith("KASKAD_WEB_PASS="):
                lines[i] = f"KASKAD_WEB_PASS={new}"
                found = True
                break
        if not found:
            lines.append(f"KASKAD_WEB_PASS={new}")
        WEBUI_ENV.write_text("\n".join(lines) + "\n")
        WEBUI_ENV.chmod(0o600)
    except Exception as e:
        return jsonify(error=f"запись {WEBUI_ENV}: {e}"), 500

    WEB_PASS = new
    return jsonify(ok=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    """Форма входа; сессия Flask (cookie), не HTTP Basic."""
    if _session_ok():
        return redirect(_safe_next(request.args.get("next")))
    next_url = _safe_next(request.args.get("next", "/"))
    if request.method == "POST":
        next_url = _safe_next(request.form.get("next"))
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if u == WEB_USER and p == WEB_PASS:
            session.clear()
            session["kaskad"] = True
            session.permanent = True
            return redirect(next_url)
        return render_template("login.html", error="Неверный логин или пароль", next_url=next_url)
    return render_template("login.html", next_url=next_url)


@app.route("/logout")
def logout_page():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.route("/")
@require_auth
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host=os.environ.get("KASKAD_HOST", "0.0.0.0"),
            port=int(os.environ.get("KASKAD_PORT", "8088")),
            debug=False)
