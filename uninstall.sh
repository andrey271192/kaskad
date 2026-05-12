#!/usr/bin/env bash
# Kaskad — полное удаление.
#
#   curl -fsSL https://raw.githubusercontent.com/andrey271192/kaskad/main/uninstall.sh | bash
#
# Что удаляется:
#   • systemd-юниты ru-tg-bot.service, ru-webui.service
#   • скрипты в /usr/local/bin (ru-failover.py, ru-set.sh, ru-routes.sh, ru-domains.py,
#                                add-ru-helper.sh, add-ams-helper.sh, ru-tg-bot.py)
#   • cron-задачи ru-failover и ru-domains
#   • WG-туннель ru (на ам.) или wg_ru (на RU)
#   • конфиги: /etc/kaskad/, /etc/wireguard/ru-servers.json, ru-extra.list, ru-base.aips,
#              ru-domains.json, ru.conf, notify.env
#   • директория /opt/kaskad
#
# По умолчанию НЕ трогает: SSH-ключ /root/.ssh/id_ed25519, X-ray, общесистемный iptables-persistent.
# Запросит подтверждение перед удалением.

set -euo pipefail

RED=$(printf '\033[31m'); GRN=$(printf '\033[32m'); YEL=$(printf '\033[33m')
BLU=$(printf '\033[34m'); BLD=$(printf '\033[1m'); RST=$(printf '\033[0m')
say()  { printf "%s==>%s %s\n" "$BLU" "$RST" "$*"; }
ok()   { printf "%s✓%s %s\n"  "$GRN" "$RST" "$*"; }
warn() { printf "%s!%s %s\n"  "$YEL" "$RST" "$*"; }
die()  { printf "%s✗%s %s\n"  "$RED" "$RST" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запускай под root"

FORCE=${KASKAD_FORCE:-0}
PURGE_KEYS=${KASKAD_PURGE_KEYS:-0}

if [ "$FORCE" != "1" ]; then
    cat <<EOF
${BLD}Удаление Kaskad${RST}

Будут удалены:
  • сервисы: ru-tg-bot.service, ru-webui.service
  • скрипты в /usr/local/bin/
  • cron-задачи (failover, refresh доменов)
  • WG-туннели (ru / wg_ru)
  • конфиги: /etc/kaskad/, /etc/wireguard/ru-servers.json, ru.conf, notify.env, ru-base.aips, ru-extra.list, ru-domains.json
  • директория /opt/kaskad/

НЕ удаляются (по умолчанию):
  • /root/.ssh/id_ed25519 (SSH-ключ — может использоваться чем-то ещё)
  • X-ray / 3x-ui
  • системные пакеты (wireguard, python3-flask, ...)

EOF
    read -r -p "Точно удалить? (введи ${BLD}YES${RST}): " ans
    [ "$ans" = "YES" ] || { echo "отмена"; exit 0; }
fi

say "стопаю сервисы"
systemctl disable --now ru-tg-bot.service 2>/dev/null || true
systemctl disable --now ru-webui.service  2>/dev/null || true
rm -f /etc/systemd/system/ru-tg-bot.service /etc/systemd/system/ru-webui.service
systemctl daemon-reload || true
ok "сервисы остановлены"

say "опускаю WG-туннели"
wg-quick down ru     2>/dev/null || true
wg-quick down wg_ru  2>/dev/null || true
systemctl disable wg-quick@ru    2>/dev/null || true
systemctl disable wg-quick@wg_ru 2>/dev/null || true
ok "WG"

say "удаляю скрипты"
rm -f /usr/local/bin/ru-failover.py /usr/local/bin/ru-failover.py.bak.* \
      /usr/local/bin/ru-set.sh /usr/local/bin/ru-routes.sh /usr/local/bin/ru-domains.py \
      /usr/local/bin/add-ru-helper.sh /usr/local/bin/add-ams-helper.sh \
      /usr/local/bin/ru-tg-bot.py
ok "скрипты"

say "удаляю cron-задачи"
( crontab -l 2>/dev/null | grep -v 'ru-failover\|ru-domains' || true ) | crontab - || true
ok "cron"

say "удаляю конфиги"
rm -rf /etc/kaskad
rm -f /etc/wireguard/ru.conf /etc/wireguard/wg_ru.conf \
      /etc/wireguard/ru-servers.json /etc/wireguard/ru-extra.list \
      /etc/wireguard/ru-base.aips /etc/wireguard/ru-domains.json \
      /etc/wireguard/notify.env \
      /etc/wireguard/ru.conf.bak.* /etc/wireguard/wg_ru.conf.bak.*
ok "конфиги"

# Опционально — затираем WG-ключи и SSH-ключ бота
if [ "$PURGE_KEYS" = "1" ]; then
    say "затираю ключи (KASKAD_PURGE_KEYS=1)"
    rm -f /etc/wireguard/ru_private.key /etc/wireguard/ru_public.key
    rm -f /root/.ssh/id_ed25519 /root/.ssh/id_ed25519.pub
    ok "ключи затёрты"
else
    warn "ключи /etc/wireguard/ru_*.key и /root/.ssh/id_ed25519 НЕ удалены"
    warn "хочешь грохнуть и их: KASKAD_PURGE_KEYS=1 bash uninstall.sh"
fi

say "удаляю /opt/kaskad"
rm -rf /opt/kaskad /opt/kaskad-webui
ok "/opt/kaskad"

cat <<EOF

${GRN}${BLD}✓ Kaskad удалён.${RST}

Что осталось (можно убрать вручную, если надо):
  • системные пакеты:   apt purge wireguard sshpass python3-flask
  • iptables-persistent: netfilter-persistent save  (после очистки правил)

EOF
