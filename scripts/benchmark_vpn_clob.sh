#!/usr/bin/env bash
# Benchmark HTTPS latency to Polymarket CLOB across NetworkManager VPN profiles.
#
# See project docs for VPN benchmarking notes if needed.
#
# Requires: NetworkManager (nmcli), curl. Secrets should be stored in NM (no password prompt).
#
# Usage:
#   chmod +x hft_bot/scripts/benchmark_vpn_clob.sh
#   ./hft_bot/scripts/benchmark_vpn_clob.sh
#
# Env:
#   CLOB_URL           default https://clob.polymarket.com/
#   BENCHMARK_RUNS     default 3
#   VPN_SETTLE_SEC     seconds after "up" before curl (default 3)
#   VPN_ONLY_UUID      if set, only this NM connection UUID is benchmarked
#   VPN_SILENT_NMCLI   if 1, hide nmcli stderr on failure (default 0 = show error text)

set -euo pipefail

CLOB_URL="${CLOB_URL:-https://clob.polymarket.com/}"
BENCHMARK_RUNS="${BENCHMARK_RUNS:-3}"
VPN_SETTLE_SEC="${VPN_SETTLE_SEC:-3}"
VPN_ONLY_UUID="${VPN_ONLY_UUID:-}"
VPN_SILENT_NMCLI="${VPN_SILENT_NMCLI:-0}"

die() { echo "error: $*" >&2; exit 1; }

command -v nmcli >/dev/null 2>&1 || die "nmcli not found"
command -v curl >/dev/null 2>&1 || die "curl not found"

VPN_TYPES='^(vpn|wireguard|openvpn|pptp|l2tp|openconnect)$'

# UUID + TYPE (no display name)
mapfile -t _uuid_types < <(
  nmcli -t -f UUID,TYPE connection show 2>/dev/null | awk -F: -v re="$VPN_TYPES" '
    NF == 2 && $2 ~ re { print $1 "\t" $2 }'
)

[[ ${#_uuid_types[@]} -gt 0 ]] || die "no VPN/WireGuard/OpenVPN profiles in NetworkManager"

if [[ -n "$VPN_ONLY_UUID" ]]; then
  _filtered=()
  for ut in "${_uuid_types[@]}"; do
    IFS=$'\t' read -r u _ <<<"$ut"
    if [[ "$u" == "$VPN_ONLY_UUID" ]]; then
      _filtered+=("$ut")
      break
    fi
  done
  if [[ ${#_filtered[@]} -eq 0 ]]; then
    die "VPN_ONLY_UUID=$VPN_ONLY_UUID not found among VPN-type connections (see nmcli -t -f UUID,TYPE connection show)"
  fi
  _uuid_types=("${_filtered[@]}")
fi

# Resolve rough location label for an IP (ipinfo.io).
get_location_for_ip() {
  local ip="$1"
  local info city country
  info=$(curl -s "https://ipinfo.io/$ip/json")
  city=$(echo "$info" | grep '"city"' | awk -F'"' '{print $4}')
  country=$(echo "$info" | grep '"country"' | awk -F'"' '{print $4}')
  echo "$country, $city"
}

# Human-readable profile label (optional location suffix).
name_for_uuid() {
  local uuid="$1"
  local base_name
  base_name=$(nmcli -g connection.id connection show uuid "$uuid" 2>/dev/null || echo "$uuid")

  # If LOC_TEMP is set, prefix it (VPN egress location).
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
[[ -n "$VPN_ONLY_UUID" ]] && echo "VPN_ONLY_UUID=$VPN_ONLY_UUID (single profile)"
echo ""

for ut in "${_uuid_types[@]}"; do
  IFS=$'\t' read -r uuid typ <<<"$ut"
  _pname=$(nmcli -g connection.id connection show uuid "$uuid" 2>/dev/null || echo "$uuid")

  # Bring VPN up
  ok=0
  _nm_out=""
  if _nm_out=$(nmcli connection up uuid "$uuid" 2>&1); then
    ok=1
  else
    echo "  connection up FAILED: $_pname  ($typ)"
    echo "    hint: save VPN password/key in NetworkManager for all users, or fix the profile."
    if [[ "$VPN_SILENT_NMCLI" != "1" && -n "$_nm_out" ]]; then
      printf '%s\n' "$_nm_out" | sed 's/^/    nmcli: /'
    fi
  fi

  if [[ "$ok" -eq 1 ]]; then
    sleep "$VPN_SETTLE_SEC"

    # Current public IP over VPN
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

  # Tear VPN down
  down_if_vpn_uuid "$uuid"
  echo ""
done

echo "========== Summary (avg_s / min_s / max_s = seconds to complete HTTPS request) =========="
column -t -s $'\t' <"$results_file" | sed 's/^/  /'

if [[ -n "$_prev_vpn_uuid" ]]; then
  echo ""
  echo "Note: a VPN was active before this run (uuid=$_prev_vpn_uuid). Reconnect manually if needed."
fi