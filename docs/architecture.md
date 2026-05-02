# Архитектура

## Обзор

Каждый ам. сервер — это **WG-клиент**, держит ОДИН активный туннель к одному из RU-серверов. RU-серверы — это **WG-серверы**, у каждого N пиров (по числу ам.).

```
                     ┌─────────────┐
                     │   RU primary│
                     │  (priority 1)│
                     └──┬──┬──┬──┬──┘
                        │  │  │  │  WG туннели
                  ┌─────┘  │  │  └─────┐
                  ▼        ▼  ▼        ▼
              ┌──────┐ ┌──────┐ ┌──────┐
              │ ams1 │ │ ams2 │ │ ams3 │
              │X-ray │ │X-ray │ │X-ray │
              └──────┘ └──────┘ └──────┘
                  ▲        ▲        ▲
                  │        │        │  failover при падении primary
                  │   ┌────┘        │
                  │   │             │
                     ┌─▼─────────────▼──┐
                     │   RU backup      │
                     │  (priority 2)    │
                     └──────────────────┘
```

## Компоненты

### На RU-серверах
- `wg_ru` интерфейс, listen UDP/1939, peers = все ам. серверы (`tunnel_ip` → `pubkey`)
- `iptables -t nat -A POSTROUTING -o <wan_iface> -j MASQUERADE` — выходящий трафик от ам. серверов наружу
- `iptables -I FORWARD -i wg_ru -j ACCEPT`, `-o wg_ru -j ACCEPT` — пропуск форварда

### На ам. серверах
- `ru` интерфейс, peer = один из RU-серверов (тот что активен сейчас)
- `/etc/wireguard/ru.conf` — текущая конфигурация туннеля
- `/etc/wireguard/ru-servers.json` — общий список всех RU и ам. серверов (синхронизируется ботом/WebUI)
- `/etc/wireguard/ru-base.aips` — базовый набор подсетей в `AllowedIPs` (создаётся при первой установке)
- `/etc/wireguard/ru-extra.list` — дополнительные IP/CIDR, добавленные через бот
- `/etc/wireguard/ru-domains.json` — карта домен → список IP (резолвится через `dig`)

### Скрипты на ам. серверах (`/usr/local/bin/`)
| Скрипт | Что делает |
|---|---|
| `ru-failover.py` | Cron каждую минуту. Проверяет handshake/probe текущего peer'а, переключает на другой при падении. Trial-failback при возврате primary. |
| `ru-set.sh <id>` | Принудительная установка peer'а из JSON по `id` |
| `ru-routes.sh add\|remove\|clear\|list\|apply` | Управление `ru-extra.list` + live `ip route` + iptables mangle + sync allowed-ips через `wg syncconf` |
| `ru-domains.py add\|remove\|list\|show\|refresh` | Резолв доменов и проксирование результатов в `ru-routes.sh` |

## Маршрутизация трафика

```
Запрос с телефона на gosuslugi.ru:
  1. X-ray на ам. сервере получает пакет на amn0 интерфейсе
  2. iptables -t mangle -A PREROUTING -i amn0 -d 95.163.0.0/16 -j MARK --set-mark 100
  3. ip rule fwmark 100 lookup 200
  4. table 200: default dev ru
  5. WG проверяет AllowedIPs пира (95.163.0.0/16 ∈ allowed) → шифрует
  6. Пакет идёт в туннель к RU-серверу
  7. На RU: pакет приходит на wg_ru, MASQUERADE на wan, идёт в интернет с RU IP
  8. Ответ возвращается обратно через NAT → wg_ru → ам. сервер → телефон
```

Для НЕ-российских сайтов: пакет в mangle не получает MARK 100, идёт по обычному маршруту через провайдера ам. сервера.

## Failover

`ru-failover.py` запускается из cron каждую минуту:

```
1. Прочитать /etc/wireguard/ru-servers.json (отсортированный по priority)
2. Определить current = peer чей endpoint в /etc/wireguard/ru.conf
3. age = now - last_handshake (от wg show)
4. Если current dead (age > 180 && TCP probe не отвечает && cooldown 5min прошёл):
     → переключиться на ближайший живой
5. Если current не highest-priority И есть более приоритетный живой И cooldown && fail_backoff прошли:
     → переключиться на него (mode = failback test)
6. Если в режиме failback test:
     - handshake появился за <60s → success
     - не появился → откат на запасной + 30min backoff на повторный failback
```

Переключение через `wg syncconf` — без рестарта интерфейса. Маршруты, mangle, расширенные allowed-ips восстанавливаются через `ru-routes.sh apply` (вызывается из PostUp и сразу после switch).

## Синхронизация конфига

`ru-servers.json` — единый источник правды. Хранится на каждом ам. сервере. Изменения вносятся ТОЛЬКО через бот/WebUI на «локальном» ам. сервере (где бот живёт), затем `save_and_distribute()` SCP-ит файл на остальные через root SSH-ключ.

Конфиг WG-серверов (`/etc/wireguard/wg_ru.conf` на RU) бот редактирует напрямую при `/ams-add` / `/ams-remove` — добавляет/удаляет [Peer] секции и делает `wg syncconf wg_ru`.

## SSH между серверами

Бот (на одном из ам. серверов) генерирует ed25519 ключ при первом запуске. Этот ключ:
- автоматически прописывается в `/root/.ssh/authorized_keys` на всех остальных ам. серверах при `/ams-add` (или вручную при онбординге)
- автоматически прописывается на новых RU при `/server-add` (через add-ru-helper.sh)
- для существующих RU — добавляется один раз вручную при первичной настройке

После этого бот работает только по ключу, никаких сохранённых паролей.

## Уведомления

`ru-failover.py` каждый switch/success/fail логирует через `logger -t ru-failover` И шлёт в Telegram если есть `/etc/wireguard/notify.env` с `TG_BOT_TOKEN` и `TG_CHAT_ID`.

С RU-серверов в России TG API часто недоступен, поэтому бот живёт на одном из ам. серверов.
