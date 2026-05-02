#!/bin/bash
# add-ams-helper.sh <bot_pubkey_b64>
# Запускается на НОВОМ ам. сервере. Только устанавливает базу — основные файлы scp-ом.
set -e
exec 2>&1

BOT_KEY_B64=$1
DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard iptables-persistent curl python3 >/dev/null 2>&1

mkdir -p /etc/wireguard /usr/local/bin /var/lib/ru-failover
mkdir -p /root/.ssh && chmod 700 /root/.ssh
BOT_KEY=$(echo "$BOT_KEY_B64" | base64 -d)
grep -qF "$BOT_KEY" /root/.ssh/authorized_keys 2>/dev/null || echo "$BOT_KEY" >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

sysctl -w net.ipv4.ip_forward=1 >/dev/null
grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

PUBLIC_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo unknown)
IFACE=$(ip route | awk '/^default/{print $5; exit}')
echo "----RESULT----"
echo "PUBLIC_IP=$PUBLIC_IP"
echo "IFACE=$IFACE"
echo "----END----"
