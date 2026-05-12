#!/usr/bin/env bash
# Kaskad — one-command installer.
# Запускается на ПЕРВОМ ам. (зарубежном) сервере, который станет хостом WebUI и TG-бота.
# Сервер уже должен быть настроен как X-ray-нода (3x-ui или ручной xray) с интерфейсом amn0.
#
#   curl -fsSL https://raw.githubusercontent.com/andrey271192/kaskad/main/install.sh | bash
#
# Или клонируй репо и запусти: sudo bash install.sh
#
# Скрипт интерактивный — задаст 5-6 вопросов и сам всё развернёт.

set -euo pipefail
IFS=$'\n\t'

# --------- цвета и хелперы ---------
RED=$(printf '\033[31m'); GRN=$(printf '\033[32m'); YEL=$(printf '\033[33m')
BLU=$(printf '\033[34m'); BLD=$(printf '\033[1m'); RST=$(printf '\033[0m')
say()  { printf "%s==>%s %s\n" "$BLU" "$RST" "$*"; }
ok()   { printf "%s✓%s %s\n"  "$GRN" "$RST" "$*"; }
warn() { printf "%s!%s %s\n"  "$YEL" "$RST" "$*"; }
die()  { printf "%s✗%s %s\n"  "$RED" "$RST" "$*" >&2; exit 1; }
ask()  { local p="$1" def="${2:-}" var; read -r -p "$p${def:+ [$def]}: " var; echo "${var:-$def}"; }
ask_secret() { local p="$1" var; read -r -s -p "$p: " var; echo >&2; echo "$var"; }

[ "$(id -u)" -eq 0 ] || die "запускай под root (или через sudo)"

REPO_URL="${KASKAD_REPO:-https://github.com/andrey271192/kaskad.git}"
KASKAD_DIR="${KASKAD_DIR:-/opt/kaskad}"

cat <<EOF
${BLD}╔══════════════════════════════════════════════════════════════╗
║                     Kaskad — установка                       ║
║          каскадный VPN с авто-failover + WebUI + бот         ║
╚══════════════════════════════════════════════════════════════╝${RST}

Запускай этот скрипт на ${BLD}зарубежном (ам.) сервере${RST} — он станет
хостом WebUI и Telegram-бота. Остальные ам. серверы добавишь потом
через WebUI или бот в один клик.

EOF

# --------- 1. собираем входные данные ---------
say "Шаг 1/6: данные для настройки"

TG_BOT_TOKEN=$(ask "Telegram bot token (от @BotFather)")
[ -n "$TG_BOT_TOKEN" ] || die "TG bot token нужен"

TG_CHAT_ID=$(ask "Telegram chat ID для уведомлений (узнай через @userinfobot)")
[ -n "$TG_CHAT_ID" ] || die "TG chat ID нужен"

PUBLIC_IP=$(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
LOCAL_HOST_ID=$(ask "Короткое имя ЭТОГО ам. сервера" "ams1")
LOCAL_IP=$(ask "Внешний IP ЭТОГО ам. сервера" "$PUBLIC_IP")

RU_HOST=$(ask "Хост ПЕРВОГО RU-сервера (IP или домен)")
[ -n "$RU_HOST" ] || die "RU host нужен"
RU_SSH_PORT=$(ask "SSH-порт RU-сервера" "22")
RU_PASS=$(ask_secret "Пароль root@${RU_HOST} (нужен ОДИН раз — для установки SSH-ключа)")
[ -n "$RU_PASS" ] || die "пароль RU нужен (используется только сейчас)"

WEB_USER=$(ask "Логин для WebUI" "admin")
WEB_PASS=$(ask_secret "Пароль для WebUI (минимум 8 символов)")
[ "${#WEB_PASS}" -ge 8 ] || die "пароль WebUI должен быть ≥ 8 символов"

XRAY_IFACE=$(ask "Имя интерфейса X-ray на этом сервере" "amn0")

# --------- 2. ставим пакеты ---------
say "Шаг 2/6: установка пакетов"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wireguard iptables iptables-persistent python3 python3-flask \
    sshpass dnsutils curl git cron >/dev/null
ok "пакеты"

# --------- 3. клонируем репу ---------
say "Шаг 3/6: получаем код Kaskad"
if [ -d "$KASKAD_DIR/.git" ]; then
    git -C "$KASKAD_DIR" pull --ff-only >/dev/null
else
    git clone --depth 1 "$REPO_URL" "$KASKAD_DIR" >/dev/null 2>&1
fi
ok "$KASKAD_DIR"

# --------- 4. локальные ключи и конфиги ---------
say "Шаг 4/6: WireGuard, ключи, конфиги"
mkdir -p /etc/wireguard /etc/kaskad
chmod 700 /etc/wireguard /etc/kaskad

# WG-ключ для этого ам.
if [ ! -f /etc/wireguard/ru_private.key ]; then
    umask 077
    wg genkey | tee /etc/wireguard/ru_private.key | wg pubkey > /etc/wireguard/ru_public.key
fi
AMS_PUB=$(cat /etc/wireguard/ru_public.key)
AMS_PRIV=$(cat /etc/wireguard/ru_private.key)

# SSH-ключ для управления остальными серверами
if [ ! -f /root/.ssh/id_ed25519 ]; then
    mkdir -p /root/.ssh; chmod 700 /root/.ssh
    ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519 -q
fi
BOT_KEY_PUB=$(cat /root/.ssh/id_ed25519.pub)

# Заливаем SSH-ключ на RU через пароль
say "  • устанавливаю SSH-ключ на $RU_HOST"
sshpass -p "$RU_PASS" ssh -o StrictHostKeyChecking=accept-new -p "$RU_SSH_PORT" \
    "root@$RU_HOST" "mkdir -p /root/.ssh && chmod 700 /root/.ssh && \
        grep -qF '$BOT_KEY_PUB' /root/.ssh/authorized_keys 2>/dev/null || \
        echo '$BOT_KEY_PUB' >> /root/.ssh/authorized_keys && \
        chmod 600 /root/.ssh/authorized_keys" >/dev/null
ok "ssh-ключ установлен"

# --------- настройка RU-сервера, если ещё пуст ---------
say "  • настраиваю RU-сервер $RU_HOST"
ssh -p "$RU_SSH_PORT" -o StrictHostKeyChecking=no -o BatchMode=yes "root@$RU_HOST" bash <<'RUSETUP' >/dev/null
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wireguard iptables iptables-persistent python3 curl >/dev/null
sysctl -w net.ipv4.ip_forward=1 >/dev/null
grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf
cd /etc/wireguard
if [ ! -f wg_ru.conf ]; then
  umask 077
  [ -f ru_private.key ] || wg genkey | tee ru_private.key | wg pubkey > ru_public.key
  IFACE=$(ip route | awk '/^default/{print $5; exit}')
  PRIVKEY=$(cat ru_private.key)
  cat > wg_ru.conf <<EOF
[Interface]
Address = 10.0.0.1/24
PrivateKey = $PRIVKEY
ListenPort = 1939
PostUp = iptables -t nat -A POSTROUTING -o $IFACE -j MASQUERADE; iptables -I FORWARD 1 -i wg_ru -j ACCEPT; iptables -I FORWARD 1 -o wg_ru -j ACCEPT; iptables -I INPUT -i wg_ru -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o $IFACE -j MASQUERADE; iptables -D FORWARD -i wg_ru -j ACCEPT; iptables -D FORWARD -o wg_ru -j ACCEPT; iptables -D INPUT -i wg_ru -j ACCEPT
EOF
  chmod 600 wg_ru.conf
  iptables -C INPUT -p udp --dport 1939 -j ACCEPT 2>/dev/null || iptables -A INPUT -p udp --dport 1939 -j ACCEPT
  netfilter-persistent save >/dev/null 2>&1 || true
  systemctl enable --now wg-quick@wg_ru >/dev/null 2>&1 || wg-quick up wg_ru
fi
RUSETUP
RU_PUB=$(ssh -p "$RU_SSH_PORT" "root@$RU_HOST" cat /etc/wireguard/ru_public.key)
RU_IFACE=$(ssh -p "$RU_SSH_PORT" "root@$RU_HOST" "ip route | awk '/^default/{print \$5; exit}'")
ok "RU настроен, pubkey: ${RU_PUB:0:20}…"

# --------- локальный ru.conf на этом ам. ---------
cat > /etc/wireguard/ru.conf <<EOF
[Interface]
Address = 10.0.0.2/32
PrivateKey = $AMS_PRIV
Table = off
PostUp = ip rule add fwmark 100 table 200; ip route add default dev ru table 200; iptables -t nat -A POSTROUTING -o ru -j MASQUERADE
PostDown = ip rule del fwmark 100 table 200 2>/dev/null; ip route flush table 200 2>/dev/null; iptables -t nat -D POSTROUTING -o ru -j MASQUERADE 2>/dev/null
[Peer]
PublicKey = $RU_PUB
Endpoint = $RU_HOST:1939
AllowedIPs = 10.0.0.0/24
PersistentKeepalive = 25
EOF
chmod 600 /etc/wireguard/ru.conf

# Базовая mangle для X-ray → wg
iptables -t mangle -C PREROUTING -i "$XRAY_IFACE" -j MARK --set-mark 100 2>/dev/null || \
    iptables -t mangle -A PREROUTING -i "$XRAY_IFACE" -j MARK --set-mark 100
netfilter-persistent save >/dev/null 2>&1 || true

# notify.env
cat > /etc/wireguard/notify.env <<EOF
TG_BOT_TOKEN=$TG_BOT_TOKEN
TG_CHAT_ID=$TG_CHAT_ID
EOF
chmod 600 /etc/wireguard/notify.env

# webui.env
cat > /etc/kaskad/webui.env <<EOF
KASKAD_WEB_USER=$WEB_USER
KASKAD_WEB_PASS=$WEB_PASS
LOCAL_HOST=$LOCAL_HOST_ID
LOCAL_IP=$LOCAL_IP
KASKAD_HOST=0.0.0.0
KASKAD_PORT=8088
KASKAD_SSH_KEY=/root/.ssh/id_ed25519
KASKAD_SERVERS_JSON=/etc/wireguard/ru-servers.json
EOF
chmod 600 /etc/kaskad/webui.env

# ru-servers.json
cat > /etc/wireguard/ru-servers.json <<EOF
{
  "servers": [
    {"id":"primary","host":"$RU_HOST","endpoint":"$RU_HOST:1939","pubkey":"$RU_PUB",
     "probe_port":$RU_SSH_PORT,"priority":1,"label":"$RU_HOST",
     "ssh_user":"root","ssh_port":$RU_SSH_PORT,"wg_iface":"$RU_IFACE"}
  ],
  "ams_servers": [
    {"id":"$LOCAL_HOST_ID","host":"$LOCAL_IP","ssh_port":22,"tunnel_ip":"10.0.0.2",
     "pubkey":"$AMS_PUB","xray_iface":"$XRAY_IFACE","is_local":true}
  ]
}
EOF
chmod 600 /etc/wireguard/ru-servers.json

# базовые ip-списки
touch /etc/wireguard/ru-extra.list /etc/wireguard/ru-base.aips
[ -s /etc/wireguard/ru-domains.json ] || echo '{}' > /etc/wireguard/ru-domains.json
chmod 600 /etc/wireguard/ru-extra.list /etc/wireguard/ru-base.aips /etc/wireguard/ru-domains.json
ok "локальные конфиги созданы"

# --------- 5. ставим скрипты, юниты, cron ---------
say "Шаг 5/6: скрипты, сервисы, cron"
install -m 755 "$KASKAD_DIR/bin/ru-failover.py"    /usr/local/bin/
install -m 755 "$KASKAD_DIR/bin/ru-set.sh"         /usr/local/bin/
install -m 755 "$KASKAD_DIR/bin/ru-routes.sh"      /usr/local/bin/
install -m 755 "$KASKAD_DIR/bin/ru-domains.py"     /usr/local/bin/
install -m 755 "$KASKAD_DIR/bin/add-ru-helper.sh"  /usr/local/bin/
install -m 755 "$KASKAD_DIR/bin/add-ams-helper.sh" /usr/local/bin/
install -m 755 "$KASKAD_DIR/bot/ru-tg-bot.py"      /usr/local/bin/

# webui — поднимаем из /opt/kaskad/webui
install -m 644 "$KASKAD_DIR/bot/ru-tg-bot.service" /etc/systemd/system/
install -m 644 "$KASKAD_DIR/webui/ru-webui.service" /etc/systemd/system/

# Прокидываем LOCAL_HOST/LOCAL_IP в бот-юнит (там Environment=, мы должны их перезаписать)
sed -i "s|^Environment=LOCAL_HOST=.*$|Environment=LOCAL_HOST=$LOCAL_HOST_ID|" /etc/systemd/system/ru-tg-bot.service || true
sed -i "s|^Environment=LOCAL_IP=.*$|Environment=LOCAL_IP=$LOCAL_IP|" /etc/systemd/system/ru-tg-bot.service || true
grep -q "^Environment=LOCAL_HOST=" /etc/systemd/system/ru-tg-bot.service || \
    sed -i "/^EnvironmentFile=/a Environment=LOCAL_HOST=$LOCAL_HOST_ID\nEnvironment=LOCAL_IP=$LOCAL_IP" /etc/systemd/system/ru-tg-bot.service

# cron
( crontab -l 2>/dev/null | grep -v 'ru-failover\|ru-domains' ; \
  echo '* * * * * /usr/local/bin/ru-failover.py' ; \
  echo '17 */6 * * * /usr/local/bin/ru-domains.py refresh >> /var/log/ru-domains.log 2>&1' \
) | crontab -

# WG up
systemctl enable --now wg-quick@ru >/dev/null 2>&1 || wg-quick up ru
ok "WG-туннель ru поднят"

# Добавляем этот ам. как peer на RU
ssh -p "$RU_SSH_PORT" -o StrictHostKeyChecking=no "root@$RU_HOST" bash <<RUPEER >/dev/null
grep -qF "$AMS_PUB" /etc/wireguard/wg_ru.conf || cat >> /etc/wireguard/wg_ru.conf <<EOF

[Peer]
# $LOCAL_HOST_ID
PublicKey = $AMS_PUB
AllowedIPs = 10.0.0.2/32
EOF
wg set wg_ru peer "$AMS_PUB" allowed-ips 10.0.0.2/32 2>/dev/null || \
    (wg-quick down wg_ru; wg-quick up wg_ru)
RUPEER
ok "ам. добавлен как peer на RU"

# --------- 6. поднимаем сервисы ---------
say "Шаг 6/6: запуск бота и WebUI"

# WebUI юнит ссылается на /opt/kaskad-webui — пере-указываем на /opt/kaskad/webui
sed -i "s|WorkingDirectory=.*$|WorkingDirectory=$KASKAD_DIR/webui|" /etc/systemd/system/ru-webui.service
sed -i "s|ExecStart=.*$|ExecStart=/usr/bin/python3 $KASKAD_DIR/webui/app.py|" /etc/systemd/system/ru-webui.service

systemctl daemon-reload
systemctl enable --now ru-tg-bot.service
systemctl enable --now ru-webui.service
sleep 2

# открываем 8088 на firewall (опционально)
iptables -C INPUT -p tcp --dport 8088 -j ACCEPT 2>/dev/null || \
    iptables -A INPUT -p tcp --dport 8088 -j ACCEPT
netfilter-persistent save >/dev/null 2>&1 || true

# затираем пароль RU из памяти
unset RU_PASS

cat <<EOF

${GRN}${BLD}✓ Готово!${RST}

  WebUI:      ${BLD}http://$LOCAL_IP:8088${RST}
  Логин:      $WEB_USER
  Пароль:     (тот, что ты ввёл)

  Telegram-бот активен — отправь ему ${BLD}/status${RST}.

  Файлы:
    /opt/kaskad/                 — код (git pull чтобы обновить)
    /etc/wireguard/ru-servers.json — список серверов
    /etc/kaskad/webui.env        — настройки WebUI
    /etc/wireguard/notify.env    — TG token/chat

  Дальше:
    • открой WebUI и добавь больше ам. серверов / RU-серверов / доменов
    • или используй TG-бот: /help

  Удалить всё: ${BLD}sudo bash $KASKAD_DIR/uninstall.sh${RST}

EOF
