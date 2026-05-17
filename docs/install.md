# Установка Kaskad

Проще всего — одна команда на зарубежном VPS (ниже). Если любишь разбирать каждый шаг сам — есть ручной путь в конце файла.

---

## 🚀 Простой путь — одна команда

### Что нужно заранее

- **Один зарубежный сервер** (Ubuntu/Debian 20.04+, root) с настроенным X-ray (3x-ui подойдёт), интерфейс обычно `amn0`. Здесь будут жить WebUI и Telegram-бот.
- **Один RU-сервер** (Ubuntu/Debian 20.04+, root, открыт UDP/1939 наружу или проброшен с роутера). Если он чистый — скрипт сам поставит на него WireGuard.
- **Telegram-бот**: создайте через [@BotFather](https://t.me/BotFather), запишите token.
- **Свой Telegram chat ID**: узнайте через [@userinfobot](https://t.me/userinfobot).

### Запуск

На зарубежном сервере, под root:

```bash
curl -fsSL https://raw.githubusercontent.com/andrey271192/kaskad/main/install.sh | sudo bash
```

Скрипт интерактивный, спросит 5–6 вопросов:

| Вопрос | Что вводить |
|---|---|
| Telegram bot token | строка от @BotFather, типа `123456:AAH...` |
| Telegram chat ID | число, например `123456789` |
| Короткое имя ам. сервера | любое, `ams1` подойдёт |
| Внешний IP | сам подтянет с `api.ipify.org` |
| Хост RU-сервера | IP или домен RU-машины |
| SSH-порт RU | обычно `22` |
| Пароль root@RU | используется ОДИН раз, потом затирается |
| Логин WebUI | например `admin` |
| Пароль WebUI | минимум 8 символов |
| Имя X-ray-интерфейса | обычно `amn0` |

Через 2–3 минуты:

```
✓ Готово!

  WebUI:    http://YOUR_AMS_IP:8088
  Логин:    admin
  Пароль:   (тот, что ты ввёл)

  Telegram-бот активен — отправь ему /status.
```

Дальше всё через WebUI или бот.

### Обновление

```bash
cd /opt/kaskad && git pull && sudo bash install.sh
```

Скрипт идемпотентный — повторный запуск ничего не сломает, просто обновит скрипты.

### Удаление

```bash
sudo bash /opt/kaskad/uninstall.sh
```

Или одной командой:

```bash
curl -fsSL https://raw.githubusercontent.com/andrey271192/kaskad/main/uninstall.sh | sudo bash
```

Подтверждение через ввод `YES`. Если хотите снести и WG-ключи — `KASKAD_PURGE_KEYS=1 sudo bash uninstall.sh`.

---

## 🛠 Ручной путь — для тех, кто хочет понять каждый шаг

Все шаги ниже делает за вас `install.sh` — приведены для отладки и образовательных целей.

### Шаг 1. Первый RU-сервер

На RU-сервере под root:

```bash
apt update && apt install -y wireguard iptables-persistent

cd /etc/wireguard
umask 077
wg genkey | tee ru_private.key | wg pubkey > ru_public.key
PRIVKEY=$(cat ru_private.key)

sysctl -w net.ipv4.ip_forward=1
echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

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

iptables -A INPUT -p udp --dport 1939 -j ACCEPT
netfilter-persistent save

wg-quick up wg_ru
systemctl enable wg-quick@wg_ru

cat ru_public.key  # запомните — понадобится
```

Если RU за NAT (Keenetic и т.п.) — пробросьте UDP/1939 на роутере на этот сервер.

### Шаг 2. Первый ам. сервер

На ам. сервере под root:

```bash
apt update && apt install -y wireguard iptables-persistent dnsutils python3 python3-flask sshpass

cd /etc/wireguard
umask 077
wg genkey | tee ru_private.key | wg pubkey > ru_public.key
cat ru_public.key  # запомните — добавим в peer'ы RU

# Узнать имя интерфейса X-ray
ip a | grep -E 'amn|tun' | grep -v '@'
# обычно amn0

PRIVKEY=$(cat ru_private.key)
cat > /etc/wireguard/ru.conf <<EOF
[Interface]
Address = 10.0.0.2/32
PrivateKey = $PRIVKEY
Table = off
PostUp = ip rule add fwmark 100 table 200; ip route add default dev ru table 200; iptables -t nat -A POSTROUTING -o ru -j MASQUERADE
PostDown = ip rule del fwmark 100 table 200; ip route flush table 200; iptables -t nat -D POSTROUTING -o ru -j MASQUERADE
[Peer]
PublicKey = ПУБЛИЧНЫЙ_КЛЮЧ_RU
Endpoint = ВНЕШНИЙ_IP_RU:1939
AllowedIPs = 10.0.0.0/24
PersistentKeepalive = 25
EOF
chmod 600 /etc/wireguard/ru.conf

# mangle для X-ray трафика
iptables -t mangle -A PREROUTING -i amn0 -j MARK --set-mark 100
netfilter-persistent save

wg-quick up ru
systemctl enable wg-quick@ru
wg show ru  # должен быть handshake
```

На RU-сервере добавьте этот ам. как peer:

```bash
cat >> /etc/wireguard/wg_ru.conf <<EOF

[Peer]
PublicKey = ПУБЛИЧНЫЙ_КЛЮЧ_АМ
AllowedIPs = 10.0.0.2/32
EOF
wg set wg_ru peer ПУБЛИЧНЫЙ_КЛЮЧ_АМ allowed-ips 10.0.0.2/32
```

Проверьте на ам.:

```bash
curl --interface ru https://gosuslugi.ru -I
# должен ответить HTTP/... 200
```

### Шаг 3. Скрипты, бот, WebUI

```bash
git clone https://github.com/andrey271192/kaskad.git /opt/kaskad
cd /opt/kaskad

install -m 755 bin/ru-failover.py    /usr/local/bin/
install -m 755 bin/ru-set.sh         /usr/local/bin/
install -m 755 bin/ru-routes.sh      /usr/local/bin/
install -m 755 bin/ru-domains.py     /usr/local/bin/
install -m 755 bin/add-ru-helper.sh  /usr/local/bin/
install -m 755 bin/add-ams-helper.sh /usr/local/bin/
install -m 755 bot/ru-tg-bot.py      /usr/local/bin/

cp examples/notify.env.example /etc/wireguard/notify.env
chmod 600 /etc/wireguard/notify.env
# впишите TG_BOT_TOKEN и TG_CHAT_ID

cp examples/ru-servers.example.json /etc/wireguard/ru-servers.json
chmod 600 /etc/wireguard/ru-servers.json
# отредактируйте — host, pubkey, ssh_port для RU; host, pubkey, tunnel_ip для ам.

mkdir -p /etc/kaskad
cp webui/webui.env.example /etc/kaskad/webui.env
chmod 600 /etc/kaskad/webui.env
# впишите KASKAD_WEB_USER и KASKAD_WEB_PASS

install -m 644 bot/ru-tg-bot.service     /etc/systemd/system/
install -m 644 webui/ru-webui.service    /etc/systemd/system/
# отредактируйте WorkingDirectory и ExecStart в ru-webui.service → /opt/kaskad/webui

# cron
( crontab -l 2>/dev/null; \
  echo '* * * * * /usr/local/bin/ru-failover.py'; \
  echo '17 */6 * * * /usr/local/bin/ru-domains.py refresh >> /var/log/ru-domains.log 2>&1' \
) | crontab -

# SSH-ключ для бота
[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519
cat /root/.ssh/id_ed25519.pub
# добавьте в /root/.ssh/authorized_keys на остальных ам. и RU серверах

systemctl daemon-reload
systemctl enable --now ru-tg-bot.service ru-webui.service

# открываем 8088
iptables -A INPUT -p tcp --dport 8088 -j ACCEPT
netfilter-persistent save
```

Проверьте: `journalctl -u ru-tg-bot -u ru-webui -f`. Отправьте боту `/status` — должен ответить. Откройте `http://AMS_IP:8088`.

---

## Дальнейшее расширение

После того как первая пара RU + ам. работает — добавлять новые **через WebUI или бот**, ничего больше не надо настраивать вручную.

- **+RU-сервер** → в WebUI «+ добавить RU-сервер» (нужен root-пароль для первичной онбординги) или `/server-add` в боте
- **+ам. сервер** → «+ добавить ам. сервер» или `/ams-add`
- **+домен** (vk.com, ozon.ru) → секция «Домены» или `/add-domain vk.com ozon.ru`
- **+IP/CIDR** → секция «Доп. IP/CIDR» или `/add 5.45.192.1/32`

Cron каждую минуту проверяет здоровье текущего RU peer'а через handshake age + TCP-пробу и переключает на следующий по приоритету при необходимости.

---

## Если что-то не работает

См. [troubleshooting.md](troubleshooting.md).
