#!/bin/sh
# 证书巡检包装: 同时检查 relay 与 signaling 配置, 非零退出时输出明确告警
DIR=$(dirname "$0")
overall=0
for cfg in /etc/rdc-relay/relay.conf /etc/rdc-signaling/signaling.conf; do
  [ -f "$cfg" ] || continue
  @PYTHON@ "$DIR/cert_check.py" --config "$cfg" --warn-days 14 --critical-days 7 --json
  code=$?
  [ "$code" -ne 0 ] && overall=$code
done
if [ "$overall" -ne 0 ]; then
  echo "CERT_CHECK_ALERT exit=$overall certs=/etc/rdc-relay/tls/server.crt; 1=WARN 2=CRITICAL; 排查: journalctl -u rdc-cert-check -n 50 --no-pager" >&2
fi
exit "$overall"
