#!/bin/sh
# 配置与证书备份/恢复演练: 备份留在服务器本地受限目录, 私钥不出本机
set -eu

BACKUP_DIR=${BACKUP_DIR:-/var/backups/rdc}
PYTHON=${PYTHON:-/opt/rdc-py312/bin/python3.12}
CERT_CHECK=${CERT_CHECK:-/opt/rdc-relay/bin/cert_check.py}

backup() {
    umask 077
    install -d -m 700 "$BACKUP_DIR"
    stamp=$(date +%Y%m%d-%H%M%S)
    target="$BACKUP_DIR/rdc-config-$stamp.tar.gz"
    # 状态目录含 accounts.db(账号/设备/屏幕墙归属)与 WAL; 不归档会在恢复演练中丢掉墙归属
    set -- etc/rdc-relay etc/rdc-signaling
    for dir in var/lib/rdc-relay var/lib/rdc-signaling; do
        [ -d "/$dir" ] && set -- "$@" "$dir"
    done
    tar czf "$target" -C / "$@"
    chmod 600 "$target"
    # 只保留 14 天, 防止本地备份撑盘
    find "$BACKUP_DIR" -name 'rdc-config-*.tar.gz' -mtime +14 -delete
    echo "BACKUP_OK $target"
    ls -l "$BACKUP_DIR"
    sha256sum "$target"
}

drill() {
    latest=$(ls -1t "$BACKUP_DIR"/rdc-config-*.tar.gz | head -1)
    tmp=$(mktemp -d /tmp/rdc-restore.XXXXXX)
    trap 'rm -rf "$tmp"' EXIT
    tar xzf "$latest" -C "$tmp"
    echo "DRILL_SOURCE $latest"
    echo "DRILL_LIST"; tar tzf "$latest" | sed 's/^/  /'
    # 目录可遍历, 只放开公开文件的读权限; 私钥保持 0600 root
    find "$tmp" -type d -exec chmod 755 {} \;
    chmod 644 "$tmp"/etc/rdc-relay/tls/ca.crt "$tmp"/etc/rdc-relay/tls/server.crt \
              "$tmp"/etc/rdc-relay/relay.conf "$tmp"/etc/rdc-signaling/signaling.conf
    echo "DRILL_VERIFY_CHAIN"
    openssl verify -CAfile "$tmp/etc/rdc-relay/tls/ca.crt" "$tmp/etc/rdc-relay/tls/server.crt"
    echo "DRILL_CERT_CHECK_AS_SERVICE_USER"
    su -s /bin/sh rdc -c "$PYTHON $CERT_CHECK --cert $tmp/etc/rdc-relay/tls/server.crt --warn-days 14 --critical-days 7 --json"
    echo "DRILL_CONFIG_READABLE_AS_SERVICE_USER"
    su -s /bin/sh rdc -c "cat $tmp/etc/rdc-relay/relay.conf >/dev/null && echo relay.conf readable"
    su -s /bin/sh rdc-signaling -c "cat $tmp/etc/rdc-signaling/signaling.conf >/dev/null && echo signaling.conf readable"
    echo "DRILL_KEY_MODE"; find "$tmp/etc" -name '*.key' -exec stat -c '%a %U %n' {} \;
    echo "DRILL_HASH_COMPARE"
    sha256sum "$tmp/etc/rdc-relay/tls/server.crt" "$tmp/etc/rdc-signaling/signaling.conf"
    sha256sum /etc/rdc-relay/tls/server.crt /etc/rdc-signaling/signaling.conf
    # 账号库(屏幕墙归属)必须随备份恢复: 检查 user_version 与 walls 表, 缺库直接判失败
    echo "DRILL_ACCOUNTS_DB"
    for db in "$tmp/var/lib/rdc-relay/accounts.db" "$tmp/var/lib/rdc-signaling/accounts.db"; do
        if [ ! -f "$db" ]; then
            echo "  MISSING $db"
            exit 1
        fi
        "$PYTHON" - "$db" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
try:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    walls = connection.execute("SELECT count(*) FROM walls").fetchone()[0]
    rows = connection.execute("SELECT wall_id, account_id FROM walls ORDER BY rowid").fetchall()
finally:
    connection.close()
print(f"  {sys.argv[1]}: user_version={version} walls={walls}")
for wall_id, account_id in rows:
    print(f"    {wall_id} -> {account_id}")
PY
    done
    echo "DRILL_OK"
}

case ${1:-backup} in
    backup) backup ;;
    drill) drill ;;
    all) backup; drill ;;
    *) echo "用法: $0 [backup|drill|all]" >&2; exit 2 ;;
esac
