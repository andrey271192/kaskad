# Установка

## Требования

- Минимум 1 RU-сервер (Ubuntu 20.04+) с публичным IP или пробросом UDP/1939 на роутере
- Минимум 1 ам. сервер (Ubuntu 20.04+) с установленной X-ray панелью (3x-ui)
- Telegram-бот (создать в @BotFather) и chat ID для уведомлений (узнать у @userinfobot)
- Root доступ на оба сервера

## Шаг 1. Первый RU-сервер

На RU-сервере под root:

```bash
apt update && apt install -y wireguard iptables-persistent

# 1. Генерация ключа
cd /etc/wireguard
umask 077
wg genkey | tee ru_private.key | wg pubkey > ru_public.key
PRIVKEY=$(cat ru_private.key)

# 2. ip_forward
sysctl -w net.ipv4.ip_forward=1
echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

# 3. Конфиг — пока без peer'ов, добавятся когда подключим первый ам.
IFACE=$(ip route | awk '/^default/{print $5; exit}')
cat > /etc/wireguard/wg_ru.conf <<EOF
[Interface]
Address = 10.0.0.1/24
PrivateKey = $PRIVKEY
ListenPort = 1939
PostUp = iptables -t nat -A POSTROUTING -o $IFACE -j MASQUERADE; iptables -I FORWARD 1 -i wg_ru -j ACCEPT; iptables -I FORWARD 1 -o wg_ru -j ACCEPT; iptables -I INPUT -i wg_ru -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o $IFACE -j MASQUERADE; iptables -D FORWARD -i wg_ru -j ACCEPT; iptables -D FORWARD -o wg_ru -j ACCEPT; iptables -D INPUT -i wg_ru -j ACCEPT
EOF
chmod 600 /etc/wireguard/wg_ru.conf

# 4. Открыть порт 1939/UDP
iptables -A INPUT -p udp --dport 1939 -j ACCEPT
netfilter-persistent save

# 5. Запуск + автостарт
wg-quick up wg_ru
systemctl enable wg-quick@wg_ru

# 6. Сохранить публичный ключ — пригодится
cat ru_public.key
```

Если RU за NAT (Keenetic и т.п.) — пробрось UDP/1939 на роутере на этот сервер.

## Шаг 2. Первый ам. сервер

На ам. сервере под root:

```bash
apt update && apt install -y wireguard iptables-persistent dnsutils

# 1. Ключ
cd /etc/wireguard
umask 077
wg genkey | tee ru_private.key | wg pubkey > ru_public.key
cat ru_public.key  # записать — добавим в peer'ы RU

# 2. Узнать имя интерфейса X-ray
ip a | grep -E 'amn|tun' | grep -v '@'
# обычно amn0 — используем дальше

# 3. Конфиг ru.conf — IP в туннеле = 10.0.0.2 (первый ам.)
PRIVKEY=$(cat ru_private.key)
cat > /etc/wireguard/ru.conf <<EOF
[Interface]
Address = 10.0.0.2/32
PrivateKey = $PRIVKEY
Table = off
PostUp = ip route add 95.163.0.0/16 dev ru; ip route add 185.73.192.0/22 dev ru; ip route add 213.59.0.0/16 dev ru; ip route add 77.88.0.0/18 dev ru; ip route add 93.158.128.0/18 dev ru; ip route add 188.40.167.0/24 dev ru; ip route add 176.114.120.0/22 dev ru; ip route add 178.248.232.0/22 dev ru; ip route add 213.180.192.0/20 dev ru; ip route add 87.240.128.0/18 dev ru; ip rule add fwmark 100 table 200; ip route add default dev ru table 200; iptables -t nat -A POSTROUTING -o ru -j MASQUERADE
PostDown = ip rule del fwmark 100 table 200; ip route flush table 200; iptables -t nat -D POSTROUTING -o ru -j MASQUERADE
[Peer]
PublicKey = ПУБЛИЧНЫЙ_КЛЮЧ_RU_СЕРВЕРА
Endpoint = ВНЕШНИЙ_IP_RU:1939
AllowedIPs = 10.0.0.0/24, 95.163.0.0/16, 185.73.192.0/22, 213.59.0.0/16, 77.88.0.0/18, 93.158.128.0/18, 188.40.167.0/24, 176.114.120.0/22, 178.248.232.0/22, 213.180.192.0/20, 87.240.128.0/18
PersistentKeepalive = 25
EOF
chmod 600 /etc/wireguard/ru.conf

# 4. Mangle для X-ray трафика — за каждой подсетью
for net in 95.163.0.0/16 185.73.192.0/22 213.59.0.0/16 77.88.0.0/18 93.158.128.0/18 188.40.167.0/24 176.114.120.0/22 178.248.232.0/22 213.180.192.0/20 87.240.128.0/18; do
  iptables -t mangle -A PREROUTING -i amn0 -d $net -j MARK --set-mark 100
done
netfilter-persistent save

# 5. Запуск
wg-quick up ru
systemctl enable wg-quick@ru
wg show ru  # должен быть handshake
```

На RU-сервере добавить этот ам. как peer:
```bash
wg set wg_ru peer ПУБЛИЧНЫЙ_КЛЮЧ_АМ allowed-ips 10.0.0.2/32
cat >> /etc/wireguard/wg_ru.conf <<EOF

[Peer]
PublicKey = ПУБЛИЧНЫЙ_КЛЮЧ_АМ
AllowedIPs = 10.0.0.2/32
EOF
```

Проверить что трафик идёт:
```bash
# на ам. сервере
curl --interface ru https://gosuslugi.ru -I
# должен ответить HTTP/... 200
```

## Шаг 3. Установка скриптов и бота

На ам. сервере где будет жить бот (в Амстердаме, Telegram должен быть доступен!):

```bash
git clone https://github.com/andrey271192/kaskad.git /opt/kaskad
cd /opt/kaskad

# 1. Скопировать скрипты
install -m 755 bin/ru-failover.py    /usr/local/bin/
install -m 755 bin/ru-set.sh         /usr/local/bin/
install -m 755 bin/ru-routes.sh      /usr/local/bin/
install -m 755 bin/ru-domains.py     /usr/local/bin/
install -m 755 bin/add-ru-helper.sh  /usr/local/bin/
install -m 755 bin/add-ams-helper.sh /usr/local/bin/

# 2. Создать notify.env
cp examples/notify.env.example /etc/wireguard/notify.env
chmod 600 /etc/wireguard/notify.env
# ВПИСАТЬ TG_BOT_TOKEN и TG_CHAT_ID

# 3. Создать ru-servers.json
cp examples/ru-servers.example.json /etc/wireguard/ru-servers.json
chmod 600 /etc/wireguard/ru-servers.json
# ОТРЕДАКТИРОВАТЬ — вписать host, pubkey, ssh_user/port для каждого RU; для каждого ам. — host, pubkey, tunnel_ip

# 4. Базовый список allowed-ips (берётся из ru.conf при первом apply)
# создаст ru-base.aips автоматически
/usr/local/bin/ru-routes.sh apply

# 5. Cron на failover и refresh доменов
( crontab -l 2>/dev/null; \
  echo '* * * * * /usr/local/bin/ru-failover.py'; \
  echo '17 */6 * * * /usr/local/bin/ru-domains.py refresh >> /var/log/ru-domains.log 2>&1' \
) | crontab -

# 6. SSH-ключ бота (нужен для управления остальными серверами через ssh-key auth)
[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519
cat /root/.ssh/id_ed25519.pub
# скопировать в /root/.ssh/authorized_keys на всех остальных ам. серверах
# и на всех RU-серверах

# 7. Установить бот
install -m 755 bot/ru-tg-bot.py /usr/local/bin/
install -m 644 bot/ru-tg-bot.service /etc/systemd/system/
apt install -y python3 sshpass
systemctl daemon-reload
systemctl enable --now ru-tg-bot.service
journalctl -u ru-tg-bot.service -f
```

Послать боту в Telegram `/status` — должно прийти текущее состояние.

## Шаг 4. Веб-интерфейс (опционально)

На том же ам. сервере, где бот:

```bash
cd /opt/kaskad
apt install -y python3-flask

install -m 755 webui/app.py /usr/local/bin/ru-webui.py
mkdir -p /usr/local/share/kaskad
cp -r webui/templates webui/static /usr/local/share/kaskad/
# в app.py указать template_folder и static_folder если нужно;
# по умолчанию работает из текущей директории, поэтому проще:
ln -s /usr/local/share/kaskad/templates /usr/local/bin/templates
ln -s /usr/local/share/kaskad/static /usr/local/bin/static

mkdir -p /etc/kaskad
cp webui/webui.env.example /etc/kaskad/webui.env
chmod 600 /etc/kaskad/webui.env
# ВПИСАТЬ KASKAD_WEB_USER и KASKAD_WEB_PASS

install -m 644 webui/ru-webui.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ru-webui.service

# Открыть порт 8088 (по желанию)
iptables -I INPUT -p tcp --dport 8088 -j ACCEPT
netfilter-persistent save

# Открыть в браузере: http://AMS-IP:8088
```

Рекомендуется поставить за HTTPS reverse-proxy (nginx, caddy, traefik) с Let's Encrypt.

## Шаг 5. Дальше

Через бот или WebUI:
- `/server-add` — добавить новый RU-сервер
- `/ams-add` — добавить новый ам. сервер
- `/add-domain vk.com ozon.ru` — добавить русские сайты по доменам
- `/add 5.45.192.1/32` — добавить конкретные IP/CIDR

Failover-скрипт каждую минуту проверяет здоровье текущего peer'а и переключает при необходимости.

## Если что-то не работает

См. [docs/troubleshooting.md](troubleshooting.md).
