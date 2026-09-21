"""
IPv4 优先补丁（v4.0.1 性能 hotfix）

背景（排查结论）：
    本机网络不通 IPv6，而 dashscope.aliyuncs.com 的 DNS 解析优先返回 2 个 IPv6 地址。
    Python 的 socket 按 getaddrinfo 返回顺序逐个连接：
        第 1 个 IPv6 地址挂 21s 超时 → 第 2 个 IPv6 地址又挂 21s 超时
        → 约 42s 后才回退到 IPv4（0.1s 秒连）
    于是每次 API 调用都固定多出 ~42s 冤枉延迟：
        embedding 单次 42.9s（实测 3 次稳定）
        视觉模型每张 45~55s（42s 超时 + 真实处理 3~13s）
        而 curl 直连仅 0.4s（curl 默认 IPv4 优先，绕过了这个坑）

修复：
    monkeypatch socket.getaddrinfo，过滤掉 IPv6 结果、只保留 IPv4。
    对所有底层使用 socket 的第三方库（requests / httpx / dashscope SDK）全局生效。

用法（幂等，可重复调用）：
    from ipv4_patch import apply_ipv4_first
    apply_ipv4_first()
"""
import socket

_original_getaddrinfo = socket.getaddrinfo
_patched = False


def _ipv4_first(*args, **kwargs):
    """过滤 getaddrinfo 结果：优先只返回 IPv4；若完全没有 IPv4 则回退原始结果"""
    results = _original_getaddrinfo(*args, **kwargs)
    ipv4 = [r for r in results if r[0] == socket.AF_INET]
    return ipv4 if ipv4 else results


def apply_ipv4_first():
    """启用 IPv4 优先补丁（幂等：多次调用只打一次）"""
    global _patched
    if not _patched:
        socket.getaddrinfo = _ipv4_first
        _patched = True
        print("[IPv4补丁] 已启用 IPv4 优先（跳过 IPv6 超时，API 调用不再多等 ~42s）")
