# 文件: __main__.py | 用途: python -m rdc.relay 入口 | 边界: 只转发到 server.main
"""模块入口."""

from rdc.relay.server import main

if __name__ == "__main__":
    raise SystemExit(main())
