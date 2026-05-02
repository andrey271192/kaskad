# Troubleshooting

## Туннель не устанавливается (нет handshake)

```bash
# на ам. сервере
wg show ru
# если нет endpoint вообще — что-то с конфигом
# если есть, но нет handshake:
nc -uvz <RU_HOST> 1939   # проверить что 1939/UDP проброшен
journalctl -u wg-quick@ru -n 30
```

На RU:
```bash
iptables -L INPUT -n | grep 1939   # должно быть ACCEPT
ss -ulnp | grep 1939                # WG слушает
wg show wg_ru                        # должны быть peer'ы
```

Если за NAT (Keenetic) — пробрось 1939/UDP на роутере.

## Handshake есть, curl через туннель не идёт (HTTP 000)

Скорее всего `FORWARD` policy = DROP на RU (часто из-за установленного Docker):
```bash
iptables -L FORWARD -n | head -1
# если DROP — добавить:
iptables -I FORWARD -i wg_ru -j ACCEPT
iptables -I FORWARD -o wg_ru -j ACCEPT
iptables -I INPUT -i wg_ru -j ACCEPT
netfilter-persistent save
```

И прописать в `PostUp` `wg_ru.conf` чтобы пережили рестарт.

Также проверить `ip_forward`:
```bash
sysctl net.ipv4.ip_forward   # должно быть = 1
```

## Failover не срабатывает / срабатывает зря

Логи:
```bash
journalctl -t ru-failover -n 50
cat /var/lib/ru-failover/last_switch    # timestamp последнего switch
cat /var/lib/ru-failover/test_started   # 0 если не в режиме failback test
cat /var/lib/ru-failover/last_fail      # timestamp последнего failed failback
```

Health-check использует TCP probe (по `probe_port` из JSON). Если хост жив, но probe_port закрыт — будет ложное срабатывание. Проверь:
```bash
nc -zv <RU_HOST> <probe_port>
```

Если ICMP блокируется на RU — это нормально, мы не используем ping.

## /add-domain ничего не добавляет

```bash
# на ам. сервере
which dig    # если нет — apt install dnsutils
dig +short A vk.com   # должны быть IP
/usr/local/bin/ru-domains.py add vk.com   # ручной тест
```

## Бот не отвечает

```bash
systemctl status ru-tg-bot.service
journalctl -u ru-tg-bot.service -n 30
# проверка доступности TG:
curl -s -m 5 -o /dev/null -w '%{http_code}\n' https://api.telegram.org
# должно быть 302; 000 = заблокирован, бота нужно перенести на сервер вне РФ
```

Проверь, что `TG_CHAT_ID` в `notify.env` совпадает с твоим chat_id (узнать у `@userinfobot` или просто ничего боту не пиши — он молчит для всех кроме разрешённого chat_id).

## WebUI не открывается

```bash
systemctl status ru-webui.service
journalctl -u ru-webui.service -n 30
ss -tlnp | grep 8088
iptables -L INPUT -n | grep 8088   # порт должен быть ACCEPT
```

## Сменился pubkey ам. сервера, но JSON не обновился

Если ты руками регенерил ключи WG на ам. сервере — нужно:
1. Обновить `pubkey` этого ам. в `ru-servers.json` через бот: `/ams-remove <id>` + `/ams-add ...`
2. Или вручную в JSON и `wg syncconf wg_ru` на каждом RU

## Конфиги разъехались между серверами

Бот считает `ru-servers.json` на «локальном» ам. сервере источником правды и синхронит на остальные при каждом изменении. Если ты вручную менял JSON на не-локальном — изменения потеряются.

Принудительная синхронизация: на локальном ам.
```bash
/usr/local/bin/ru-tg-bot.py --resync   # (если нет — через любую команду /add /remove /server-add т.п.)
```

Или просто пересохрани JSON через WebUI (любая операция запишет и распространит).
