# ==========================================================================================
# 文件        : healthcheck.py
# 所属目录    : deploy/relay/
# 文件用途    : 中继服务健康检查, 供部署脚本、systemd 与人工排障调用
# 架构定位    : 运维工具, 不属于产品运行时代码
# 核心功能    : 读取配置的 health_port, 请求 /healthz, 校验 status 与预算, 退出码 0 或 1
# 职责边界    : 只读检查; 不重启服务, 不改配置, 不输出令牌与凭据
# 运行环境    : Python 3.8 及以上, 只依赖标准库
# 依赖关系    : 读取 /etc/rdc-relay/relay.conf 的 [server] health_port
# ==========================================================================================

"""中继服务健康检查."""

from __future__ import annotations

import configparser
import json
import sys
import urllib.request


def main(argv: list[str]) -> int:
    """执行健康检查."""
    config_path = argv[1] if len(argv) > 1 else "/etc/rdc-relay/relay.conf"
    parser = configparser.ConfigParser()
    if not parser.read(config_path, encoding="utf-8"):
        print(f"FAIL 配置不可读: {config_path}")
        return 1
    port = parser.getint("server", "health_port", fallback=8751)
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as error:  # 健康检查失败一律返回非零, 不吞异常
        print(f"FAIL {url} 请求失败: {type(error).__name__}: {error}")
        return 1
    if body.get("status") != "ok" or body.get("service") != "rdc-relay":
        print(f"FAIL 响应异常: {body}")
        return 1
    if not isinstance(body.get("budget_kbps"), int) or body["budget_kbps"] <= 0:
        print(f"FAIL 预算异常: {body}")
        return 1
    print(f"OK {url} 预算 {body['budget_kbps']} kbps 档位 {body.get('degrade_profile')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
