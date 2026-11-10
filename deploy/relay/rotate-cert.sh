#!/usr/bin/env bash
# 证书轮换：重签服务端证书（可选同时轮换 CA），原子替换后 SIGHUP 热加载，不重启服务。
# 用法:
#   ./rotate-cert.sh                         # 用现有 CA 重签服务端证书，客户端无需改信任
#   ./rotate-cert.sh --rotate-ca             # 同时轮换 CA，客户端必须更新 ca.crt
#   ./rotate-cert.sh --days 90 --ca-days 3650
set -euo pipefail

TLS_DIR="${TLS_DIR:-/etc/rdc-relay/tls}"
SERVICE="${SERVICE:-rdc-relay}"
CONFIG="${CONFIG:-/etc/rdc-relay/relay.conf}"
GROUP="${GROUP:-rdc}"
CN="${RELAY_CN:-relay.example.com}"
DAYS=90
CA_DAYS=3650
SAN="${RELAY_SAN:-DNS:relay.example.com,DNS:localhost,IP:127.0.0.1}"
ROTATE_CA=0
RELOAD=1

while [ $# -gt 0 ]; do
  case "$1" in
    --rotate-ca) ROTATE_CA=1 ;;
    --days) DAYS="$2"; shift ;;
    --ca-days) CA_DAYS="$2"; shift ;;
    --san) SAN="$2"; shift ;;
    --tls-dir) TLS_DIR="$2"; shift ;;
    --service) SERVICE="$2"; shift ;;
    --config) CONFIG="$2"; shift ;;
    --no-reload) RELOAD=0 ;;
    *) echo "未知参数: $1" >&2; exit 64 ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || { echo "必须以 root 执行" >&2; exit 1; }
command -v openssl >/dev/null || { echo "缺少 openssl" >&2; exit 1; }
[ -f "$TLS_DIR/ca.crt" ] && [ -f "$TLS_DIR/ca.key" ] || { echo "缺少 CA: $TLS_DIR/ca.crt|ca.key" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== 轮换前 =="
openssl x509 -in "$TLS_DIR/server.crt" -noout -serial -dates -fingerprint -sha256 | sed 's/^/  /'

if [ "$ROTATE_CA" -eq 1 ]; then
  echo "== 生成新 CA（$CA_DAYS 天） =="
  openssl req -x509 -newkey rsa:2048 -nodes -days "$CA_DAYS" \
    -keyout "$WORK/ca.key" -out "$WORK/ca.crt" -subj "/CN=RDC-Relay-CA" 2>/dev/null
  install -m 0600 -o root -g root "$WORK/ca.key" "$TLS_DIR/ca.key"
  install -m 0644 -o root -g root "$WORK/ca.crt" "$TLS_DIR/ca.crt"
fi

echo "== 重签服务端证书（$DAYS 天） =="
openssl req -newkey rsa:2048 -nodes -keyout "$WORK/server.key" -out "$WORK/server.csr" \
  -subj "/CN=$CN" 2>/dev/null
printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\n' "$SAN" > "$WORK/server.ext"
openssl x509 -req -in "$WORK/server.csr" -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" \
  -CAcreateserial -days "$DAYS" -out "$WORK/server.crt" -extfile "$WORK/server.ext" 2>/dev/null
openssl verify -CAfile "$TLS_DIR/ca.crt" "$WORK/server.crt" >/dev/null

echo "== 原子替换 =="
install -m 0640 -o root -g "$GROUP" "$WORK/server.key" "$TLS_DIR/server.key.new"
install -m 0644 -o root -g root "$WORK/server.crt" "$TLS_DIR/server.crt.new"
mv -f "$TLS_DIR/server.key.new" "$TLS_DIR/server.key"
mv -f "$TLS_DIR/server.crt.new" "$TLS_DIR/server.crt"
openssl x509 -in "$TLS_DIR/server.crt" -noout -serial -dates -fingerprint -sha256 | sed 's/^/  /'

if [ "$RELOAD" -eq 1 ]; then
  echo "== SIGHUP 热加载（不重启进程） =="
  PID="$(systemctl show -p MainPID --value "$SERVICE")"
  if [ -z "$PID" ] || [ "$PID" = "0" ]; then
    echo "未取到 $SERVICE 主进程, 跳过热加载" >&2
    exit 1
  fi
  kill -HUP "$PID"
  sleep 1
  echo "== 热加载后校验 =="
  if command -v curl >/dev/null; then
    curl -sS -m 5 http://127.0.0.1:8751/healthz | tr ',' '\n' | grep -E 'cert_days_left|"status"' || true
  fi
  echo | timeout 8 openssl s_client -connect 127.0.0.1:8750 -servername "$CN" \
    -CAfile "$TLS_DIR/ca.crt" -verify_return_error 2>&1 \
    | grep -E 'Protocol|Verify return code|subject=' | sed 's/^/  /'
  journalctl -u "$SERVICE" -n 20 --no-pager | grep -E 'tls.reloaded|tls.reload_failed' | tail -1 || true
fi

if [ "$ROTATE_CA" -eq 1 ]; then
  echo "== 客户端需更新 CA =="
  echo "  新 CA: $TLS_DIR/ca.crt"
  sha256sum "$TLS_DIR/ca.crt" | sed 's/^/  /'
  echo "  分发到各控制端后，其固定 CA 校验才能继续通过；只重签服务端证书时客户端无需改动。"
else
  echo "== 客户端无需改动（CA 未变） =="
fi
