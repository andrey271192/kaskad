#!/bin/bash
# add-ru-helper.sh <bot_pubkey_b64> <listen_port> <peer_pk> <peer_ip> ...
set -e
exec 2>&1

BOT_KEY_B64=$1; shift
LISTEN_PORT=$1; shift
declare -a PEERS_PK PEERS_IP
while [ $# -ge 2 ]; do
  PEERS_PK+=("$1"); shift
  PEERS_IP+=("$1"); shift
done

DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard iptables-persistent curl >/dev/null 2>&1

mkdir -p /etc/wireguard
cd /etc/wireguard
if [ ! -f ru_private.key ]; then
  umask 077
  wg genkey | tee ru_private.key | wg pubkey > ru_public.key
fi
PRIVKEY=$(cat ru_private.key)
PUBKEY=$(cat ru_public.key)
IFACE=$(ip route | awk '/^default/{print $5; exit}')
PUBLIC_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo unknown)

CONF=/etc/wireguard/wg_ru.conf
{
  echo "[Interface]"
  echo "Address = 10.0.0.1/24"
  echo "PrivateKey = $PRIVKEY"
  echo "ListenPort = $LISTEN_PORT"
  echo "PostUp = iptables -t nat -A POSTROUTING -o $IFACE -j MASQUERADE; iptables -I FORWARD 1 -i wg_ru -j ACCEPT; iptables -I FORWARD 1 -o wg_ru -j ACCEPT; iptables -I INPUT -i wg_ru -j ACCEPT"
  echo "PostDown = iptables -t nat -D POSTROUTING -o $IFACE -j MASQUERADE; iptables -D FORWARD -i wg_ru -j ACCEPT; iptables -D FORWARD -o wg_ru -j ACCEPT; iptables -D INPUT -i wg_ru -j ACCEPT"
  for i in "${!PEERS_PK[@]}"; do
    echo
    echo "[Peer]"
    echo "PublicKey = ${PEERS_PK[$i]}"
    echo "AllowedIPs = ${PEERS_IP[$i]}/32"
  done
} > "$CONF"
chmod 600 "$CONF"

sysctl -w net.ipv4.ip_forward=1 >/dev/null
grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

iptables -C INPUT -p udp --dport "$LISTEN_PORT" -j ACCEPT 2>/dev/null \
  || iptables -I INPUT -p udp --dport "$LISTEN_PORT" -j ACCEPT
netfilter-persistent save >/dev/null 2>&1 || iptables-save > /etc/iptables/rules.v4 2>/dev/null

wg-quick down wg_ru 2>/dev/null || true
wg-quick up wg_ru
systemctl enable wg-quick@wg_ru >/dev/null 2>&1

# bot pubkey в root authorized_keys для будущего управления
mkdir -p /root/.ssh && chmod 700 /root/.ssh
BOT_KEY=$(echo "$BOT_KEY_B64" | base64 -d)
grep -qF "$BOT_KEY" /root/.ssh/authorized_keys 2>/dev/null || echo "$BOT_KEY" >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

echo "----RESULT----"
echo "PUBKEY=$PUBKEY"
echo "IFACE=$IFACE"
echo "PUBLIC_IP=$PUBLIC_IP"
echo "----END----"
