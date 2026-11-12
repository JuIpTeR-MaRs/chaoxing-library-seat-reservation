import os
import unittest

import requests

from utils import net


class DirectProxyBypassTest(unittest.TestCase):
    def test_chaoxing_domains_are_in_no_proxy(self):
        no_proxy = os.environ["NO_PROXY"].split(",")
        self.assertIn("chaoxing.com", no_proxy)
        self.assertIn("chaoxing.cn", no_proxy)

    def test_requests_bypasses_proxy_for_chaoxing(self):
        # 模拟系统里存在一个没启动的代理（127.0.0.1:7897 是 Clash 的默认端口）
        proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        old = {k: os.environ.get(k) for k in proxy_keys}
        for key in proxy_keys:
            os.environ[key] = "http://127.0.0.1:7897"
        try:
            session = requests.Session()
            # 超星域名解析出的代理为空 -> 直连，不会再报 WinError 10061
            self.assertEqual(
                {},
                session.merge_environment_settings(
                    "https://passport2.chaoxing.com/mlogin?loginType=1&fid=",
                    {},
                    None,
                    None,
                    {},
                )["proxies"],
            )
            self.assertEqual(
                {},
                session.merge_environment_settings(
                    "https://office.chaoxing.com/data/apps/seat/submit",
                    {},
                    None,
                    None,
                    {},
                )["proxies"],
            )
            # 非超星域名仍然沿用系统代理
            self.assertEqual(
                "http://127.0.0.1:7897",
                session.merge_environment_settings(
                    "https://example.com/", {}, None, None, {}
                )["proxies"]["https"],
            )
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_apply_is_idempotent(self):
        first = net.apply_direct_proxy_bypass()
        second = net.apply_direct_proxy_bypass()
        self.assertEqual(first, second)
        self.assertEqual(second.count("chaoxing.com"), 1)


if __name__ == "__main__":
    unittest.main()
