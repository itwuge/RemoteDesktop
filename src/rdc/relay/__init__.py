# 文件: __init__.py | 用途: 中继服务包入口 | 边界: 只暴露配置与服务类, 不引入客户端模块
"""中继服务层: 可部署的信令接入与健康检查."""

from rdc.relay.server import RelayConfig, RelayServer, load_config

__all__ = ["RelayConfig", "RelayServer", "load_config"]
