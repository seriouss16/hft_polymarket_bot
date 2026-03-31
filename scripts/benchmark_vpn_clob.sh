#!/usr/bin/env bash
# Benchmark HTTPS latency to Polymarket CLOB across NetworkManager VPN profiles.
#
# Instructions (RU): see benchmark_vpn_clob.md in this directory.
#
# Requires: NetworkManager (nmcli), curl. Secrets should be stored in NM (no password prompt).
#
# Usage:
#   chmod +x hft_bot/scripts/benchmark_vpn_clob.sh
#   ./hft_bot/scripts/benchmark_vpn_clob.sh
#
# Env:
#   CLOB_URL          default https://clob.polymarket.com/
#   BENCHMARK_RUNS    default 3
#   VPN_SETTLE_SEC    seconds after "up" before curl (default 3)

#!/usr/bin/env bash
# Benchmark HTTPS latency to Polymarket CLOB across NetworkManager VPN profiles.
#
# Автоматическое определение местоположения VPN по публичному IP.
#
# Usage:
#   chmod +x hft_bot/scripts/benchmark_vpn_clob.sh
#   ./hft_bot/scripts/benchmark_vpn_clob.sh

set -euo pipefail

CLOB_URL="${CLOB_URL:-https://clob.polymarket.com/}"
BENCHMARK_RUNS="${BENCHMARK_RUNS:-3}"
VPN_SETTLE_SEC="${VPN_SETTLE_SEC:-3}"

die() { echo "error: $*" >&2; exit 1; }

command -v nmcli >/dev/null 2>&1 || die "nmcli not found"
command -v curl >/dev/null 2>&1 || die "curl not found"

VPN_TYPES='^(vpn|wireguard|openvpn|pptp|l2tp|openconnect)$'

# UUID + TYPE (без имени)
mapfile -t _uuid_types < <(
  nmcli -t -f UUID,TYPE connection show 2>/dev/null | awk -F: -v re="$VPN_TYPES" '
    NF == 2 && $2 ~ re { print $1 "\t" $2 }'
)

[[ ${#_uuid_types[@]} -gt 0 ]] || die "no VPN/WireGuard/OpenVPN profiles in NetworkManager"

# Функция для получения местоположения по IP
get_location_for_ip() {
  local ip="$1"
  local info city country
  info=$(curl -s "https://ipinfo.io/$ip/json")
  city=$(echo "$info" | grep '"city"' | awk -F'"' '{print $4}')
  country=$(echo "$info" | grep '"country"' | awk -F'"' '{print $4}')
  echo "$country, $city"
}

# Получение читаемого имени профиля с местоположением
name_for_uuid() {
  local uuid="$1"
  local base_name
  base_name=$(nmcli -g connection.id connection show uuid "$uuid" 2>/dev/null || echo "$uuid")

  # Если переменная LOC_TEMP установлена, использовать её
  if [[ -n "${LOC_TEMP:-}" ]]; then
    echo "$LOC_TEMP - $base_name"
  else
    echo "$base_name"
  fi
}

measure_runs() {
  local i
  for i in $(seq 1 "$BENCHMARK_RUNS"); do
    curl -o /dev/null -sS --connect-timeout 8 --max-time 25 \
      -w '%{time_total}\n' "$CLOB_URL" 2>/dev/null || echo "nan"
  done | awk '
    /^nan$/ { next }
    $1 == "" { next }
    {
      v = $1 + 0.0
      if (n == 0) { min = v; max = v }
      if (v < min) min = v
      if (v > max) max = v
      sum += v
      n++
    }
    END {
      if (n > 0) printf "%.6f\t%.6f\t%.6f\t%d\n", sum / n, min, max, n
      else print "nan\tnan\tnan\t0"
    }'
}

down_if_vpn_uuid() {
  local uuid="$1"
  local typ
  typ=$(nmcli -g connection.type connection show uuid "$uuid" 2>/dev/null || true)
  if [[ "$typ" =~ $VPN_TYPES ]]; then
    nmcli connection down uuid "$uuid" >/dev/null 2>&1 || true
  fi
}

_prev_vpn_uuid=""
_prev_vpn_uuid=$(
  nmcli -t -f UUID,TYPE connection show --active 2>/dev/null \
    | awk -F: -v re="$VPN_TYPES" 'NF == 2 && $2 ~ re { print $1; exit }'
) || true

results_file=$(mktemp)
trap 'rm -f "$results_file"' EXIT

printf '%s\n' "uuid	name	type	ok	avg_s	min_s	max_s	ok_runs" >"$results_file"

echo "CLOB_URL=$CLOB_URL  runs=$BENCHMARK_RUNS  settle=${VPN_SETTLE_SEC}s  profiles=${#_uuid_types[@]}"
echo ""

for ut in "${_uuid_types[@]}"; do
  IFS=$'\t' read -r uuid typ <<<"$ut"

  # Поднятие VPN
  ok=0
  if nmcli connection up uuid "$uuid" >/dev/null 2>&1; then
    ok=1
  else
    echo "  connection up FAILED (save password in NM or fix profile)"
  fi

  if [[ "$ok" -eq 1 ]]; then
    sleep "$VPN_SETTLE_SEC"

    # Получаем текущий публичный IP VPN
    IP=$(curl -s https://ipinfo.io/ip || echo "")
    if [[ -n "$IP" ]]; then
      LOC_TEMP=$(get_location_for_ip "$IP")
    else
      LOC_TEMP=""
    fi

    name=$(name_for_uuid "$uuid")
    IFS=$'\t' read -r avg min max nruns <<<"$(measure_runs)" || true
  else
    LOC_TEMP=""
    name=$(name_for_uuid "$uuid")
    avg="nan"; min="nan"; max="nan"; nruns=0
  fi

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$uuid" "$name" "$typ" "$ok" "$avg" "$min" "$max" "$nruns" >>"$results_file"

  # Отключаем VPN
  down_if_vpn_uuid "$uuid"
  echo ""
done

echo "========== Summary (avg_s / min_s / max_s = seconds to complete HTTPS request) =========="
column -t -s $'\t' <"$results_file" | sed 's/^/  /'

if [[ -n "$_prev_vpn_uuid" ]]; then
  echo ""
  echo "Note: a VPN was active before this run (uuid=$_prev_vpn_uuid). Reconnect manually if needed."
fi