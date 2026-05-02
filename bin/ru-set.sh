#!/bin/bash
set -u
ID="${1:-}"
[ -z "$ID" ] && { echo "usage: $0 <server_id>"; exit 1; }
DATA=$(python3 -c "
import json, sys
try:
    data = json.load(open('/etc/wireguard/ru-servers.json'))
except Exception as e:
    print('ERR: '+str(e), file=sys.stderr); sys.exit(2)
for s in data['servers']:
    if s['id'] == sys.argv[1]:
        print(s['pubkey'] + '|' + s['endpoint'] + '|' + s['label']); sys.exit(0)
sys.exit(1)
" "$ID")
[ -z "$DATA" ] && { echo "unknown server: $ID"; exit 1; }
PK="${DATA%%|*}"; rest="${DATA#*|}"; EP="${rest%%|*}"; LABEL="${rest#*|}"
sed -i "s|^PublicKey = .*|PublicKey = $PK|" /etc/wireguard/ru.conf
sed -i "s|^Endpoint = .*|Endpoint = $EP|" /etc/wireguard/ru.conf
wg syncconf ru <(wg-quick strip ru)
/usr/local/bin/ru-routes.sh apply >/dev/null 2>&1 || true
mkdir -p /var/lib/ru-failover
date +%s > /var/lib/ru-failover/last_switch
echo 0   > /var/lib/ru-failover/test_started
echo "OK: ru → $LABEL ($EP)"
