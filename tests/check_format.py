"""真机验证投递格式：轮询路径走普通图文、/tw_test 路径走合并转发（用服务器上真实的 config.toml）。

    ~/maimai/MaiBot/.venv/bin/python check_format.py [handle]

只把消息发给一个假的聊天流（不会真的发到群里），验证的是"用哪套发送 API"。
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

spec = importlib.util.spec_from_file_location("tw_format_check", PLUGIN_DIR / "plugin.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

HANDLE = sys.argv[1] if len(sys.argv) > 1 else "limbuscompany_b"
FAILURES: list[str] = []


def check(ok: bool, message: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'} {message}")
    if not ok:
        FAILURES.append(message)


class FakeSend:
    """只记录用了哪些发送接口，不真的发消息。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def text(self, text, stream_id, **kwargs):
        self.calls.append(("text", len(str(text))))
        return True

    async def image(self, data, stream_id, **kwargs):
        self.calls.append(("image", len(str(data))))
        return True

    async def forward(self, messages, stream_id, **kwargs):
        self.calls.append(("forward", len(messages)))
        return True

    async def hybrid(self, segments, stream_id, **kwargs):
        kinds = [str(item.get("type")) for item in segments]
        self.calls.append(("hybrid", tuple(kinds)))
        return True

    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeChat:
    async def get_all_streams(self, platform: str = "qq"):
        return []


class FakePaths:
    def __init__(self, path: Path) -> None:
        self.data_dir = path
        self.runtime_dir = path / "runtime"


class FakeCtx:
    def __init__(self, path: Path) -> None:
        self.send = FakeSend()
        self.chat = FakeChat()
        self.paths = FakePaths(path)
        self.logger = logging.getLogger("check-format")


async def main() -> int:
    data_dir = Path("/tmp/tw-test/formatdata")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "state.json").unlink(missing_ok=True)

    plugin = mod.create_plugin()
    if CONFIG_PATH.exists():
        raw = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        print(f"=== 使用 {CONFIG_PATH.name} 里的实际配置 ===")
    else:
        raw = plugin.get_default_config()
        print("=== 没有 config.toml，使用插件默认配置 ===")
    raw.setdefault("poll", {})["initial_delay_seconds"] = 99999
    plugin.set_plugin_config(raw)

    ctx = FakeCtx(data_dir)
    plugin._set_context(ctx)
    await plugin.on_load()

    print("=== 格式相关配置 ===")
    print("  display.use_forward      =", plugin.config.display.use_forward)
    print("  display.forward_for_poll =", plugin.config.display.forward_for_poll)
    print("  display.forward_for_test =", plugin.config.display.forward_for_test)
    print("  display.batch_forward    =", plugin.config.display.batch_forward)
    print("  display.video_in_forward =", plugin.config.display.video_in_forward)

    client = plugin._client
    tweets = await client.fetch_timeline(HANDLE, 5)
    if len(tweets) < 2:
        print(f"@HANDLE 推文不够，换一个 handle 再试")
        await client.close()
        return 1
    sample = tweets[:3]
    print(f"\n取 @{HANDLE} 最近 {len(sample)} 条推文做样本：{[item.id for item in sample]}")

    print("\n=== 1) 轮询路径（use_forward 不指定 → 跟随 forward_for_poll）===")
    ctx.send.calls.clear()
    delivered = await plugin._deliver_many(list(sample), "fake-stream")
    kinds = ctx.send.kinds()
    print("  发送调用：", kinds)
    check(len(delivered) == len(sample), f"{len(delivered)} 条全部投递成功")
    check("forward" not in kinds, "没有使用合并转发（普通图文）")
    check(kinds.count("text") >= len(sample), f"每条推文各发了一条普通消息（text×{kinds.count('text')}）")

    print("\n=== 2) /tw_test 路径（use_forward=forward_for_test）===")
    ctx.send.calls.clear()
    count = await plugin._push_latest(
        HANDLE, "fake-stream", count=2, use_forward=bool(plugin.config.display.forward_for_test)
    )
    kinds = ctx.send.kinds()
    print("  发送调用：", kinds)
    check(count == 2, f"预览推了 {count} 条")
    check("forward" in kinds, "使用了合并转发（聊天记录）")
    check(kinds.count("forward") == 1, "两条推文打包成一条聊天记录")

    await client.close()
    await plugin.on_unload()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print("  -", item)
        return 1
    print("投递格式真机验证通过 ✅")
    return 0


sys.exit(asyncio.run(main()))
