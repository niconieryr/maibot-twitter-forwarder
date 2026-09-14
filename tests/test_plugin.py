"""推特转发插件的离线 + 在线自测脚本。

用 MaiBot 自带 venv 运行（需要 maibot_sdk 与 aiohttp）：

    ~/maimai/MaiBot/.venv/bin/python test_plugin.py

覆盖：
1. 纯函数（handle 归一化、计数/时间格式化、Atom 解析、HTML 转文本）
2. 命令正则与组件注册
3. 真实网络下的订阅、轮询、去重、基线、排序、过滤
4. 投递路径（合并转发 / 回退图文）与命令回复
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import shutil
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
STATE_PATH = DATA_DIR / "state.json"

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, message: str) -> None:
    """记录一条断言结果。"""

    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  \033[32mPASS\033[0m {message}")
    else:
        print(f"  \033[31mFAIL\033[0m {message}")
        FAILURES.append(message)


def load_plugin_module():
    """按路径加载 plugin.py。"""

    spec = importlib.util.spec_from_file_location("tw_plugin_under_test", PLUGIN_DIR / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeSend:
    """记录发送调用，替代真实 QQ 发送。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def reset(self) -> None:
        self.calls = []

    async def text(self, text: str, stream_id: str, **kwargs):
        self.calls.append(("text", stream_id, text))
        return True

    async def image(self, image_data: str, stream_id: str, **kwargs):
        self.calls.append(("image", stream_id, len(image_data)))
        return True

    async def forward(self, messages, stream_id: str, **kwargs):
        nodes = []
        for node in messages:
            segments = node.get("segments") or []
            nodes.append(
                {
                    "nickname": str(node.get("nickname") or ""),
                    "texts": [str(s.get("content") or "") for s in segments if s.get("type") == "text"],
                    "images": [len(str(s.get("content") or "")) for s in segments if s.get("type") == "image"],
                    "videos": [
                        {
                            "inline": len(str((s.get("data") or {}).get("binary_data_base64") or "")),
                            "file": str((s.get("data") or {}).get("file") or ""),
                        }
                        for s in segments
                        if s.get("type") == "video"
                    ],
                }
            )
        self.calls.append(("forward", stream_id, nodes))
        return True

    def forwards(self) -> list[tuple]:
        return [call for call in self.calls if call[0] == "forward"]

    @staticmethod
    def node_texts(call) -> list[str]:
        return [(node["texts"][0] if node["texts"] else "") for node in call[2]]

    @staticmethod
    def node_images(call) -> list[int]:
        return [len(node["images"]) for node in call[2]]

    @staticmethod
    def node_videos(call) -> list[list[dict]]:
        return [node["videos"] for node in call[2]]

    async def hybrid(self, segments, stream_id: str, **kwargs):
        summary = []
        for segment in segments:
            kind = str(segment.get("type") or "")
            if kind == "video":
                data = segment.get("data") or {}
                base64_len = len(str(data.get("binary_data_base64") or ""))
                summary.append(("video", base64_len, str(data.get("file") or "")))
            else:
                content = str(segment.get("content") or "")
                summary.append((kind, len(content), content[:200]))
        self.calls.append(("hybrid", stream_id, summary))
        return True

    def hybrids(self) -> list[tuple]:
        return [call for call in self.calls if call[0] == "hybrid"]

    def last_text(self) -> str:
        for call in reversed(self.calls):
            if call[0] == "text":
                return call[2]
        return ""

    def summary(self) -> str:
        return json.dumps(self.calls, ensure_ascii=False)[:400]


class FakeLLM:
    """LLM 能力替身：默认把输入原样返回（等于"没翻译"），可切换成返回固定译文。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.echo = True
        self.response = "【译文】"
        self.success = True

    def reset(self) -> None:
        self.calls = []

    async def generate(self, prompt, model: str = "", **kwargs):
        self.calls.append({"prompt": prompt, "model": model, "kwargs": kwargs})
        if not self.success:
            return {"success": False, "response": "", "error": "模拟翻译失败"}
        if self.echo:
            user_text = ""
            if isinstance(prompt, list):
                for message in prompt:
                    if isinstance(message, dict) and message.get("role") == "user":
                        user_text = str(message.get("content") or "")
            else:
                user_text = str(prompt)
            return {"success": True, "response": user_text, "model": model}
        return {"success": True, "response": self.response, "model": model}


class FakeChat:
    """聊天流能力替身。"""

    async def get_all_streams(self, platform: str = "qq"):
        return [
            {"session_id": "stream-a", "group_name": "测试群A", "is_group_session": True},
            {"session_id": "stream-b", "group_name": "测试群B", "is_group_session": True},
        ]


class FakePaths:
    """PluginPaths 替身。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.runtime_dir = data_dir / "runtime"


class FakeContext:
    """PluginContext 替身。"""

    def __init__(self, data_dir: Path) -> None:
        self.send = FakeSend()
        self.chat = FakeChat()
        self.llm = FakeLLM()
        self.paths = FakePaths(data_dir)
        self.logger = logging.getLogger("tw_test")


class StubClient:
    """可控的 FxTwitter 客户端替身。"""

    def __init__(
        self,
        tweets,
        image_bytes: bytes = b"\xff\xd8\xff\xe0" + b"x" * 512,
        head_sizes: dict | None = None,
        video_bytes: bytes | None = None,
        pages: dict | None = None,
    ) -> None:
        # 保持引用：测试里往同一个 list 追加推文时替身要能看到
        self.tweets = tweets
        self.image_bytes = image_bytes
        self.head_sizes = dict(head_sizes or {})
        self.video_bytes = video_bytes if video_bytes is not None else b"V" * 4096
        self.download_count = 0
        self.video_downloads: list[str] = []
        self.file_downloads: list[tuple[str, str]] = []
        self.pages: dict[str, str] = dict(pages or {})
        self.proxy_pages: dict[str, str] = {}
        self.fetched_pages: list[tuple[str, str | None]] = []

    async def fetch_text(
        self,
        url: str,
        *,
        use_proxy: bool = False,
        proxy: str | None = None,
        timeout: float = 25.0,
        max_bytes: int = 0,
        validate=None,
        max_redirects: int = 5,
    ):
        # 替身不模拟真实网络，但仍然尊重 SSRF 校验：校验不通过就当抓不到
        if validate is not None:
            allowed, _reason = await validate(url)
            if not allowed:
                self.fetched_pages.append((url, proxy))
                return ""
        self.fetched_pages.append((url, proxy))
        if proxy:
            return self.proxy_pages.get(url, "")
        return self.pages.get(url, "")

    async def fetch_timeline(self, handle: str, count: int):
        return list(self.tweets)

    async def head_size(self, url: str, **kwargs):
        return self.head_sizes.get(url)

    async def download_bytes(self, url: str, *, max_bytes: int, timeout: float, expect: tuple = ()):
        self.download_count += 1
        if url in self.head_sizes:
            self.video_downloads.append(url)
            if len(self.video_bytes) > max_bytes:
                return None
            return self.video_bytes
        return self.image_bytes if len(self.image_bytes) <= max_bytes else None

    async def download_to_file(self, url: str, dest, *, max_bytes: int, timeout: float):
        self.file_downloads.append((url, str(dest)))
        data = self.video_bytes or b"V" * 1024
        if len(data) > max_bytes:
            return False
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return True

    async def download_image(self, url: str, *, max_bytes: int, timeout: float):
        self.download_count += 1
        return self.image_bytes if len(self.image_bytes) <= max_bytes else None

    async def close(self) -> None:
        return None


def make_tweet(module, tweet_id: str, ts: float, **overrides):
    """构造一条测试推文。"""

    payload = {
        "id": tweet_id,
        "url": f"https://x.com/elonmusk/status/{tweet_id}",
        "text": overrides.pop("text", f"推文 {tweet_id}"),
        "created_ts": ts,
        "author_name": overrides.pop("author_name", "Elon Musk"),
        "author_screen_name": overrides.pop("author_screen_name", "elonmusk"),
        "media": overrides.pop(
            "media",
            [module.TweetMedia(kind="photo", url=f"https://pbs.twimg.com/media/{tweet_id}.jpg?name=orig")],
        ),
    }
    payload.update(overrides)
    return module.Tweet(**payload)


def test_pure_functions(module) -> None:
    """纯函数与解析测试。"""

    print("\n[1] 纯函数 / 解析")
    normalize = module.normalize_handle
    check(normalize("@ElonMusk") == "elonmusk", "解析 @名字")
    check(normalize("elonmusk") == "elonmusk", "解析裸名字")
    check(normalize("https://x.com/elonmusk") == "elonmusk", "解析 x.com 主页链接")
    check(normalize("https://x.com/elonmusk/status/123456") == "elonmusk", "解析推文链接")
    check(normalize("https://twitter.com/elonmusk/") == "elonmusk", "解析 twitter.com 带斜杠链接")
    check(normalize("x.com/elonmusk") == "elonmusk", "解析无协议链接")
    check(normalize("https://twitter.com/i/web/status/123") is None, "拒绝保留路径")
    check(normalize("马斯克") is None, "拒绝中文名")
    check(normalize("a" * 16) is None, "拒绝超长 handle")
    check(normalize("") is None, "拒绝空串")
    check(module.parse_handle_list("@a b, c、d") == ["a", "b", "c", "d"], "多 handle 解析")

    # 输入报错要能说清原因（历史 bug：超长名字只回一句"用法"）
    long_name = "limbus_company_b"
    check(len(long_name) == 16, "样例用户名确实是 16 字符")
    check(module.normalize_handle(long_name) is None, "超过 15 字符的用户名被拒绝")
    reason = module.describe_handle_problem(long_name)
    check("16 个字符" in reason and "15" in reason, f"超长名字给出字符数原因：{reason}")
    check("15 字符上限" in module.describe_handle_problem("a" * 20), "更长的名字同样给出上限原因")
    check("字符" in module.describe_handle_problem("abc- def"), "非法字符给出原因")
    check("链接" in module.describe_handle_problem("https://example.com/abc"), "非 X 链接给出原因")
    check("数字" in module.describe_handle_problem("123456789"), "纯数字给出原因")

    handles, problems = module.parse_handle_input("elonmusk limbus_company_b")
    check(handles == ["elonmusk"] and len(problems) == 1, "混合输入只报有效项 + 说明跳过原因")

    candidates = module.handle_repair_candidates(long_name)
    check("limbuscompany_b" in candidates, f"修复候选覆盖「删掉一个下划线」：{candidates}")
    check("limbuscompanyb" in candidates, f"修复候选覆盖「删掉全部下划线」：{candidates}")
    check(all(len(item) <= 15 for item in candidates), "候选都满足长度限制")

    check(module.format_count(999) == "999", "计数 <1万")
    check(module.format_count(12345) == "1.2万", "计数万")
    check(module.format_count(120000000) == "1.2亿", "计数亿")
    check(module.parse_tweet_id("https://x.com/a/status/12345") == "12345", "提取推文 ID")

    tweet = module.tweet_from_api(
        {
            "id": "999",
            "url": "https://x.com/a/status/999",
            "text": "hello",
            "created_timestamp": 1700000000,
            "author": {"name": "A", "screen_name": "a"},
            "reposted_by": {"name": "B", "screen_name": "b"},
            "likes": 10,
            "reposts": 2,
            "media": {"all": [{"type": "photo", "url": "https://pbs.twimg.com/media/x.jpg?name=orig"}]},
        }
    )
    check(tweet is not None and tweet.is_repost and tweet.reposted_by_screen_name == "b", "v2 转推解析")
    check(tweet is not None and tweet.image_sources() == ["https://pbs.twimg.com/media/x.jpg?name=orig"], "图片源提取")
    check(
        module.resize_twimg_url("https://pbs.twimg.com/media/x.jpg?name=orig", "medium")
        == "https://pbs.twimg.com/media/x.jpg?name=medium",
        "图片尺寸改写",
    )
    check(module.parse_twitter_time("Fri Sep 11 17:53:28 +0000 2026") > 0, "Twitter 时间格式解析")

    atom_entry = module.ET.fromstring(
        """<entry xmlns="http://www.w3.org/2005/Atom">
        <title>t</title>
        <link href="https://x.com/other/status/777" rel="alternate"/>
        <link rel="enclosure" href="https://pbs.twimg.com/media/y.jpg" type="image/jpeg"/>
        <id>https://x.com/other/status/777</id>
        <published>2026-09-10T15:43:24.000Z</published>
        <content type="html"><![CDATA[<p>line1<br/>line2</p>]]></content>
        </entry>"""
    )
    feed_tweet = module.tweet_from_atom(atom_entry, "elonmusk")
    check(feed_tweet is not None and feed_tweet.id == "777", "Atom 条目转推文")
    check(feed_tweet is not None and feed_tweet.is_repost, "Atom 转推识别")
    check(feed_tweet is not None and "line1" in feed_tweet.text and "line2" in feed_tweet.text, "Atom HTML 转文本")

    video_tweet = module.tweet_from_api(
        {
            "id": "888",
            "url": "https://x.com/a/status/888",
            "text": "带视频的推文",
            "created_timestamp": 1700000000,
            "author": {"name": "A", "screen_name": "a"},
            "media": {
                "all": [
                    {
                        "type": "video",
                        "url": "https://video.twimg.com/4k.mp4",
                        "thumbnail_url": "https://pbs.twimg.com/thumb.jpg",
                        "duration": 26.3,
                        "formats": [
                            {"url": "https://video.twimg.com/720.mp4", "bitrate": 2176000, "container": "mp4"},
                            {"url": "https://video.twimg.com/360.mp4", "bitrate": 832000, "container": "mp4"},
                            {"url": "https://video.twimg.com/hls.m3u8", "bitrate": None, "container": "m3u8"},
                        ],
                    }
                ]
            },
        }
    )
    check(video_tweet is not None and video_tweet.has_video, "视频推文识别")
    check(len(video_tweet.video_media()) == 1, "视频媒体提取")
    video_media = video_tweet.video_media()[0]
    check(len(video_media.formats) == 2, f"只保留 mp4 变体（实际 {len(video_media.formats)}）")
    check(abs(video_media.duration - 26.3) < 0.01, "视频时长解析")
    check(video_media.best_variant_candidates()[0][0] == "https://video.twimg.com/4k.mp4", "主地址排在最前")
    check(video_media.best_variant_candidates()[1][1] == 2176000, "其余变体按码率降序")
    check(
        video_tweet.image_sources() == ["https://pbs.twimg.com/thumb.jpg"],
        "视频推文的封面仍可作回退图",
    )


def test_url_safety(module) -> None:
    """外链安全（SSRF 防护）与默认安全值。"""

    print("\n[1b] 外链安全 / 默认安全值")
    blocked = [
        "localhost",
        "127.0.0.1",
        "127.1.2.3",
        "0.0.0.0",
        "10.0.0.5",
        "172.16.9.9",
        "192.168.1.104",
        "169.254.169.254",  # 云 metadata
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "foo.local",
        "router.localdomain",
        "metadata.google.internal",
        "redis.internal",
        "",
    ]
    for host in blocked:
        check(module.host_is_blocked(host), f"内网/本机主机名被拦截：{host or '(空)'}")

    for host in ["example.com", "pbs.twimg.com", "8.8.8.8", "2606:4700::1111"]:
        check(not module.host_is_blocked(host), f"公网主机名放行：{host}")

    check(module.ip_is_blocked(module.ipaddress.ip_address("100.64.0.1")), "运营商级 NAT 段也拦（100.64/10）")
    check(module.ip_is_blocked(module.ipaddress.ip_address("169.254.169.254")), "metadata 地址判定为危险")
    check(not module.ip_is_blocked(module.ipaddress.ip_address("1.1.1.1")), "公网 IP 判定为安全")

    # 默认安全值
    plugin = module.create_plugin()
    defaults = plugin.get_default_config()
    check(defaults["command"]["cross_chat_admin_only"] is True, "跨聊天命令默认仅管理员可用")
    check(defaults["command"]["admin_only"] is False, "聊天内命令默认不限管理员")
    check(defaults["push"]["skip_sensitive"] is True, "默认跳过敏感推文")
    check(defaults["link"]["allow_private_hosts"] is False, "默认禁止抓取内网链接")


def test_components(module) -> None:
    """组件注册与命令正则测试。"""

    print("\n[2] 组件注册与命令正则")
    plugin = module.create_plugin()
    components = plugin.get_components()
    by_type: dict[str, list[dict]] = {}
    for component in components:
        by_type.setdefault(component["type"], []).append(component)

    command_count = len(by_type.get("COMMAND", []))
    check(command_count == 13, f"注册了 13 个命令（实际 {command_count}）")
    check(len(by_type.get("TOOL", [])) == 1, "注册了 1 个工具")
    check(len(by_type.get("HOME_CARD", [])) == 1, "注册了 1 个首页卡片")

    samples = {
        "tw_sub": ["/tw_sub elonmusk", "／tw_sub @a b", "/tw_sub https://x.com/a/status/1", "@bot /tw_sub a", "/tw_sub"],
        "tw_unsub": ["/tw_unsub a", "/tw_unsub"],
        "tw_list": ["/tw_list"],
        "tw_all": ["/tw_all"],
        "tw_on": ["/tw_on"],
        "tw_off": ["/tw_off"],
        "tw_check": ["/tw_check", "/tw_check elonmusk"],
        "tw_test": ["/tw_test elonmusk", "/tw_test elonmusk 2"],
        "tw_interval": ["/tw_interval", "/tw_interval 15", "/tw_interval reset"],
        "tw_reset": ["/tw_reset a"],
        "tw_del": ["/tw_del a"],
        "tw_status": ["/tw_status"],
        "tw_help": ["/tw", "/tw_help"],
    }
    patterns = {
        component["name"]: component["metadata"].get("command_pattern", "")
        for component in by_type.get("COMMAND", [])
    }
    for name, pattern in patterns.items():
        check(bool(pattern), f"{name} 声明了正则")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:  # pragma: no cover
            check(False, f"{name} 正则可编译（{exc}）")
            continue
        for sample in samples.get(name, []):
            matched = compiled.search(sample)
            expected = True
            if name == "tw_sub" and sample == "/tw_sub":
                expected = True  # 允许空参数，由处理器给出用法提示
            check(bool(matched) == expected, f"{name} 匹配 {sample!r}")
        if name not in {"tw_help"}:
            check(compiled.search("/tw_help") is None, f"{name} 不会误匹配 /tw_help")
        check(compiled.search("/tw_subx a") is None, f"{name} 不会误匹配前缀命令 /tw_subx")

    # 反向：/tw_help 也不该被别的命令抢走
    help_pattern = re.compile(patterns["tw_help"])
    check(bool(help_pattern.search("/tw_status")) is False, "tw_help 不误吃其它命令")


async def test_subscribe(module, plugin, context) -> None:
    """真实网络下的订阅流程。"""

    print("\n[3] 真实网络订阅（FxTwitter）")
    if STATE_PATH.exists():
        STATE_PATH.unlink()

    await plugin.on_load()
    check(plugin._client is not None, "网络客户端已创建")

    result = await plugin.handle_sub(
        stream_id="stream-a",
        matched_groups={"handles": "elonmusk"},
        user_id="10001",
        is_local_operator=False,
    )
    reply = context.send.last_text()
    print("  订阅回复:", reply.replace("\n", " | ")[:200])
    check(result[0] is True, "订阅命令返回成功")
    check("elonmusk" in reply, "订阅回复包含 handle")
    check(STATE_PATH.exists(), "状态文件已写入")

    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    check("elonmusk" in state.get("handles", {}), "状态中记录了订阅")
    check(
        state["handles"]["elonmusk"]["streams"] == ["stream-a"],
        "状态中记录了推送目标",
    )
    check(len(state["handles"]["elonmusk"]["seen_ids"]) > 0, "首次订阅建立了基线")

    bad = await plugin.handle_sub(
        stream_id="stream-a",
        matched_groups={"handles": "qzxwvutmsp1234"},
        user_id="10001",
        is_local_operator=False,
    )
    check("失败" in context.send.last_text() or bad[0] is False, "不存在的账号会报错")

    print("\n[3b] 用户名写错时的提示")
    context.send.reset()
    rejected = await plugin.handle_sub(
        stream_id="stream-a",
        matched_groups={"handles": "limbus_company_b"},
        user_id="10001",
        is_local_operator=False,
    )
    reply = context.send.last_text()
    print("  报错回复:", reply.replace("\n", " | ")[:220])
    check(rejected[0] is False, "超长用户名被拒绝")
    check("16 个字符" in reply, "回复里说明了是长度问题")
    check("用法" not in reply.split("\n")[0], "不再只回一句干巴巴的用法")
    check("LimbusCompany_B" in reply, "回复里猜出了正确的用户名")

    context.send.reset()
    await plugin.handle_sub(stream_id="stream-a", matched_groups={"handles": ""}, user_id="10001")
    check("没有写用户名" in context.send.last_text(), "空参数时提示「没写用户名」")

    context.send.reset()
    await plugin.handle_sub(
        stream_id="stream-a",
        matched_groups={"handles": "elonmusk limbus_company_b"},
        user_id="10001",
        is_local_operator=False,
    )
    mixed = context.send.last_text()
    check("已跳过" in mixed, "混合输入里无效项被明确跳过")
    check("已经在订阅列表里了" in mixed, "混合输入里的有效项照常处理")


async def test_polling(module, plugin, context) -> None:
    """轮询、去重、排序、过滤逻辑（用替身客户端）。"""

    print("\n[4] 轮询与去重逻辑")
    if plugin._client is not None:
        await plugin._client.close()
    now = time.time()
    handle = "elonmusk"
    entry = plugin._require_state().handle_entry(handle)
    entry["seen_ids"] = ["100"]
    entry["last_seen_ts"] = now - 3600
    entry["streams"] = ["stream-a"]

    tweets = [
        make_tweet(module, "300", now - 10, text="最新"),
        make_tweet(module, "200", now - 100, text="中间"),
        make_tweet(module, "100", now - 200, text="旧的已见过"),
    ]
    context.send.reset()
    plugin._client = StubClient(tweets)
    result = await plugin._poll_handle(handle, 20)
    check(result.pushed == 2, f"推送了 2 条新推文（实际 {result.pushed}）")
    check(not result.error, "轮询无错误")
    forwards = context.send.forwards()
    check(len(forwards) == 1, f"两条新推文打包成一条聊天记录（实际 {len(forwards)} 条）")
    if forwards:
        check(len(forwards[0][2]) == 2, "聊天记录里有 2 个节点")
        texts = context.send.node_texts(forwards[0])
        check("最新" in texts[0], f"最新的推文排在最上面：{[t[:6] for t in texts]}")
        check("中间" in texts[1], "旧的排在下面")
        check(all(count > 0 for count in context.send.node_images(forwards[0])), "每个节点都带上了自己的配图")

    # 再轮询一次：不应重复推送
    context.send.reset()
    again = await plugin._poll_handle(handle, 20)
    check(again.pushed == 0, "同一批推文不会重复推送")

    # 新增一条
    tweets.append(make_tweet(module, "400", now, text="更新的"))
    context.send.reset()
    third = await plugin._poll_handle(handle, 20)
    check(third.pushed == 1, "新增推文被发现")
    check("更新的" in context.send.last_text() or "更新的" in "".join(context.send.node_texts(context.send.forwards()[0])), "推送内容正确")

    # 转推过滤
    plugin.config.push.include_reposts = False
    tweets.append(
        make_tweet(
            module,
            "500",
            now + 5,
            text="转推内容",
            author_screen_name="someone",
            is_repost=True,
            reposted_by_name="Elon Musk",
            reposted_by_screen_name="elonmusk",
        )
    )
    context.send.reset()
    filtered = await plugin._poll_handle(handle, 20)
    check(filtered.pushed == 0, "关闭转推后不推送转推")
    plugin.config.push.include_reposts = True

    # 回复过滤（默认关闭回复）
    tweets.append(make_tweet(module, "600", now + 10, text="回复内容", is_reply=True))
    context.send.reset()
    reply_filtered = await plugin._poll_handle(handle, 20)
    check(reply_filtered.pushed == 0, "默认不推送回复")

    # 每轮上限
    plugin.config.poll.max_tweets_per_poll = 2
    for index in range(5):
        tweets.append(make_tweet(module, str(700 + index), now + 20 + index, text=f"批量{index}"))
    context.send.reset()
    limited = await plugin._poll_handle(handle, 20)
    check(limited.pushed == 2, f"每轮上限生效（实际 {limited.pushed}）")
    context.send.reset()
    rest = await plugin._poll_handle(handle, 20)
    check(rest.pushed == 2, "剩余推文在下一轮继续推送")
    context.send.reset()
    rest2 = await plugin._poll_handle(handle, 20)
    check(rest2.pushed == 1, "最后一条也补上了")
    plugin.config.poll.max_tweets_per_poll = 3

    # 暂停推送
    plugin._require_state().stream_entry("stream-a")["paused"] = True
    tweets.append(make_tweet(module, "900", now + 100, text="暂停期间"))
    context.send.reset()
    paused = await plugin._poll_handle(handle, 20)
    check(paused.pushed == 0, "暂停后不推送")
    plugin._require_state().stream_entry("stream-a")["paused"] = False

    # 失败处理
    class BrokenClient(StubClient):
        async def fetch_timeline(self, handle: str, count: int):
            raise module.FxTwitterError("模拟失败")

    plugin._client = StubClient(tweets)
    plugin._client = BrokenClient([])
    broken = await plugin._poll_handle(handle, 20)
    check(broken.error != "" and not broken.ok, "拉取失败被记录为错误")
    check(int(plugin._require_state().handles[handle]["fail_count"]) > 0, "失败次数已累计")

    plugin._client = StubClient(tweets)
    recovered = await plugin._poll_handle(handle, 20)
    check(recovered.ok, "恢复后错误被清除")
    check(plugin._require_state().handles[handle]["fail_count"] == 0, "失败计数归零")


async def test_delivery_paths(module, plugin, context) -> None:
    """投递路径：合并转发失败时回退图文。"""

    print("\n[5] 投递与回退")
    tweet = make_tweet(module, "1001", time.time(), text="回退测试")

    class FailingForwardSend(FakeSend):
        async def forward(self, messages, stream_id: str, **kwargs):
            self.calls.append(("forward-failed", stream_id))
            return False

    failing = FailingForwardSend()
    original_send = context.send
    context.send = failing
    delivered = await plugin._deliver(tweet, "stream-a", images=[b"\xff\xd8" + b"y" * 256])
    check(delivered, "回退后仍然算投递成功")
    kinds = [call[0] for call in failing.calls]
    check("text" in kinds and "image" in kinds, "回退为文字 + 图片发送")
    context.send = original_send

    # 媒体下载失败时只发文字（现在单条也走聊天记录，所以检查节点里没有图片段）
    plugin._client = StubClient([tweet], image_bytes=b"z" * (10 * 1024 * 1024))
    plugin.config.media.max_image_mb = 1.0
    context.send.reset()
    ok = await plugin._deliver(tweet, "stream-a")
    check(ok, "无图时仍能投递")
    forwards = context.send.forwards()
    check(bool(forwards), "无图时仍然发出聊天记录")
    if forwards:
        check(context.send.node_images(forwards[0]) == [0], "超限图片被跳过，节点里只有文字")
        check("回退测试" in context.send.node_texts(forwards[0])[0], "文字内容照常发出")
    plugin.config.media.max_image_mb = 3.0

    # 图片总大小上限
    multi = make_tweet(
        module,
        "1002",
        time.time(),
        media=[module.TweetMedia(kind="photo", url=f"https://pbs.twimg.com/media/m{index}.jpg") for index in range(4)],
    )
    plugin._client = StubClient([multi], image_bytes=b"m" * (400 * 1024))
    plugin.config.media.max_images = 4
    plugin.config.media.max_total_mb = 1.0
    images = await plugin._collect_images(multi)
    check(len(images) == 2, f"图片总大小上限生效（实际 {len(images)} 张）")
    plugin.config.media.max_total_mb = 6.0


async def test_video_delivery(module, plugin, context) -> None:
    """视频下载与投递：内联 base64 / docker 借道容器 / 回退封面。"""

    print("\n[5b] 视频投递")
    # 这一段专门测"视频单独成条"的路径（视频进聊天记录的路径在 [5c] 里测）
    plugin.config.display.video_in_forward = False
    now = time.time()
    small_url = "https://video.twimg.com/360.mp4"
    big_url = "https://video.twimg.com/1080.mp4"
    huge_url = "https://video.twimg.com/4k.mp4"

    def make_video_tweet(tweet_id: str):
        return make_tweet(
            module,
            tweet_id,
            now,
            text="视频推文",
            media=[
                module.TweetMedia(
                    kind="video",
                    url=huge_url,
                    thumbnail_url="https://pbs.twimg.com/thumb.jpg",
                    duration=30.0,
                    formats=[(big_url, 10368000), (small_url, 832000)],
                )
            ],
        )

    # 1) 小视频：走内联 base64
    tweet = make_video_tweet("2001")
    plugin._client = StubClient(
        [tweet],
        head_sizes={huge_url: 80 * 1024 * 1024, big_url: 9 * 1024 * 1024, small_url: 800 * 1024},
        video_bytes=b"V" * (2 * 1024 * 1024),
    )
    context.send.reset()
    check(await plugin._deliver(tweet, "stream-a"), "小视频投递成功")
    hybrids = context.send.hybrids()
    check(len(hybrids) == 1, f"发了一条混合消息（实际 {len(hybrids)}）")
    if hybrids:
        segments = hybrids[0][2]
        check(segments[0][0] == "text", "混合消息先带文字")
        check("配图" not in segments[0][2], f"视频路径的文案不写「配图 N 张」：{segments[0][2][:40]!r}")
        check(segments[1][0] == "video", "混合消息里带视频段")
        check(segments[1][1] > 0, "视频段用 base64 内联")
        check(not segments[1][2], "内联视频不带容器路径")
    check(
        plugin._client.video_downloads == [big_url],
        f"选了上限内码率最高的变体（实际 {plugin._client.video_downloads}）",
    )

    # 2) 中等视频（都超过内联上限）：docker 拷进容器 → 用容器内路径发送
    docker_calls: list[tuple] = []

    async def fake_exec(container: str, *args: str, timeout: float = 30.0) -> None:
        docker_calls.append(("exec", container, args))

    async def fake_cp(host_path, container: str, container_path: str, timeout: float = 300.0) -> bool:
        docker_calls.append(("cp", str(host_path), container, container_path))
        return True

    original_exec = plugin._docker_exec
    original_cp = plugin._docker_cp
    original_which = shutil.which
    plugin._docker_exec = fake_exec
    plugin._docker_cp = fake_cp
    shutil.which = lambda name, *a, **k: "/usr/bin/docker" if name == "docker" else original_which(name, *a, **k)

    try:
        tweet2 = make_video_tweet("2002")
        plugin._client = StubClient(
            [tweet2],
            head_sizes={huge_url: 80 * 1024 * 1024, big_url: 40 * 1024 * 1024, small_url: 20 * 1024 * 1024},
            video_bytes=b"V" * (1024 * 1024),
        )
        context.send.reset()
        check(await plugin._deliver(tweet2, "stream-a"), "大视频经容器路由投递成功")
        hybrids = context.send.hybrids()
        check(len(hybrids) == 1, f"大视频发了一条混合消息（实际 {len(hybrids)}）")
        if hybrids:
            video_segment = hybrids[0][2][1]
            check(video_segment[0] == "video", "大视频段类型正确")
            check(video_segment[2].startswith("/app/data/twvideo/"), f"用容器内路径发送：{video_segment[2]}")
            check(video_segment[1] == 0, "大视频不占用 base64 内联")
        check(plugin._client.file_downloads and plugin._client.file_downloads[0][0] == huge_url, "下载了 300MB 上限内最大的变体")
        kinds = [call[0] for call in docker_calls]
        check("cp" in kinds, "调用了 docker cp")
        check(kinds.count("exec") >= 1, "调用了 docker exec（在容器里建目录）")
        # 容器内文件默认延迟删除（NapCat 传大视频可能在 send 返回后还在读文件）
        check(plugin.config.media.video_keep_seconds > 0, f"默认延迟清理（{plugin.config.media.video_keep_seconds}s）")
        check(
            not any(call[2][:2] == ("rm", "-f") for call in docker_calls if call[0] == "exec"),
            "发送后没有立刻删除容器内文件",
        )
        check(len(plugin._cleanup_tasks) == 1, f"已排入延迟清理任务（实际 {len(plugin._cleanup_tasks)}）")
        video_dir = Path(context.paths.data_dir) / "videos"
        leftovers = list(video_dir.iterdir()) if video_dir.exists() else []
        check(not leftovers, f"本地临时视频已删除（残留 {leftovers}）")

        # 2b) 延迟清理本身：container_delay=0 立刻删；>0 到点才删
        probe = module.VideoPayload(
            data={"file": "/app/data/twvideo/probe.mp4"},
            size=0,
            url="https://video.twimg.com/probe.mp4",
            container="snowluma",
            container_path="/app/data/twvideo/probe.mp4",
        )
        await plugin._cleanup_video_payload(probe, container_delay=0)
        check(
            any(call[2][:2] == ("rm", "-f") for call in docker_calls if call[0] == "exec"),
            "container_delay=0 时立刻删除容器内文件",
        )
        docker_calls.clear()
        await plugin._cleanup_video_payload(probe, container_delay=0.1)
        check(not docker_calls, "container_delay>0 时先不删")
        await asyncio.sleep(0.5)
        check(
            any(call[2][:2] == ("rm", "-f") for call in docker_calls if call[0] == "exec"),
            "延迟到点后删掉了容器内文件",
        )
        for task in list(plugin._cleanup_tasks):
            task.cancel()
        plugin._cleanup_tasks.clear()

        # 3) 超过 max_video_mb：回退成封面 + 链接
        tweet3 = make_video_tweet("2003")
        plugin._client = StubClient(
            [tweet3],
            head_sizes={huge_url: 900 * 1024 * 1024, big_url: 800 * 1024 * 1024, small_url: 700 * 1024 * 1024},
            video_bytes=b"V" * (1024 * 1024),
        )
        context.send.reset()
        plugin.config.display.use_forward = True
        check(await plugin._deliver(tweet3, "stream-a"), "超大视频回退后仍然投递成功")
        check(not context.send.hybrids(), "超大视频不再尝试发视频")
        forwards = context.send.forwards()
        check(bool(forwards), "超大视频回退为封面合并转发")
        if forwards:
            check("配图" in context.send.node_texts(forwards[0])[0], "回退路径的文案保留配图说明")
    finally:
        plugin._docker_exec = original_exec
        plugin._docker_cp = original_cp
        shutil.which = original_which

    # 4) thumbnail 模式：完全不下载视频
    tweet4 = make_video_tweet("2004")
    plugin.config.media.video_mode = "thumbnail"
    plugin._client = StubClient(
        [tweet4],
        head_sizes={huge_url: 80 * 1024 * 1024, big_url: 9 * 1024 * 1024, small_url: 800 * 1024},
        video_bytes=b"V" * (2 * 1024 * 1024),
    )
    context.send.reset()
    check(await plugin._deliver(tweet4, "stream-a"), "thumbnail 模式投递成功")
    check(not context.send.hybrids(), "thumbnail 模式不发视频")
    check(not plugin._client.file_downloads, "thumbnail 模式不下载视频")
    plugin.config.media.video_mode = "auto"

    # 5) 打开 video_in_forward：视频改走聊天记录节点
    plugin.config.display.video_in_forward = True
    tweet5 = make_video_tweet("2005")
    plugin._client = StubClient(
        [tweet5],
        head_sizes={huge_url: 80 * 1024 * 1024, big_url: 9 * 1024 * 1024, small_url: 800 * 1024},
        video_bytes=b"V" * (2 * 1024 * 1024),
    )
    context.send.reset()
    check(await plugin._deliver(tweet5, "stream-a"), "video_in_forward 投递成功")
    check(not context.send.hybrids(), "video_in_forward 打开后不再单独发视频")
    forwards = context.send.forwards()
    check(len(forwards) == 1 and len(forwards[0][2]) == 2, "视频进了聊天记录（正文 + 视频节点）")
    plugin.config.display.video_in_forward = True  # 还原成默认值，后面的用例依赖它

    # 6) docker 可执行文件查找：PATH 里没有（systemd 常见）时仍能从兜底路径找到
    module._docker_path_cache = None
    original_which2 = shutil.which
    original_paths = module.DOCKER_FALLBACK_PATHS
    fake_docker = Path(context.paths.data_dir) / "fake-docker"
    fake_docker.parent.mkdir(parents=True, exist_ok=True)
    fake_docker.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_docker.chmod(0o755)  # find_docker 会检查可执行位
    try:
        shutil.which = lambda name, *a, **k: None
        module.DOCKER_FALLBACK_PATHS = (str(fake_docker),)
        check(module.find_docker(refresh=True) == str(fake_docker), "PATH 里没有 docker 时从兜底路径找到")
        check(module.find_docker() == str(fake_docker), "查找结果被缓存复用")
        module.DOCKER_FALLBACK_PATHS = ("/definitely/not/here/docker",)
        module._docker_path_cache = None
        check(module.find_docker(refresh=True) is None, "PATH 与兜底路径都没有时返回 None")
        # 找不到 docker 时，超过内联上限的视频应回退成封面而不是静默丢视频
        module._docker_path_cache = None
        plugin.config.display.video_in_forward = True
        tweet6 = make_video_tweet("2006")
        plugin._client = StubClient(
            [tweet6],
            head_sizes={huge_url: 80 * 1024 * 1024, big_url: 40 * 1024 * 1024, small_url: 20 * 1024 * 1024},
            video_bytes=b"V" * (1024 * 1024),
        )
        context.send.reset()
        check(await plugin._deliver(tweet6, "stream-a"), "没有 docker 时大视频仍投递成功（回退封面）")
        check(not context.send.hybrids(), "没有 docker 时不发视频段")
        check(bool(context.send.forwards()), "没有 docker 时封面走聊天记录")
    finally:
        shutil.which = original_which2
        module.DOCKER_FALLBACK_PATHS = original_paths
        module._docker_path_cache = None

    # 7) docker cp 失败时退回 stdin 管道（snap 版 docker 有私有 /tmp，cp 会 lstat 失败）
    spawn_calls: list[list[str]] = []

    class FakeProc:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

        async def communicate(self):
            return b"", b"lstat /tmp/tw-test: no such file or directory"

    async def fake_spawn(*argv, **kwargs):
        spawn_calls.append([str(item) for item in argv])
        return FakeProc(1 if len(argv) > 1 and argv[1] == "cp" else 0)

    original_spawn = module.asyncio.create_subprocess_exec
    module.asyncio.create_subprocess_exec = fake_spawn
    src = Path(context.paths.data_dir) / "cp-src.bin"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"x" * 4096)
    try:
        check(await plugin._docker_cp(src, "snowluma", "/app/data/twvideo/cp-src.bin"), "docker cp 失败后 stdin 管道兜底成功")
        check(len(spawn_calls) == 2, f"先 cp 后 exec 共 2 次调用（实际 {len(spawn_calls)}）")
        if len(spawn_calls) == 2:
            check("cp" in spawn_calls[0], f"第一次是 docker cp：{spawn_calls[0][:3]}")
            check(spawn_calls[1][1:4] == ["exec", "-i", "snowluma"], f"兜底是 docker exec -i：{spawn_calls[1][:4]}")
            check(any("cat >" in part for part in spawn_calls[1]), "兜底命令用 cat > 容器内路径")
    finally:
        module.asyncio.create_subprocess_exec = original_spawn
        src.unlink(missing_ok=True)


async def test_batch_forward(module, plugin, context) -> None:
    """一批新推文打成一个聊天记录，每条推文一个节点。"""

    print("\n[5c] 聊天记录打包")
    now = time.time()

    def make(tweet_id: str, text: str, *, images: int = 0, video: bool = False, ts: float | None = None):
        media = [
            module.TweetMedia(kind="photo", url=f"https://pbs.twimg.com/media/{tweet_id}_{index}.jpg")
            for index in range(images)
        ]
        if video:
            media = [
                module.TweetMedia(
                    kind="video",
                    url=f"https://video.twimg.com/{tweet_id}.mp4",
                    thumbnail_url=f"https://pbs.twimg.com/thumb_{tweet_id}.jpg",
                    duration=10.0,
                    formats=[],
                )
            ]
        return make_tweet(module, tweet_id, now if ts is None else ts, text=text, media=media)

    # 1) 三条纯文字推文 → 一条聊天记录、三个节点，最新在最上面
    batch = [
        make("3001", "第一条", ts=now - 300),
        make("3002", "第二条", ts=now - 200),
        make("3003", "第三条", ts=now - 100),
    ]
    plugin._client = StubClient(batch, image_bytes=b"x" * 256)
    context.send.reset()
    delivered = await plugin._deliver_many(batch, "stream-a")
    check(len(delivered) == 3, f"三条都算投递成功（实际 {len(delivered)}）")
    forwards = context.send.forwards()
    check(len(forwards) == 1, f"只发了一条聊天记录（实际 {len(forwards)}）")
    if forwards:
        check(len(forwards[0][2]) == 3, f"聊天记录里 3 个节点（实际 {len(forwards[0][2])}）")
        texts = context.send.node_texts(forwards[0])
        check("第三条" in texts[0] and "第一条" in texts[2], f"最新的排在最上面：{[t[:6] for t in texts]}")
        nicknames = [node["nickname"] for node in forwards[0][2]]
        check(all("Elon Musk" in name for name in nicknames), f"每个节点用自己的作者昵称：{nicknames[:2]}")

    # 2) 带配图的推文：图片落在各自节点里
    batch2 = [make("3101", "图文A", images=2), make("3102", "图文B", images=1)]
    plugin._client = StubClient(batch2, image_bytes=b"y" * 512)
    context.send.reset()
    delivered = await plugin._deliver_many(batch2, "stream-a")
    check(len(delivered) == 2, "图文批次投递成功")
    forwards = context.send.forwards()
    check(len(forwards) == 1, "图文批次也是一条聊天记录")
    if forwards:
        check(context.send.node_images(forwards[0]) == [2, 1], f"每张图跟自己的推文（实际 {context.send.node_images(forwards[0])}）")
        check(all("配图" in text for text in context.send.node_texts(forwards[0])), "节点文案说明配图张数")

    # 3) 图片总量超预算 → 自动拆成多条聊天记录
    big = b"z" * int(2.5 * 1024 * 1024)  # 单图 2.5MB，低于单图上限 3MB
    batch3 = [make("3201", "大图1", images=1), make("3202", "大图2", images=1), make("3203", "大图3", images=1)]
    plugin._client = StubClient(batch3, image_bytes=big)
    plugin.config.media.max_image_mb = 3.0
    plugin.config.media.max_total_mb = 6.0
    plugin.config.display.batch_max_mb = 7.0
    context.send.reset()
    delivered = await plugin._deliver_many(batch3, "stream-a")
    check(len(delivered) == 3, "超预算批次仍然全部投递")
    forwards = context.send.forwards()
    check(len(forwards) == 2, f"超预算时自动拆成 2 条聊天记录（实际 {len(forwards)}）")
    if len(forwards) == 2:
        sizes = [len(call[2]) for call in forwards]
        check(sizes == [2, 1], f"拆分节点数正确（实际 {sizes}）")

    # 4) 视频推文 + 文字推文：默认把视频放进自己的节点里
    batch4 = [
        make("3301", "文字推文"),
        make("3302", "视频推文", video=True),
        make("3303", "再来一条"),
    ]
    plugin._client = StubClient(
        batch4,
        image_bytes=b"x" * 256,
        head_sizes={"https://video.twimg.com/3302.mp4": 2 * 1024 * 1024},
        video_bytes=b"V" * (1024 * 1024),
    )
    context.send.reset()
    delivered = await plugin._deliver_many(batch4, "stream-a")
    check(len(delivered) == 3, "视频+文字混合批次全部投递")
    check(not context.send.hybrids(), "视频没有单独成条")
    forwards = context.send.forwards()
    check(len(forwards) == 1, "三条推文合并成一条聊天记录")
    if forwards:
        nodes = forwards[0][2]
        check(len(nodes) == 4, f"视频推文占「文字 + 视频」两个节点（实际 {len(nodes)}）")
        videos = context.send.node_videos(forwards[0])
        check([len(item) for item in videos] == [0, 0, 1, 0], f"只有视频节点带视频段（实际 {[len(i) for i in videos]}）")
        check(videos[2] and videos[2][0]["inline"] > 0, "节点里的视频走内联 base64")
        check(not nodes[2]["texts"], "视频节点里只有视频，不带文字（QQ 要求视频必须是消息里唯一元素）")
        check("视频推文" in nodes[1]["texts"][0], "视频推文的正文在它前面的节点里")

    # 4b) 关掉 video_in_forward → 视频退回单独成条
    plugin.config.display.video_in_forward = False
    plugin._client = StubClient(
        batch4,
        image_bytes=b"x" * 256,
        head_sizes={"https://video.twimg.com/3302.mp4": 2 * 1024 * 1024},
        video_bytes=b"V" * (1024 * 1024),
    )
    context.send.reset()
    delivered = await plugin._deliver_many(batch4, "stream-a")
    check(len(delivered) == 3, "关闭 video_in_forward 后仍然全部投递")
    check(len(context.send.hybrids()) == 1, "关闭后视频单独成条")
    forwards = context.send.forwards()
    check(bool(forwards) and len(forwards[0][2]) == 2, "关闭后聊天记录里只有 2 个节点")
    plugin.config.display.video_in_forward = True
    # 4c) 单条视频推文也走聊天记录（正文节点 + 视频节点）
    single_video = [make("3350", "单条视频", video=True)]
    plugin._client = StubClient(
        single_video,
        head_sizes={"https://video.twimg.com/3350.mp4": 1024 * 1024},
        video_bytes=b"V" * (512 * 1024),
    )
    context.send.reset()
    await plugin._deliver_many(single_video, "stream-a")
    forwards = context.send.forwards()
    check(len(forwards) == 1 and len(forwards[0][2]) == 2, "单条视频推文也是一条聊天记录（正文 + 视频两个节点）")
    if forwards:
        check(context.send.node_videos(forwards[0])[0] == [], "第一个节点是正文")
        check(len(context.send.node_videos(forwards[0])[1]) == 1, "第二个节点是视频")

    # 4d) 单条纯文字推文同样用聊天记录
    plugin._client = StubClient([], image_bytes=b"x")
    context.send.reset()
    await plugin._deliver_many([make("3351", "单条文字")], "stream-a")
    forwards = context.send.forwards()
    check(len(forwards) == 1 and len(forwards[0][2]) == 1, "单条纯文字也走聊天记录")
    check("单条文字" in context.send.node_texts(forwards[0])[0], "单条文字内容正确")
    check(not context.send.calls[0][0] == "text", "不再直接发普通文本消息")

    # 5) 关掉批量 → 逐条发
    plugin.config.display.batch_forward = False
    plugin._client = StubClient([], image_bytes=b"x" * 256)
    context.send.reset()
    delivered = await plugin._deliver_many(
        [make("3401", "逐条1", images=1), make("3402", "逐条2", images=1)], "stream-a"
    )
    check(len(delivered) == 2, "关闭批量后仍然投递成功")
    forwards = context.send.forwards()
    check(len(forwards) == 2, f"关闭批量时逐条发送（实际 {len(forwards)} 条）")
    check(all(len(call[2]) == 1 for call in forwards), "逐条发送时每条聊天记录只有一个节点")
    plugin.config.display.batch_forward = True

    # 6) 单条纯文字推文现在也走聊天记录（上面 4d 已覆盖），这里验证节点数上限
    plugin._client = StubClient([], image_bytes=b"x")
    single = make("3501", "单条文字")
    context.send.reset()
    await plugin._deliver_many([single], "stream-a")
    forwards = context.send.forwards()
    check(len(forwards) == 1 and len(forwards[0][2]) == 1, "单条推文只有 1 个节点")

    # 7) /tw_test 预览多条 → 一条聊天记录
    plugin._client = StubClient([make("3601", "预览1"), make("3602", "预览2"), make("3603", "预览3")], image_bytes=b"x")
    context.send.reset()
    count = await plugin._push_latest("elonmusk", "stream-a", count=3)
    check(count == 3, f"预览 3 条全部投递（实际 {count}）")
    forwards = context.send.forwards()
    check(len(forwards) == 1 and len(forwards[0][2]) == 3, "预览多条也只发一条聊天记录")

    # 8) 轮询路径整体走一遍：3 条新推文 → 1 条聊天记录
    state = plugin._require_state()
    entry = state.handle_entry("elonmusk")
    entry["seen_ids"] = ["1"]
    entry["last_seen_ts"] = now - 3600
    entry["streams"] = ["stream-a"]
    poll_tweets = [make("3701", "轮询1"), make("3702", "轮询2"), make("3703", "轮询3")]
    plugin._client = StubClient(poll_tweets, image_bytes=b"x")
    plugin.config.poll.max_tweets_per_poll = 3
    context.send.reset()
    result = await plugin._poll_handle("elonmusk", 20)
    check(result.pushed == 3, f"轮询发现 3 条新推文（实际 {result.pushed}）")
    forwards = context.send.forwards()
    check(len(forwards) == 1 and len(forwards[0][2]) == 3, "轮询的 3 条推文合并成一条聊天记录")


async def test_delivery_format(module, plugin, context) -> None:
    """投递格式：自己轮询到的新推走普通图文，/tw_test 预览走合并转发。"""

    print("\n[5f] 投递格式（轮询 vs /tw_test）")
    now = time.time()

    def make(tweet_id: str, text: str, *, images: int = 0, ts: float | None = None, video: bool = False):
        if video:
            media = [
                module.TweetMedia(
                    kind="video",
                    url=f"https://video.twimg.com/{tweet_id}.mp4",
                    thumbnail_url=f"https://pbs.twimg.com/thumb_{tweet_id}.jpg",
                    duration=8.0,
                    formats=[],
                )
            ]
        else:
            media = [
                module.TweetMedia(kind="photo", url=f"https://pbs.twimg.com/media/{tweet_id}_{index}.jpg")
                for index in range(images)
            ]
        return make_tweet(module, tweet_id, now if ts is None else ts, text=text, media=media)

    # 0) 默认值
    defaults = module.create_plugin().get_default_config()
    check(defaults["display"]["forward_for_poll"] is False, "轮询默认「不用」合并转发")
    check(defaults["display"]["forward_for_test"] is True, "/tw_test 默认「用」合并转发")
    check(defaults["display"]["use_forward"] is True, "合并转发总开关仍默认开着")

    # 1) 轮询路径：普通图文，最旧的先发（聊天里从上往下就是时间顺序）
    plugin.config.display.forward_for_poll = False
    plugin.config.display.forward_for_test = True
    plugin.config.display.batch_forward = True
    plugin.config.display.use_forward = True
    batch = [make("5001", "旧推", ts=now - 300), make("5002", "新推", ts=now - 100)]
    plugin._client = StubClient(batch, image_bytes=b"x" * 128)
    context.send.reset()
    delivered = await plugin._deliver_many(batch, "stream-a")
    check(len(delivered) == 2, f"轮询格式两条都投递成功（实际 {len(delivered)}）")
    check(not context.send.forwards(), "轮询格式不发合并转发")
    texts = [call[2] for call in context.send.calls if call[0] == "text"]
    check(len(texts) == 2, f"两条各发一条普通消息（实际 {len(texts)}）")
    check("旧推" in texts[0] and "新推" in texts[1], "普通图文按时间顺序发（最旧的先发）")

    # 2) 轮询路径的配图：逐张普通图片消息，不打包
    plugin._client = StubClient([], image_bytes=b"y" * 256)
    context.send.reset()
    await plugin._deliver_many([make("5003", "带图推文", images=2)], "stream-a")
    check(not context.send.forwards(), "轮询格式的配图不打包成聊天记录")
    check(len([c for c in context.send.calls if c[0] == "image"]) == 2, "配图逐张发普通图片消息")

    # 3) 轮询路径的视频：单独成条，不进聊天记录
    video_tweet = make("5004", "视频推文", video=True)
    plugin._client = StubClient(
        [video_tweet],
        head_sizes={"https://video.twimg.com/5004.mp4": 900 * 1024},
        video_bytes=b"V" * 4096,
    )
    context.send.reset()
    await plugin._deliver_many([video_tweet], "stream-a")
    check(not context.send.forwards(), "轮询格式下视频不进聊天记录")
    check(len(context.send.hybrids()) == 1, "视频单独成条（混合消息）")

    # 4) /tw_test 预览：一条聊天记录，每条推文一个节点，最新在最上面
    preview = [make("5101", "预览旧", ts=now - 300), make("5102", "预览新", ts=now - 100)]
    plugin._client = StubClient(preview, image_bytes=b"x")
    context.send.reset()
    result = await plugin.handle_test(
        stream_id="stream-a", matched_groups={"rest": "elonmusk 2"}, user_id="10001"
    )
    check(result[0] is True, "/tw_test 执行成功")
    forwards = context.send.forwards()
    check(len(forwards) == 1, f"/tw_test 发一条聊天记录（实际 {len(forwards)}）")
    if forwards:
        check(len(forwards[0][2]) == 2, "两条预览推文各占一个节点")
        node_texts = context.send.node_texts(forwards[0])
        check("预览新" in node_texts[0] and "预览旧" in node_texts[1], f"聊天记录里最新在最上面：{[t[:6] for t in node_texts]}")

    # 5) 关掉 forward_for_test → 预览也走普通图文
    plugin.config.display.forward_for_test = False
    plugin._client = StubClient(preview, image_bytes=b"x")
    context.send.reset()
    await plugin.handle_test(stream_id="stream-a", matched_groups={"rest": "elonmusk 2"}, user_id="10001")
    check(not context.send.forwards(), "关掉 forward_for_test 后预览不发聊天记录")

    # 6) 总开关 use_forward=False 时，即使 forward_for_test=True 也不发聊天记录
    plugin.config.display.forward_for_test = True
    plugin.config.display.use_forward = False
    plugin._client = StubClient(preview, image_bytes=b"x")
    context.send.reset()
    await plugin.handle_test(stream_id="stream-a", matched_groups={"rest": "elonmusk 2"}, user_id="10001")
    check(not context.send.forwards(), "总开关关掉后任何路径都不发聊天记录")
    plugin.config.display.use_forward = True
    plugin.config.display.forward_for_poll = True  # 还原成其余用例依赖的值


async def test_translation(module, plugin, context) -> None:
    """自动翻译：模型选择、替换原文、跳过条件、失败兜底、缓存。"""

    print("\n[5d] 自动翻译")
    now = time.time()

    check(module.match_target_lang_code("简体中文") == "zh", "目标语言识别：简体中文 → zh")
    check(module.match_target_lang_code("English") == "en", "目标语言识别：English → en")
    check(module.match_target_lang_code("火星文") == "", "识别不了的语言返回空")
    check(module.clean_translation('```\n你好\n```') == "你好", "清理代码块包裹")
    check(module.clean_translation('"你好"') == "你好", "清理包裹引号")
    check(module.clean_translation("译文：你好") == "你好", "清理「译文：」前缀")

    config = plugin.config.translation
    config.enabled = True
    config.model_task = "replyer"
    config.target_lang = "简体中文"
    config.max_chars = 1200
    context.llm.reset()
    context.llm.echo = False
    context.llm.response = "这是一条翻译后的推文"

    tweet = make_tweet(module, "4001", now, text="This is a tweet", lang="en")
    await plugin._translate_tweets([tweet])
    check(tweet.translation == "这是一条翻译后的推文", f"译文写回了推文（实际 {tweet.translation!r}）")
    check(len(context.llm.calls) == 1, "调用了一次模型")
    if context.llm.calls:
        call = context.llm.calls[0]
        check(call["model"] == "replyer", f"用的是 replyer 模型（实际 {call['model']!r}）")
        check(isinstance(call["prompt"], list) and call["prompt"][0]["role"] == "system", "提示词带 system 角色")
        check("简体中文" in call["prompt"][0]["content"], "提示词里带上了目标语言")
        check(call["prompt"][1]["content"] == "This is a tweet", "待翻译正文作为 user 消息传入")
        check(call["kwargs"].get("rpc_timeout_ms", 0) >= 45000, "翻译请求带上了单独的超时")

    # 渲染时用译文替换原文，不保留原语言正文
    rendered = plugin._render_tweet(tweet, [])
    check("这是一条翻译后的推文" in rendered, "渲染使用译文")
    check("This is a tweet" not in rendered, "原文不再出现（替换不保留）")

    # 缓存：再翻一次不再调用模型
    other = make_tweet(module, "4001", now, text="This is a tweet", lang="en")
    context.llm.reset()
    await plugin._translate_tweets([other])
    check(not context.llm.calls, "同一条推文命中缓存，不重复调用模型")
    check(other.translation == "这是一条翻译后的推文", "缓存命中也写入译文")

    # 已经是目标语言 → 跳过
    context.llm.reset()
    zh_tweet = make_tweet(module, "4002", now, text="这是一条中文推文，不需要翻译", lang="zh")
    await plugin._translate_tweets([zh_tweet])
    check(not context.llm.calls and not zh_tweet.translation, "中文推文跳过翻译")
    check("这是一条中文推文" in plugin._render_tweet(zh_tweet, []), "跳过后照常显示原文")

    # 接口没给 lang 时按字符比例兜底
    context.llm.reset()
    fallback = make_tweet(module, "4003", now, text="这是一条完全没有语言标记的中文推文内容", lang="")
    await plugin._translate_tweets([fallback])
    check(not context.llm.calls, "没有 lang 时按汉字比例判断为中文并跳过")

    # 超长推文跳过
    context.llm.reset()
    config.max_chars = 20
    long_tweet = make_tweet(module, "4004", now, text="x" * 50, lang="en")
    await plugin._translate_tweets([long_tweet])
    check(not context.llm.calls and not long_tweet.translation, "超长推文跳过翻译")
    config.max_chars = 1200

    # 模型失败 → 保留原文
    context.llm.reset()
    context.llm.success = False
    failed = make_tweet(module, "4005", now, text="please keep me", lang="en")
    await plugin._translate_tweets([failed])
    check(not failed.translation, "翻译失败不写入译文")
    check("please keep me" in plugin._render_tweet(failed, []), "翻译失败时保留原文")
    context.llm.success = True

    # 引用推文一起翻译
    context.llm.reset()
    quoted = make_tweet(module, "4006", now, text="main text", lang="en")
    quoted.quote_text = "quoted english text"
    quoted.quote_author = "someone"
    await plugin._translate_tweets([quoted])
    check(quoted.quote_translation == "这是一条翻译后的推文", "引用内容也被翻译")
    rendered = plugin._render_tweet(quoted, [])
    check("quoted english text" not in rendered, "引用原文同样被替换")

    # 关掉翻译 → 完全不动
    context.llm.reset()
    config.enabled = False
    off_tweet = make_tweet(module, "4007", now, text="no translation please", lang="en")
    await plugin._translate_tweets([off_tweet])
    check(not context.llm.calls and not off_tweet.translation, "关闭翻译后不调用模型")
    config.enabled = True
    context.llm.echo = True

    # 投递链路里生效：聊天记录节点文本是译文
    context.llm.echo = False
    context.llm.response = "翻译好的正文"
    batch = [make_tweet(module, "4008", now, text="tweet one", lang="en"), make_tweet(module, "4009", now, text="tweet two", lang="en")]
    plugin._client = StubClient(batch, image_bytes=b"x" * 256)
    context.send.reset()
    await plugin._deliver_many(batch, "stream-a")
    forwards = context.send.forwards()
    check(bool(forwards), "带翻译的批次照常打包")
    if forwards:
        texts = context.send.node_texts(forwards[0])
        check(all("翻译好的正文" in text for text in texts), f"节点里用的是译文：{texts[0][:40]!r}")
        check(all("tweet one" not in text and "tweet two" not in text for text in texts), "节点里不再有原文")
    context.llm.echo = True


async def test_link_preview(module, plugin, context) -> None:
    """链接正文抓取：Steam RSS 路径、通用网页、翻译、缓存与跳过规则。"""

    print("\n[5e] 链接内容")
    now = time.time()
    steam_url = "https://store.steampowered.com/news/app/1973530/view/705530923042472109"

    # --- 纯函数 ---
    links = module.extract_links(
        f"看这个 {steam_url} 还有 https://x.com/a/status/1 https://t.co/abc "
        "https://pbs.twimg.com/media/x.jpg https://example.com/a.png https://example.com/read"
    )
    check(links == [steam_url, "https://example.com/read"], f"链接筛选正确：{links}")
    check(module.extract_links("没有链接") == [], "无链接返回空")

    boilerplate = "\n".join(
        ["登录", "商店", "主页", "探索队列", "愿望单", "新闻", "社区", "关于", "客服", "隐私政策", "法律信息", "退款"]
    )
    check(module.looks_like_boilerplate(boilerplate), "导航栏文本被判定为垃圾")
    check(
        not module.looks_like_boilerplate(
            "In the combat phase, units on both sides will act simultaneously. "
            "During the scramble, characters targeting each other may Clash."
        ),
        "正常正文不被误判",
    )
    # 整页菜单（例如 SPA 官网）：行数不少但全是短条目
    nav_dump = "\n".join(
        [
            "SpaceX - Events",
            "Skip to main content",
            "Investors",
            "Financials",
            "Events",
            "Leadership",
            "Stock Information",
            "Updates",
            "Upcoming Events",
            "Past Events",
            "Selecting a year will change event content",
            "Select a year:",
            "Loading...",
            "Sign up to receive SpaceX investor updates",
            "Personal Information",
            "Email Address *",
        ]
    )
    check(module.looks_like_boilerplate(nav_dump), "整页菜单被判为垃圾")
    check(
        not module.looks_like_boilerplate(
            "Falcon 9 launched the USSF-153 mission from pad 4E in California today. "
            "The first stage landed on the droneship about eight minutes later, "
            "marking the 300th landing of an orbital-class rocket booster."
        ),
        "短正文不会被误判成菜单",
    )
    check(
        module.extract_media_links('<div data-youtube="h5eR27Gle1U"></div>')
        == ["https://www.youtube.com/watch?v=h5eR27Gle1U"],
        "从嵌入里提取视频链接",
    )
    check(
        "示例描述" in module.extract_meta_description('<meta property="og:description" content="示例描述，这里故意写长一点凑够二十个字符">'),
        "读取 og:description",
    )

    rss_url = "https://store.steampowered.com/feeds/news/app/1973530/?l=schinese"
    rss_body = (
        "<rss><channel>"
        "<item><title>[Limbus Company] Chapter10 PV</title>"
        f"<link><![CDATA[{steam_url}]]></link>"
        '<description>&lt;div data-youtube=&quot;h5eR27Gle1U&quot;&gt;&lt;/div&gt;</description></item>'
        "<item><title>另一条公告</title><link>https://store.steampowered.com/news/app/1973530/view/999</link>"
        "<description>&lt;p&gt;更新内容&lt;/p&gt;</description></item>"
        "</channel></rss>"
    )
    plugin.config.link.enabled = True
    plugin.config.link.max_links = 1
    plugin.config.link.max_chars = 700
    plugin.config.link.link_language = "schinese"
    plugin.config.translation.enabled = False

    plugin._client = StubClient([], pages={rss_url: rss_body})
    title, body = await plugin._fetch_link_preview(steam_url)
    check(title == "[Limbus Company] Chapter10 PV", f"Steam 公告标题：{title!r}")
    check("youtube.com/watch?v=h5eR27Gle1U" in body, f"只有视频嵌入时给出视频链接：{body!r}")

    # --- 通用网页路径 ---
    article_url = "https://example.com/post"
    plugin._client = StubClient(
        [],
        pages={
            article_url: (
                "<html><head><title>一篇长文</title>"
                '<meta property="og:description" content="这是摘要，够长够长够长够长够长" /></head>'
                "<body><article><p>" + "正文内容。" * 40 + "</p></article></body></html>"
            )
        },
    )
    gtitle, gbody = await plugin._fetch_link_preview(article_url)
    check(gtitle == "一篇长文", f"通用网页标题：{gtitle!r}")
    check(len(gbody) > 50 and "正文内容" in gbody, "通用网页抽到正文")

    # --- 挂到推文上 + 渲染 ---
    context.llm.reset()
    context.llm.echo = True
    tweet = make_tweet(module, "5001", now, text=f"看这个 {steam_url}", lang="en")
    plugin._client = StubClient([tweet], pages={rss_url: rss_body})
    await plugin._attach_link_previews([tweet])
    check(
        tweet.link_preview.startswith("📄 链接内容 · [Limbus Company] Chapter10 PV"),
        f"推文挂上链接内容：{tweet.link_preview[:40]!r}",
    )
    rendered = plugin._render_tweet(tweet, [])
    check("📄 链接内容 · [Limbus Company] Chapter10 PV" in rendered, "渲染里带链接内容")
    check(rendered.index("📄") < rendered.index("🔗"), "链接内容排在原推链接之前")

    # 缓存：同一条链接不再重复抓取
    plugin._client.fetched_pages.clear()
    again = make_tweet(module, "5002", now, text=f"看这个 {steam_url}", lang="en")
    await plugin._attach_link_previews([again])
    check(not plugin._client.fetched_pages, "同一链接命中缓存")
    check(again.link_preview == tweet.link_preview, "缓存内容一致")

    # 关掉 / max_links = 0
    plugin.config.link.enabled = False
    plugin._client = StubClient([], pages={rss_url: rss_body})
    disabled = make_tweet(module, "5003", now, text=f"看这个 {steam_url}")
    await plugin._attach_link_previews([disabled])
    check(not disabled.link_preview and not plugin._client.fetched_pages, "关闭后不抓取")
    plugin.config.link.enabled = True
    plugin.config.link.max_links = 0
    await plugin._attach_link_previews([disabled])
    check(not disabled.link_preview, "max_links=0 时不抓取")
    plugin.config.link.max_links = 1

    # 抓不到内容时不留垃圾（换一条没进过缓存的链接）
    plugin.config.link.enabled = True
    fresh_url = "https://store.steampowered.com/news/app/1973530/view/123456789"
    plugin._client = StubClient([], pages={})
    empty = make_tweet(module, "5004", now, text=f"看这个 {fresh_url}")
    await plugin._attach_link_previews([empty])
    check(not empty.link_preview, "抓取失败时不留空块")
    check(bool(plugin._client.fetched_pages), "确实尝试过抓取")

    # 只有标题、没有正文（菜单页之类）同样不留块
    title_only_url = "https://example.com/events"
    plugin._client = StubClient(
        [],
        pages={
            title_only_url: "<html><head><title>Example - Events</title></head><body>"
            + "".join(f"<div>menu item {index}</div>" for index in range(15))
            + "</body></html>"
        },
    )
    title_only = make_tweet(module, "5011", now, text=f"看这个 {title_only_url}")
    await plugin._attach_link_previews([title_only])
    check(not title_only.link_preview, "只有标题没有正文时不加链接内容块")

    # 直连抓不到 → 自动换代理重试
    proxy_url = "https://store.steampowered.com/news/app/1973530/view/555000111"
    plugin._client = StubClient([], pages={})
    plugin._client.proxy_pages = {rss_url: rss_body.replace("705530923042472109", "555000111")}
    plugin.config.link.fallback_proxy = True
    plugin.config.twitter.proxy = "http://127.0.0.1:7890"
    via_proxy = make_tweet(module, "5006", now, text=f"看这个 {proxy_url}", lang="en")
    await plugin._attach_link_previews([via_proxy])
    check(bool(via_proxy.link_preview), f"直连失败后走代理拿到了内容：{via_proxy.link_preview[:30]!r}")
    used_proxy = [item for item in plugin._client.fetched_pages if item[1]]
    check(bool(used_proxy), f"确实用了代理重试：{plugin._client.fetched_pages[-1]}")

    # 关掉代理兜底 → 抓不到就抓不到
    plugin._client = StubClient([], pages={})
    plugin._client.proxy_pages = {rss_url: rss_body.replace("705530923042472109", "666000222")}
    plugin.config.link.fallback_proxy = False
    no_fallback = make_tweet(
        module, "5007", now, text="看这个 https://store.steampowered.com/news/app/1973530/view/666000222"
    )
    await plugin._attach_link_previews([no_fallback])
    check(not no_fallback.link_preview, "关闭代理兜底后不再重试")
    check(not [item for item in plugin._client.fetched_pages if item[1]], "关闭后完全没有走代理")
    plugin.config.link.fallback_proxy = True

    # --- 长文：分段翻译、最后才截断 ---
    paragraph = "This is a long paragraph about the update. " * 6  # ~250 字符
    long_article = "\n".join(paragraph for _ in range(12))  # ~3000 字符
    chunks = module.split_text_chunks(long_article, 1200)
    check(len(chunks) >= 3, f"长文被切成多段（{len(chunks)} 段）")
    check(all(len(chunk) <= 1200 for chunk in chunks), "每段都不超过上限")
    check("\n".join(chunks).split() == long_article.split(), "切分不丢内容")

    # 直接验证分段翻译：长文要多次调用，短的一次
    plugin.config.translation.enabled = True
    context.llm.reset()
    context.llm.echo = False
    context.llm.response = "【分段译文】"
    plugin.config.link.translate_chunk_chars = 1200
    joined = await plugin._translate_long_text(long_article)
    check(len(context.llm.calls) >= 3, f"长文分段翻译调用 {len(context.llm.calls)} 次模型")
    check(joined.count("【分段译文】") >= 3, "每段译文都拼进了结果")

    context.llm.reset()
    short_text = "A short notice."
    single = await plugin._translate_long_text(short_text)
    check(len(context.llm.calls) == 1 and single == "【分段译文】", "短文本只翻一次")
    context.llm.echo = True

    single_paragraph = "句子。" * 400
    hard_chunks = module.split_text_chunks(single_paragraph, 300)
    check(all(len(chunk) <= 300 for chunk in hard_chunks), "单段超长时按句子硬切")

    truncated = module.truncate_at_paragraph("第一段\n第二段\n" + "x" * 100, 30)
    check("已截断" in truncated and truncated.startswith("第一段"), f"截断切在段落处：{truncated[:20]!r}")
    check(module.truncate_at_paragraph("短文本", 100) == "短文本", "没超限就不截断")

    # 分段翻译：长正文应该多次调用模型，且不做 1200 字上限的拦截
    plugin.config.link.max_chars = 3000
    plugin.config.link.translate_chunk_chars = 1200
    plugin.config.translation.max_chars = 1200
    plugin.config.translation.enabled = True
    context.llm.reset()
    context.llm.echo = False
    context.llm.response = "【译文片段】"
    long_url = "https://example.com/long-article"
    # 段落内容各不相同，避免 trafilatura 当成重复模板去重
    distinct_paragraphs = [
        f"Paragraph {index}: the update brings changes to skill {index} and stage {index * 7}. "
        + "Details follow in this section of the notice. " * 3
        for index in range(12)
    ]
    plugin._client = StubClient(
        [],
        pages={
            long_url: (
                "<html><head><title>Long Notice</title></head><body><article>"
                + "".join(f"<p>{text}</p>" for text in distinct_paragraphs)
                + "</article></body></html>"
            )
        },
    )
    long_tweet = make_tweet(module, "5008", now, text=f"see {long_url}", lang="en")
    await plugin._attach_link_previews([long_tweet])
    check(len(context.llm.calls) >= 2, f"长正文分段翻译，调用 {len(context.llm.calls)} 次模型")
    check(long_tweet.link_preview.count("【译文片段】") >= 2, "每段译文都拼进了结果")
    check(long_tweet.link_preview.startswith("📄 链接内容 · Long Notice"), "长文也带标题")
    check("This is a long paragraph" not in long_tweet.link_preview, "长文正文已被翻译（原文不保留）")

    # 超过 max_chars 才截断，且截断发生在翻译之后
    plugin.config.link.max_chars = 300
    context.llm.reset()
    context.llm.response = "译文内容。" * 100  # 600 字符
    trunc_url = "https://example.com/trunc"
    plugin._client = StubClient(
        [],
        pages={
            trunc_url: (
                "<html><head><title>超长公告</title></head><body><article>"
                + "".join(f"<p>{paragraph}</p>" for _ in range(6))
                + "</article></body></html>"
            )
        },
    )
    trunc_tweet = make_tweet(module, "5009", now, text=f"see {trunc_url}", lang="en")
    await plugin._attach_link_previews([trunc_tweet])
    check("已截断" in trunc_tweet.link_preview, "超过上限时明确标注已截断")
    check(len(trunc_tweet.link_preview) < 500, f"截断后长度受控（{len(trunc_tweet.link_preview)}）")
    check(context.llm.calls, "截断前仍然翻译了全文")
    plugin.config.link.max_chars = 3000
    plugin.config.translation.max_chars = 1200
    context.llm.echo = True

    # 正文需要翻译时，走翻译
    plugin.config.translation.enabled = True
    context.llm.reset()
    context.llm.echo = False
    context.llm.response = "【翻译后的公告正文】"
    translated_url = "https://example.com/en-post"
    plugin._client = StubClient(
        [],
        pages={
            translated_url: (
                "<html><head><title>Patch Notes</title></head><body><article><p>"
                + "Bug fixes and improvements. " * 20
                + "</p></article></body></html>"
            )
        },
    )
    need_translate = make_tweet(module, "5005", now, text=f"see {translated_url}", lang="en")
    await plugin._attach_link_previews([need_translate])
    check("【翻译后的公告正文】" in need_translate.link_preview, f"链接正文被翻译：{need_translate.link_preview[:50]!r}")
    check(len(context.llm.calls) >= 1, "翻译链接正文时调用了模型")
    context.llm.echo = True

    # --- 排版 ---
    plugin.config.translation.enabled = False
    layout = make_tweet(module, "5010", now, text=f"正文第一行\n\n\n\n正文第二行\n\n{steam_url}", lang="en")
    layout.likes = 1234
    layout.views = 56000
    plugin._client = StubClient([], pages={rss_url: rss_body})
    await plugin._attach_link_previews([layout])
    rendered = plugin._render_tweet(layout, [])

    lines = rendered.split("\n")
    check(lines[0].startswith("🐦 "), "第一行是作者头")
    check(lines[1] == plugin.config.display.header_divider, f"作者行下面有分隔线：{lines[1]!r}")
    check("\n\n\n" not in rendered, "正文里的多余空行被收敛")
    check("\n\n📄 链接内容" in rendered, "链接内容前面空一行")
    check("\n\n🔗" in rendered, "尾部链接前面空一行")
    check(rendered.endswith("👁 5.6万"), f"统计行收尾：{rendered[-20:]!r}")
    check(steam_url not in rendered, "已展开的链接不再重复出现在正文里")

    # 关掉分隔线 / 不隐藏链接
    plugin.config.display.header_divider = ""
    plugin.config.display.hide_expanded_url = False
    rendered2 = plugin._render_tweet(layout, [])
    check(plugin.config.display.header_divider not in rendered2.split("\n")[1:2], "分隔线可关闭")
    check(steam_url in rendered2, "关闭 hide_expanded_url 后正文保留链接")
    plugin.config.display.header_divider = "────────────────"
    plugin.config.display.hide_expanded_url = True
    plugin.config.translation.enabled = True


async def test_commands(module, plugin, context) -> None:
    """命令回复测试。"""

    print("\n[6] 命令回复")
    state = plugin._require_state()
    # 10001 当作管理员：跨聊天命令（/tw_all /tw_del /tw_reset /tw_check /tw_interval）
    # 现在默认只允许管理员与本地操作员，下面的用例都用它来调用
    plugin.config.command.admins = ["10001"]

    context.send.reset()
    await plugin.handle_list(stream_id="stream-a", user_id="10001")
    listing = context.send.last_text()
    check("本聊天订阅了" in listing, "/tw_list 输出订阅数")
    check("elonmusk" in listing, "/tw_list 列出推主")

    context.send.reset()
    await plugin.handle_all(stream_id="stream-a", user_id="10001")
    all_text = context.send.last_text()
    check("全部订阅" in all_text, "/tw_all 输出总览")
    check("stream-a" in all_text or "测试群A" in all_text, "/tw_all 显示推送目标")

    context.send.reset()
    await plugin.handle_status(stream_id="stream-a", user_id="10001")
    status_text = context.send.last_text()
    check("轮询间隔" in status_text, "/tw_status 显示间隔")
    check("10 分钟" in status_text, "/tw_status 间隔默认 10 分钟")

    context.send.reset()
    await plugin.handle_interval(stream_id="stream-a", matched_groups={"value": "15"}, user_id="10001")
    check("15 分钟" in context.send.last_text(), "/tw_interval 设置成功")
    check(plugin._effective_interval_minutes() == 15, "间隔覆盖已生效")
    saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    check(saved["runtime"]["interval_minutes"] == 15, "间隔覆盖已落盘")

    context.send.reset()
    await plugin.handle_interval(stream_id="stream-a", matched_groups={"value": "reset"}, user_id="10001")
    check(plugin._effective_interval_minutes() == 10, "/tw_interval reset 恢复配置值")

    context.send.reset()
    await plugin.handle_interval(stream_id="stream-a", matched_groups={"value": "abc"}, user_id="10001")
    check("必须是分钟数" in context.send.last_text(), "非法间隔有提示")

    context.send.reset()
    await plugin.handle_off(stream_id="stream-b", user_id="10001")
    check(bool(state.stream_entry("stream-b")["paused"]), "/tw_off 暂停生效")
    context.send.reset()
    await plugin.handle_on(stream_id="stream-b", user_id="10001")
    check(not bool(state.stream_entry("stream-b")["paused"]), "/tw_on 恢复生效")

    context.send.reset()
    await plugin.handle_help(stream_id="stream-a", user_id="10001")
    check("/tw_sub" in context.send.last_text(), "/tw_help 输出帮助")

    context.send.reset()
    await plugin.handle_reset(stream_id="stream-a", matched_groups={"handles": "elonmusk"}, user_id="10001")
    check("基线已重建" in context.send.last_text(), "/tw_reset 重建基线")
    check(plugin._require_state().handles["elonmusk"]["seen_ids"] == [], "基线确实被清空")

    # 权限
    plugin.config.command.admin_only = True
    plugin.config.command.admins = ["10001"]
    context.send.reset()
    denied = await plugin.handle_sub(
        stream_id="stream-a", matched_groups={"handles": "jack"}, user_id="99999", is_local_operator=False
    )
    check(denied[0] is False and "没有权限" in context.send.last_text(), "非管理员被拒绝")
    context.send.reset()
    allowed = await plugin.handle_list(stream_id="stream-a", user_id="10001")
    check(allowed[0] is True, "管理员放行")
    plugin.config.command.admin_only = False

    # 跨聊天命令默认收紧：普通成员不能看/改全局订阅与轮询状态
    plugin.config.command.cross_chat_admin_only = True
    plugin.config.command.admins = ["10001"]
    context.send.reset()
    blocked_all = await plugin.handle_all(stream_id="stream-a", user_id="99999")
    check(blocked_all[0] is False, "普通成员不能用 /tw_all")
    check("影响所有聊天" in context.send.last_text(), f"/tw_all 的拒绝理由说清影响面：{context.send.last_text().strip()}")
    context.send.reset()
    blocked_interval = await plugin.handle_interval(
        stream_id="stream-a", matched_groups={"value": "5"}, user_id="99999"
    )
    check(blocked_interval[0] is False, "普通成员不能用 /tw_interval")
    check(plugin._effective_interval_minutes() == 10, "被拒绝的 /tw_interval 没有真的改掉间隔")
    context.send.reset()
    blocked_del = await plugin.handle_del(stream_id="stream-a", matched_groups={"handles": "elonmusk"}, user_id="99999")
    check(blocked_del[0] is False, "普通成员不能用 /tw_del")
    check("elonmusk" in plugin._require_state().handles, "被拒绝的 /tw_del 没有真的删掉订阅")
    context.send.reset()
    blocked_reset = await plugin.handle_reset(
        stream_id="stream-a", matched_groups={"handles": "elonmusk"}, user_id="99999"
    )
    check(blocked_reset[0] is False, "普通成员不能用 /tw_reset")
    context.send.reset()
    blocked_check = await plugin.handle_check(stream_id="stream-a", user_id="99999")
    check(blocked_check[0] is False, "普通成员不能用 /tw_check")

    # 聊天内命令对普通成员保持可用
    context.send.reset()
    local_list = await plugin.handle_list(stream_id="stream-a", user_id="99999")
    check(local_list[0] is True, "普通成员仍能用聊天内的 /tw_list")
    context.send.reset()
    local_sub = await plugin.handle_sub(stream_id="stream-a", matched_groups={"handles": "jack"}, user_id="99999")
    check(local_sub[0] is True, "普通成员仍能订阅自己所在的聊天")
    plugin._require_state().handles.pop("jack", None)

    # 本地操作员始终放行；管理员同样放行
    context.send.reset()
    operator_ok = await plugin.handle_all(stream_id="stream-a", user_id="", is_local_operator=True)
    check(operator_ok[0] is True, "本地操作员可以用跨聊天命令")
    context.send.reset()
    admin_ok = await plugin.handle_all(stream_id="stream-a", user_id="10001")
    check(admin_ok[0] is True, "admins 里的管理员可以用跨聊天命令")

    # 显式关掉收紧开关后，普通成员又能用（老行为）
    plugin.config.command.cross_chat_admin_only = False
    context.send.reset()
    reopened = await plugin.handle_all(stream_id="stream-a", user_id="99999")
    check(reopened[0] is True, "关掉 cross_chat_admin_only 后普通成员可用")
    plugin.config.command.cross_chat_admin_only = True

    context.send.reset()
    await plugin.handle_del(stream_id="stream-a", matched_groups={"handles": "elonmuskk"}, user_id="10001")
    typo_reply = context.send.last_text()
    check("你是不是想删除 @elonmusk" in typo_reply, f"手误时提示最接近的推主：{typo_reply.strip()}")

    context.send.reset()
    removed = await plugin.handle_del(stream_id="stream-a", matched_groups={"handles": "elonmusk"}, user_id="10001")
    check("已删除" in context.send.last_text(), "/tw_del 删除订阅")
    check("elonmusk" not in plugin._require_state().handles, "订阅确实被删除")


def make_internal_link_tweet(module):
    """构造一条正文里带内网链接的推文（模拟恶意推文）。"""

    return module.Tweet(
        id="999001",
        url="https://x.com/a/status/999001",
        text="看这个 http://127.0.0.1:8080/secret 和 http://169.254.169.254/latest/meta-data/",
        created_ts=time.time(),
        author_name="A",
        author_screen_name="a",
    )


async def test_url_safety_async(module, plugin, context) -> None:
    """SSRF 校验：URL 级判定 + 插件层开关 + 抓取前拦截。"""

    print("\n[7] 外链 SSRF 防护")

    async def expect_blocked(url: str, label: str) -> None:
        allowed, reason = await module.url_is_public(url)
        check(not allowed, f"{label} 被拦截：{url}（{reason}）")

    await expect_blocked("http://127.0.0.1:7890/", "本机回环")
    await expect_blocked("http://localhost/admin", "localhost")
    await expect_blocked("http://192.168.1.1/", "内网段")
    await expect_blocked("http://169.254.169.254/latest/meta-data/", "云 metadata")
    await expect_blocked("http://[::1]/", "IPv6 回环")
    await expect_blocked("file:///etc/passwd", "非 http(s) 协议")
    await expect_blocked("http://2130706433/", "十进制写法的 127.0.0.1")
    await expect_blocked("http://metadata.google.internal/computeMetadata/v1/", "云厂商内网域名")
    await expect_blocked("", "空 URL")

    allowed, reason = await module.url_is_public("https://example.com/")
    check(allowed, f"公网链接放行：{reason}")

    # 插件层：开关打开时不再拦截
    check(plugin.config.link.allow_private_hosts is False, "默认不开私网抓取")
    ok, _ = await plugin._url_allowed("http://127.0.0.1:7890/")
    check(not ok, "插件默认拦截内网链接")
    plugin.config.link.allow_private_hosts = True
    ok, _ = await plugin._url_allowed("http://127.0.0.1:7890/")
    check(ok, "allow_private_hosts 打开后放行")
    plugin.config.link.allow_private_hosts = False

    # 抓取前拦截：校验不通过时不应该发出请求，返回空串
    client = plugin._client
    check(client is not None, "测试用客户端已就绪")
    if client is not None:
        page = await client.fetch_text(
            "http://127.0.0.1:7890/", timeout=5, max_bytes=4096, validate=plugin._url_allowed
        )
        check(page == "", "校验不通过时 fetch_text 直接返回空串")

    # 链接预览整条链路：内网链接不会产生链接内容块
    tweet = make_internal_link_tweet(module)
    await plugin._attach_link_previews([tweet])
    check(not tweet.link_preview, "内网链接不会抓取成链接内容")
    check(not tweet.expanded_links, "内网链接不会被标记成已展开")


async def main() -> int:
    """执行全部测试。"""

    module = load_plugin_module()
    test_pure_functions(module)
    test_url_safety(module)
    test_components(module)

    if STATE_PATH.exists():
        STATE_PATH.unlink()
    if STATE_PATH.parent.exists():
        shutil.rmtree(DATA_DIR, ignore_errors=True)

    plugin = module.create_plugin()
    config = plugin.get_default_config()
    config["poll"]["initial_delay_seconds"] = 3600  # 不让后台轮询干扰测试
    # 老用例测的是「合并转发」那套机制本身，所以这里把轮询路径也打开转发；
    # 新的默认行为（轮询走普通图文、/tw_test 走聊天记录）在 [5d] 里单独测。
    config["display"]["forward_for_poll"] = True
    plugin.set_plugin_config(config)
    context = FakeContext(DATA_DIR)
    plugin._set_context(context)

    try:
        await test_subscribe(module, plugin, context)
        await test_polling(module, plugin, context)
        await test_delivery_paths(module, plugin, context)
        await test_video_delivery(module, plugin, context)
        await test_batch_forward(module, plugin, context)
        await test_translation(module, plugin, context)
        await test_link_preview(module, plugin, context)
        await test_delivery_format(module, plugin, context)
        await test_url_safety_async(module, plugin, context)
        await test_commands(module, plugin, context)
    finally:
        await plugin.on_unload()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"\033[31m失败 {len(FAILURES)} / {CHECKS}\033[0m")
        for item in FAILURES:
            print("  -", item)
        return 1
    print(f"\033[32m全部通过：{CHECKS} 项检查\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
