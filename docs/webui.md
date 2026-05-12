# Web UI

Flask-приложение, бежит рядом с TG-ботом (на одном из ам. серверов). Использует **те же** скрипты и тот же `ru-servers.json`, что и бот.

## Возможности

- **Дашборд** — состояние туннелей всех ам. серверов: куда подключены, возраст handshake
- **Force-переключение** на любой RU-сервер одной кнопкой
- **CRUD RU-серверов** — добавление/удаление с веб-формы (бот сам пробрасывает SSH ключи и поднимает WG)
- **CRUD ам. серверов** — то же
- **CRUD доменов** — добавить/удалить с автоматическим резолвом
- **CRUD IP/CIDR** — добавить/удалить любые подсети
- **Просмотр базовых подсетей** — read-only
- **Фильтры по доменам и IP** — для удобства поиска

## API

Все endpoint'ы под `/api/` требуют **куки-сессии** (страница входа `/login`), не HTTP Basic — иначе в браузере нельзя сделать надёжный «выход».

Из скрипта / curl — сначала POST на `/login` (как форма), потом запросы с сохранённой кукой:

```bash
curl -c jar.txt -b jar.txt -X POST 'http://127.0.0.1:8088/login' \
  -d 'username=admin&password=ВАШ_ПАРОЛЬ&next=/'
curl -b jar.txt 'http://127.0.0.1:8088/api/state' | jq .
```

| Метод | Путь | Тело | Описание |
|---|---|---|---|
| GET | `/api/state` | — | Полное состояние JSON |
| POST | `/api/use` | `{"id":"primary"}` | Force-переключить все ам. на этот RU |
| POST | `/api/server` | `{host,id,user,ssh_port,priority,...}` | Добавить новый RU |
| DELETE | `/api/server/<id>` | — | Удалить RU |
| POST | `/api/ams` | `{host,id,user,ssh_port,xray_iface,...}` | Добавить новый ам. |
| DELETE | `/api/ams/<id>` | — | Удалить ам. |
| POST | `/api/domains` | `{"domains":["vk.com",...]}` | Добавить домены |
| DELETE | `/api/domains` | `{"domains":[...]}` | Удалить домены |
| POST | `/api/domains/refresh` | — | Перерезолвить |
| POST | `/api/ips` | `{"ips":["1.2.3.4/32",...]}` или `{"ips":"text with IPs"}` | Добавить |
| DELETE | `/api/ips` | то же | Удалить |
| POST | `/api/ips/clear` | — | Очистить все доп. IP |

## Безопасность

- Вход — форма `/login` + подписанная **cookie-сессия** (ключ `KASKAD_SECRET_KEY` или файл `/etc/kaskad/.session_secret`). Пароль админа — `KASKAD_WEB_PASS`, должен быть длинным и случайным.
- По умолчанию слушает на `0.0.0.0:8088`. **Рекомендуется** поставить за HTTPS reverse-proxy (nginx/caddy) с Let's Encrypt
- Если хочется привязать только к localhost — `KASKAD_HOST=127.0.0.1` и пользоваться через SSH-туннель: `ssh -L 8088:localhost:8088 root@ams1`
- Пароли SSH (`password=` при `/server-add`, `/ams-add`) передаются по HTTPS только если веб за reverse-proxy. Без HTTPS не передавай пароли через WebUI — используй ключи (см. `/bot-key`)

## Конфиг (env-переменные)

| Переменная | Дефолт | Описание |
|---|---|---|
| `KASKAD_WEB_USER` | `admin` | Логин для входа в WebUI |
| `KASKAD_WEB_PASS` | (нет) | Пароль; ОБЯЗАТЕЛЬНО задать |
| `KASKAD_SECRET_KEY` | (файл) | Секрет подписи сессии; иначе создаётся `/etc/kaskad/.session_secret` |
| `LOCAL_HOST` | `ams1` | Имя локального ам. сервера |
| `LOCAL_IP` | `127.0.0.1` | Локальный IP — определяет, какой ам. читать локально без SSH |
| `KASKAD_HOST` | `0.0.0.0` | Адрес для bind |
| `KASKAD_PORT` | `8088` | Порт |
| `KASKAD_SSH_KEY` | `/root/.ssh/id_ed25519` | SSH-ключ для управления другими серверами |
| `KASKAD_SERVERS_JSON` | `/etc/wireguard/ru-servers.json` | Путь к конфигу |

## nginx reverse-proxy (пример)

```nginx
server {
    listen 443 ssl http2;
    server_name kaskad.example.com;
    ssl_certificate /etc/letsencrypt/live/kaskad.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/kaskad.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;  # для долгих /add-domain пачкой
    }
}
```

И в WebUI поставить `KASKAD_HOST=127.0.0.1`, не открывать 8088 наружу.
