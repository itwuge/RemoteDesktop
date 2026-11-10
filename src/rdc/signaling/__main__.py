"""信令独立部署入口: python -m rdc.signaling --config /etc/rdc-signaling/signaling.conf.

复用 relay.server 已验证的 TLS 1.3、长度前缀帧与连接治理, 通过
`dataplane_enabled = false` 关闭中继数据面, 只提供信令与预算授予.
"""

from __future__ import annotations

import sys

from rdc.relay.server import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], default_config="/etc/rdc-signaling/signaling.conf"))
