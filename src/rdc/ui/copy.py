# ==========================================================================================
# 文件        : copy.py
# 所属目录    : src/rdc/ui/
# 文件用途    : 界面可见中文文案的唯一来源, 含契约错误码到中文原因的映射
# 架构定位    : 界面文案层, 只被 ui 模块引用; 不依赖命令层实现
# 核心功能    : error_text() 保证界面只出现中文原因, 错误码留给日志与结构化字段
# 职责边界    : 不改变命令层返回的 message 与 error_code; 不翻译品牌名、算法名与设备码
# ==========================================================================================

"""界面文案常量.

界面可见处一律显示中文原因; 契约错误码只保留在结构化字段与日志里.
同一原因只保留一种说法, 命令层已经给出中文 message 时优先沿用, 不再另写一套.
"""

from __future__ import annotations

import re

from rdc.core import errors

# 错误码到中文原因的兜底映射; 命令层 message 已是中文时以 message 为准
ERROR_TEXT: dict[str, str] = {
    errors.DEVICE_NOT_FOUND: "设备不存在, 请检查设备编号",
    errors.ACCESS_CODE_INVALID: "连接码不正确, 请确认后重试",
    errors.SESSION_BUSY: "本机已有会话, 请先结束",
    errors.TASK_NOT_FOUND: "任务不存在或已结束",
    errors.SESSION_NOT_FOUND: "会话不存在或已结束",
    errors.PROFILE_UNSUPPORTED: "画质档位不受支持",
    errors.PERMISSION_DENIED: "没有权限执行该操作",
    errors.HOSTING_ALREADY_ACTIVE: "被控端已经在运行",
    errors.HOSTING_NOT_ACTIVE: "被控端尚未启动",
    errors.POLICY_INVALID: "策略取值不合法",
    errors.RELAY_UNREACHABLE: "中继不可达, 请检查网络",
    errors.ACCOUNT_NOT_FOUND: "账号不存在",
    errors.CREDENTIAL_INVALID: "凭据无效, 请重新登录",
    errors.TOKEN_EXPIRED: "登录状态已过期, 请重新登录",
    errors.TOKEN_REVOKED: "登录状态已失效, 请重新登录",
    errors.TOKEN_INVALID: "令牌无效, 请重新登录",
    errors.GRANT_INVALID: "授权无效或已过期",
    errors.REQUEST_NOT_FOUND: "申请不存在或已处理",
    errors.WALL_BUDGET_EXCEEDED: "屏幕墙预算不足",
    errors.WALL_NOT_FOUND: "屏幕墙不存在",
    errors.PREVIEW_TOKEN_INVALID: "预览令牌无效",
    errors.WALL_OWNERSHIP_CONFLICT: "屏幕墙归属冲突",
    errors.SCOPE_INVALID: "授权范围不合法",
    errors.SCOPE_INSUFFICIENT: "授权范围不足",
    errors.GRANT_NOT_TRANSFERABLE: "该授权不可转授",
    errors.GRANT_SCOPE_EXCEEDED: "超出授权范围",
    errors.TRANSFER_NOT_FOUND: "传输任务不存在",
    errors.TRANSFER_PERMISSION_DENIED: "没有文件传输权限",
    errors.TRANSFER_HASH_MISMATCH: "传输文件摘要不一致",
    errors.CHUNK_CHECKSUM_MISMATCH: "数据块校验失败",
    errors.TRANSFER_IO_ERROR: "文件读写失败",
    errors.TRANSFER_RATE_INVALID: "限速取值不合法",
    errors.CLIPBOARD_DISABLED: "剪贴板同步未开启",
    errors.CLIPBOARD_UNSUPPORTED_KIND: "剪贴板内容类型不支持",
    errors.CLIPBOARD_TOO_LARGE: "剪贴板内容超过大小上限",
    errors.CLIPBOARD_EMPTY: "剪贴板内容为空",
    errors.CLIPBOARD_DECODE_ERROR: "剪贴板内容解析失败",
    errors.CLIPBOARD_DIGEST_MISMATCH: "剪贴板内容摘要不一致",
    errors.SESSION_CHANNEL_UNAVAILABLE: "会话通道不可用",
    errors.SESSION_CHANNEL_TIMEOUT: "会话通道超时",
    errors.CHANNEL_BACKPRESSURE: "通道拥塞, 请稍后重试",
    errors.UPDATE_SOURCE_UNREACHABLE: "更新源不可达, 请稍后重试",
    errors.UPDATE_MANIFEST_INVALID: "更新清单格式不正确",
    errors.UPDATE_HASH_MISMATCH: "更新包摘要不一致, 已拒绝安装",
    errors.UPDATE_DOWNGRADE_REJECTED: "目标版本低于当前版本, 已拒绝",
    errors.UPDATE_SIGNATURE_INVALID: "更新包签名校验失败",
    errors.UPDATE_SOURCE_INSECURE: "更新源不是安全连接, 已拒绝",
    errors.AUTH_FAILED: "身份认证失败",
    errors.PEER_OFFLINE: "对端设备不在线",
    errors.RELAY_FULL: "中继已满, 请稍后重试",
    errors.RELAY_UNAVAILABLE: "中继不可用",
    errors.PROTOCOL_VERSION_UNSUPPORTED: "协议版本不受支持",
    errors.RATE_LIMITED: "请求过于频繁, 请稍后重试",
    errors.FILE_OFFER_REJECTED: "对端拒绝了文件传输",
    errors.FILE_CHUNK_INVALID: "文件数据块不合法",
    errors.CAPTURE_PERMISSION_DENIED: "没有屏幕采集权限",
    errors.CAPTURE_UNAVAILABLE: "屏幕采集不可用",
    errors.CAPTURE_TIMEOUT: "屏幕采集超时",
    errors.ENCODER_UNAVAILABLE: "编码器不可用",
    errors.ENCODE_FAILED: "画面编码失败",
    errors.DECODER_UNAVAILABLE: "解码器不可用",
    errors.DECODE_FAILED: "画面解码失败",
    errors.DIRECT_FAILED: "直连失败, 已尝试降级",
    errors.TRANSPORT_CLOSED: "传输连接已关闭",
    errors.INPUT_PERMISSION_DENIED: "没有键鼠控制权限",
    errors.SIGNALING_UNREACHABLE: "账号中心或信令服务不可达, 请检查网络",
    errors.CONFIG_INVALID: "配置不合法, 请检查设置",
}

FALLBACK_TEXT = "操作失败, 请稍后重试"
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")

# 设备平台名按「产品名 + 中文描述」展示; unknown 必须落到中文兜底, 不回退英文原值
PLATFORM_TEXT: dict[str, str] = {
    "windows": "Windows 电脑",
    "win": "Windows 电脑",
    "macos": "macOS 电脑",
    "mac": "macOS 电脑",
    "linux": "Linux 电脑",
    "android": "安卓手机",
    "ios": "iPhone",
    "iphone": "iPhone",
    "unknown": "平台未知",
}

# 会话状态词; 禁止把 streaming/connected 之类英文原样带进界面
STATE_TEXT: dict[str, str] = {
    "idle": "空闲",
    "pending": "等待连接",
    "connecting": "连接中",
    "connected": "已连接",
    "streaming": "正在传输",
    "reconnecting": "重连中",
    "recovered": "已恢复",
    "failed": "已断开",
    "closed": "已结束",
    "done": "已完成",
    "canceled": "已取消",
}

TRANSPORT_TEXT: dict[str, str] = {
    "": "-",
    "p2p": "点对点直连",
    "direct": "直连",
    "relay": "中继",
    "local": "本机",
}

PROFILE_TEXT: dict[str, str] = {
    "": "-",
    "auto": "自动",
    "720p30": "720P 30 帧",
    "1080p30": "1080P 30 帧",
    "1080p60": "1080P 60 帧",
    "1440p60": "1440P 60 帧",
    "4k60": "4K 60 帧",
    "preview_480p15": "预览 480P 15 帧",
    "L0": "流畅档",
    "L1": "均衡档",
    "L2": "高清档",
    "L3": "超清档",
}

RESULT_TEXT: dict[str, str] = {
    "ok": "成功",
    "success": "成功",
    "fail": "失败",
    "failed": "失败",
    "deny": "已拒绝",
    "denied": "已拒绝",
}

UNKNOWN_TEXT = "未知"


def platform_text(value: str | None) -> str:
    """平台名: 已知产品名保留并补中文描述, 未知平台一律中文兜底."""
    key = (value or "").strip().lower()
    return PLATFORM_TEXT.get(key, "平台未知")


def state_text(value: str | None) -> str:
    """会话状态中文; 未收录的状态值不裸展示英文."""
    key = (value or "").strip().lower()
    return STATE_TEXT.get(key, "状态未知")


def transport_text(value: str | None) -> str:
    """通路中文: p2p / relay 只在结构化字段保留."""
    return TRANSPORT_TEXT.get((value or "").strip().lower(), "未知通路")


def profile_text(value: str | None) -> str:
    """画质档位中文; 未收录的档位不裸展示英文."""
    return PROFILE_TEXT.get((value or "").strip(), "未知档位")


def result_text(value: str | None) -> str:
    """结果徽标中文; 未收录时按「未知」兜底."""
    return RESULT_TEXT.get((value or "").strip().lower(), UNKNOWN_TEXT)


def trusted_source_text(value: str | None) -> str:
    """受信来源中文: 账号授信 / 设备授信."""
    return {"device": "设备授信", "owner": "账号授信"}.get((value or "").strip(), "账号授信")


# 已格式化字符串里可能残留 "CODE: " 或 "[CODE]"/"（错误码：CODE）"; 只清理已知契约错误码
_INLINE_CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{2,})\s*[:：]\s*")
_BRACKETED_CODE_PATTERN = re.compile(r"\s*[\[【(（]\s*(?:错误码\s*[:：]?\s*)?([A-Z][A-Z0-9_]{2,})\s*[\]】)）]")
_ANY_BRACKET_CODE_PATTERN = re.compile(r"[\[【(（]\s*([A-Z][A-Z0-9_]{2,})\s*[\]】)）]")
_EMPTY_BRACKET_PATTERN = re.compile(r"\s*[\[【(（]\s*(?:错误码\s*[:：]?\s*)?[\]】)）]")


def _strip_known_codes(text: str) -> str:
    """去掉文案里已经拼进去的已知错误码, 保留中文原因与设备码."""

    def inline(match: re.Match[str]) -> str:
        return "" if match.group(1) in ERROR_TEXT else match.group(0)

    def bracketed(match: re.Match[str]) -> str:
        return "" if match.group(1) in ERROR_TEXT else match.group(0)

    cleaned = _INLINE_CODE_PATTERN.sub(inline, text)
    for _ in range(2):
        staged = _BRACKETED_CODE_PATTERN.sub(bracketed, cleaned)
        staged = _ANY_BRACKET_CODE_PATTERN.sub(bracketed, staged)
        staged = _EMPTY_BRACKET_PATTERN.sub("", staged)
        if staged == cleaned:
            break
        cleaned = staged
    return cleaned.strip()


def error_text(code: str | None, message: str = "") -> str:
    """返回界面可见的中文原因; 绝不把错误码带进用户可见文案.

    命令层的中文 message 已是权威说法, 直接沿用; message 为空或非中文时按错误码兜底;
    两者都拿不到时给统一中文兜底. ``code`` 本身仍保留在 CommandResult 与日志中.
    已格式化的 message 里残留的已知错误码会先剔除, 防止字符串拼接绕过映射.
    """
    text = _strip_known_codes((message or "").strip())
    if text and CJK_PATTERN.search(text):
        return text
    return ERROR_TEXT.get((code or "").strip(), FALLBACK_TEXT)


def message_text(code: str | None, message: str, *, ok: bool) -> str:
    """命令结果的可见文案: 成功原样展示, 失败一律走错误映射, 界面不出现错误码."""
    return (message or "").strip() if ok else error_text(code, message)
