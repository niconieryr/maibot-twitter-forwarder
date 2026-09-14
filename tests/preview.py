"""真实网络预览：看一条推文最终会被渲染成什么样。

    ~/maimai/MaiBot/.venv/bin/python preview.py elonmusk
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("tw_preview", BASE / "plugin.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

HANDLE = sys.argv[1] if len(sys.argv) > 1 else "elonmusk"


class FakeSend:
    def __init__(self):
        self.calls = []

    async def text(self, text, stream_id, **kw):
        self.calls.append(("text", stream_id, text))
        return True

    async def image(self, data, stream_id, **kw):
        self.calls.append(("image", stream_id, f"{len(data) // 1024} KB base64"))
        return True

    async def forward(self, messages, stream_id, **kw):
        for node in messages:
            print(f"\n--- 合并转发节点 nickname={node.get('nickname')!r} ---")
            for seg in node.get("segments") or []:
                if seg["type"] == "text":
                    print(seg["content"])
                else:
                    print(f"[图片 {len(seg['content']) * 3 // 4 // 1024} KB]")
        self.calls.append(("forward", stream_id, len(messages)))
        return True


class FakeChat:
    async def get_all_streams(self, platform="qq"):
        return []


class FakePaths:
    def __init__(self, d):
        self.data_dir = d
        self.runtime_dir = d / "r"


class FakeCtx:
    def __init__(self, d):
        self.send = FakeSend()
        self.chat = FakeChat()
        self.paths = FakePaths(d)
        self.logger = logging.getLogger("preview")


async def main():
    # 预览用的临时目录就放在 tests/ 下面，跑完可以随手删
    data = Path(__file__).resolve().parent / "_preview-data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "state.json").unlink(missing_ok=True)

    plugin = mod.create_plugin()
    cfg = plugin.get_default_config()
    cfg["poll"]["initial_delay_seconds"] = 99999
    plugin.set_plugin_config(cfg)
    ctx = FakeCtx(data)
    plugin._set_context(ctx)
    await plugin.on_load()

    print(f"=== 拉取 @{HANDLE} 的推文 ===")
    tweets = await plugin._client.fetch_timeline(HANDLE, 3)
    print(f"拿到 {len(tweets)} 条，数据源={tweets[0].source if tweets else '-'}\n")
    for tweet in tweets:
        print("=" * 70)
        images = await plugin._collect_images(tweet)
        print("配图下载：", [f"{len(item) // 1024} KB" for item in images] or "无")
        print("-" * 70)
        print(plugin._render_tweet(tweet, images))

    print("\n" + "=" * 70)
    print("真实投递预览（走 _deliver）：")
    await plugin._deliver(tweets[0], "preview-stream")

    await plugin._client.close()
    await plugin.on_unload()


asyncio.run(main())
