"""网络环境修正：让超星站点固定直连。

背景：Windows 上 Clash 等代理工具会把 ``http_proxy`` / ``https_proxy`` 写进用户
环境变量（例如 ``http://127.0.0.1:7897``）。这些变量会被 requests 自动读取，
代理一旦没启动（开机自启尚未起来、手动退出、更新或重载配置时端口短暂关闭），
连接该端口就会被拒绝，表现为::

    HTTPSConnectionPool(host='passport2.chaoxing.com', port=443): Max retries
    exceeded ... ProxyError("Unable to connect to proxy", NewConnectionError(
    ... [WinError 10061] 由于目标计算机积极拒绝，无法连接。))

超星是境内站点，直连即可，所以这里把超星域名写进 ``NO_PROXY``，让 requests
绕过代理；其余主机仍然沿用系统代理设置。

本模块必须在创建任何 ``requests.Session`` 之前被导入，因此由
``utils/__init__.py`` 最先 import（导入即生效）。
"""

import os

# 直连白名单：超星主域及其子域全部命中（requests 用后缀匹配）。
DIRECT_HOSTS = ("chaoxing.com", "chaoxing.cn", "localhost", "127.0.0.1", "::1")


def apply_direct_proxy_bypass(hosts=DIRECT_HOSTS):
    """把 ``hosts`` 追加进 NO_PROXY / no_proxy，重复调用不会产生重复项。

    同时写大小写两种键名，因为 requests 会先读小写再读大写。
    """
    for key in ("NO_PROXY", "no_proxy"):
        entries = [h.strip() for h in os.environ.get(key, "").split(",") if h.strip()]
        known = {h.lower() for h in entries}
        for host in hosts:
            if host.lower() not in known:
                entries.append(host)
                known.add(host.lower())
        os.environ[key] = ",".join(entries)
    return os.environ["NO_PROXY"]


apply_direct_proxy_bypass()
