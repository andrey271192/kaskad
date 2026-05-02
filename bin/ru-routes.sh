#!/bin/bash
# /usr/local/bin/ru-routes.sh — управление extra-маршрутами через ru-туннель
set -u
LIST=/etc/wireguard/ru-extra.list
BASE=/etc/wireguard/ru-base.aips
CONF=/etc/wireguard/ru.conf
XRAY_IF="${XRAY_IF:-amn0}"
MARK=100

touch "$LIST"; chmod 600 "$LIST"

valid_cidr() {
  [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$ ]] || return 1
  IFS=/ read -r ip mask <<< "$1"
  IFS=. read -r a b c d <<< "$ip"
  for o in $a $b $c $d; do (( o < 0 || o > 255 )) && return 1; done
  if [[ -n "${mask:-}" ]]; then (( mask < 0 || mask > 32 )) && return 1; fi
  return 0
}
normalize() { local n="$1"; [[ "$n" == */* ]] || n="$n/32"; echo "$n"; }

peer_pk() { awk '/^\[Peer\]/{p=1} p && /^PublicKey *= /{sub(/^PublicKey *= */,""); print; exit}' "$CONF"; }
current_aips() { awk '/^\[Peer\]/{p=1} p && /^AllowedIPs *= /{sub(/^AllowedIPs *= */,""); print; exit}' "$CONF" | tr -d ' '; }

ensure_base() {
  if [ ! -f "$BASE" ]; then
    current_aips > "$BASE"
    chmod 600 "$BASE"
  fi
}

sync_wg() {
  ensure_base
  local pk base extra all
  pk=$(peer_pk)
  base=$(cat "$BASE")
  extra=$(grep -vE '^[[:space:]]*(#|$)' "$LIST" | tr -d ' ' | tr '\n' ',' | sed 's/,$//')
  if [ -n "$extra" ]; then all="$base,$extra"; else all="$base"; fi
  python3 - "$CONF" "$all" <<'PY'
import sys, re, pathlib
p = pathlib.Path(sys.argv[1])
t = p.read_text()
t = re.sub(r'^AllowedIPs *= .*$', 'AllowedIPs = ' + sys.argv[2], t, count=1, flags=re.M)
p.write_text(t)
PY
  wg set ru peer "$pk" allowed-ips "$all" 2>&1
}

apply_route() {
  local net="$1"
  ip route replace "$net" dev ru 2>/dev/null || true
  iptables -t mangle -C PREROUTING -i "$XRAY_IF" -d "$net" -j MARK --set-mark $MARK 2>/dev/null \
    || iptables -t mangle -A PREROUTING -i "$XRAY_IF" -d "$net" -j MARK --set-mark $MARK 2>/dev/null || true
}
remove_route() {
  local net="$1"
  ip route del "$net" dev ru 2>/dev/null || true
  iptables -t mangle -D PREROUTING -i "$XRAY_IF" -d "$net" -j MARK --set-mark $MARK 2>/dev/null || true
}

cmd_list() { if [ -s "$LIST" ]; then cat "$LIST"; else echo "(пусто)"; fi; }

cmd_add() {
  local added=0 skipped=0 invalid=()
  for raw in "$@"; do
    if ! valid_cidr "$raw"; then invalid+=("$raw"); continue; fi
    local net; net=$(normalize "$raw")
    if grep -qxF "$net" "$LIST"; then ((skipped++)); else echo "$net" >> "$LIST"; ((added++)); fi
    apply_route "$net"
  done
  sync_wg >/dev/null
  printf "added=%d skipped=%d invalid=%d" "$added" "$skipped" "${#invalid[@]}"
  ((${#invalid[@]})) && printf " (%s)" "${invalid[*]}"
  echo
}

cmd_remove() {
  local removed=0 missing=0
  for raw in "$@"; do
    valid_cidr "$raw" || continue
    local net; net=$(normalize "$raw")
    if grep -qxF "$net" "$LIST"; then
      grep -vxF "$net" "$LIST" > "$LIST.tmp"; mv "$LIST.tmp" "$LIST"
      ((removed++))
    else ((missing++)); fi
    remove_route "$net"
  done
  sync_wg >/dev/null
  echo "removed=$removed missing=$missing"
}

cmd_clear() {
  local n=0
  while IFS= read -r net; do
    [[ -z "$net" || "$net" == \#* ]] && continue
    remove_route "$net"; ((n++))
  done < "$LIST"
  : > "$LIST"
  sync_wg >/dev/null
  echo "cleared=$n"
}

cmd_apply() {
  local n=0
  while IFS= read -r net; do
    [[ -z "$net" || "$net" == \#* ]] && continue
    apply_route "$net"; ((n++))
  done < "$LIST"
  sync_wg >/dev/null
  echo "applied=$n"
}

case "${1:-}" in
  list) cmd_list ;;
  add) shift; cmd_add "$@" ;;
  remove|del) shift; cmd_remove "$@" ;;
  clear) cmd_clear ;;
  apply|post-up) cmd_apply ;;
  *) echo "usage: $0 {list|add NET...|remove NET...|clear|apply}"; exit 1 ;;
esac
