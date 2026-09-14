"""真机验证链接预览的 SSRF 防护（用服务器上真实的 config.toml）。

    ~/maimai/MaiBot/.venv/bin/python check_ssrf.py

会验证：
  1. 内网 / 本机 / 云 metadata 链接被拦下且**不发请求**；
  2. 公网链接照常抓取（拿真实 Steam 公告试）；
  3. 被拦下的推文仍能正常投递（只是不带链接内容块）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import tomllib
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PLUGIN_DIR / "config.toml"

spec = importlib.util.spec_from_file_location("tw_ssrf_check", PLUGIN_DIR / "plugin.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("check-ssrf")

FAILURES: list[str] = []


def check(ok: bool, message: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'} {message}")
    if not ok:
        FAILURES.append(message)


class FakePaths:
    def __init__(self, path: Path) -> None:
        self.data_dir = path
        self.runtime_dir = path / "runtime"


class FakeCtx:
    def __init__(self, path: Path) -> None:
        self.paths = FakePaths(path)
        self.logger = logging.getLogger("check")


INTERNAL_URLS = [
    ("http://127.0.0.1:7890/", "本机代理端口"),
    ("http://192.168.1.104:5099/", "内网 WebUI"),
    ("http://169.254.169.254/latest/meta-data/", "云 metadata"),
    ("http://[::1]:8080/", "IPv6 回环"),
]
PUBLIC_STEAM = "https://store.steampowered.com/feeds/news/app/1973530/?l=schinese"


async def main() -> int:
    plugin = mod.create_plugin()
    if CONFIG_PATH.exists():
        raw = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        print(f"=== 使用 {CONFIG_PATH.name} 里的实际配置 ===")
    else:
        raw = plugin.get_default_config()
        print("=== 没有 config.toml，使用插件默认配置 ===")
    plugin.set_plugin_config(raw)
    plugin._set_context(FakeCtx(Path("/tmp/tw-test/ssrfdata")))
    plugin._client = plugin._build_client()

    print("=== 当前配置 ===")
    print("  link.enabled =", plugin.config.link.enabled)
    print("  link.allow_private_hosts =", plugin.config.link.allow_private_hosts)
    print("  push.skip_sensitive =", plugin.config.push.skip_sensitive)
    print("  command.cross_chat_admin_only =", plugin.config.command.cross_chat_admin_only)
    print("  command.admins =", plugin.config.command.admins)

    check(plugin.config.link.allow_private_hosts is False, "SSRF 防护是开着的")

    print("\n=== 1) 内网链接应被拦下 ===")
    for url, label in INTERNAL_URLS:
        allowed, reason = await mod.url_is_public(url)
        check(not allowed, f"{label} 被拦截（{reason}）")
        title, body = await plugin._fetch_link_preview(url)
        check(not body, f"{label} 抓不到任何正文")

    print("\n=== 2) 公网链接照常抓取 ===")
    allowed, reason = await mod.url_is_public(PUBLIC_STEAM)
    check(allowed, f"Steam RSS 放行（{reason}）")
    title, body = await plugin._fetch_link_preview(PUBLIC_STEAM)
    check(bool(body), f"Steam 内容抓取成功（{len(body)} 字）")
    print("  标题：", title[:60])
    print("  正文开头：", body[:80].replace("\n", " "))

    print("\n=== 3) 带内网链接的推文仍能正常投递 ===")
    tweet = mod.Tweet(
        id="999002",
        url="https://x.com/a/status/999002",
        text="看这个 http://127.0.0.1:7890/ 还有 https://example.com/",
        created_ts=mod.time.time(),
        author_name="tester",
        author_screen_name="tester",
    )
    await plugin._attach_link_previews([tweet])
    check(not tweet.link_preview, "内网链接没有生成链接内容块")
    rendered = plugin._render_tweet(tweet, [])
    check("999002" in rendered, "推文本身照常渲染")
    print("  渲染结果：")
    for line in rendered.splitlines():
        print("   ", line)

    await plugin._client.close()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print("  -", item)
        return 1
    print("SSRF 防护真机验证通过 ✅")
    return 0


sys.exit(asyncio.run(main()))
