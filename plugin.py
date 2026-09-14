"""推特转发插件（MaiBot / FxTwitter）。

功能概览
--------
1. 按配置的间隔（默认 10 分钟）轮询订阅推主的最新推文；
2. 发现新推文后，把正文 + 配图推送到订阅它的聊天流；
3. 提供一组斜杠命令，在聊天里直接完成订阅管理与参数调整。

数据来源
--------
* 主接口：``https://api.fxtwitter.com/2/profile/<handle>/statuses``（FxEmbed v2 JSON）
* 兜底接口：``https://fxtwitter.com/<handle>/feed.atom.xml``（FxEmbed Atom feed）
* 图片托管在 ``pbs.twimg.com``，国内直连不通，默认走本地代理下载后再发送。

状态保存在 ``data/plugins/polarbear.twitter-forwarder/state.json``，
WebUI 配置页只是"基线配置"，聊天里的命令改动作为运行时覆盖保存在状态文件中。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import html as html_module
import importlib.util
import ipaddress
import json
import logging
import os
import random
import re
import shlex
import shutil
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse

try:  # aiohttp 由宿主环境提供；缺失时插件降级为不可用而不是加载失败
    import aiohttp
except Exception:  # pragma: no cover - 仅在缺依赖的环境触发
    aiohttp = None  # type: ignore[assignment]

try:  # 链接正文提取用，缺失时退化成正则清洗
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore[assignment]

try:
    import trafilatura  # type: ignore
except Exception:  # pragma: no cover
    trafilatura = None  # type: ignore[assignment]

from maibot_sdk import Command, Field, HomeCard, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType


LOGGER = logging.getLogger(__name__)


def available_extractors() -> list[str]:
    """当前环境里可用的「链接正文提取器」。

    ``trafilatura`` / ``bs4`` / ``readability`` 都是**可选增强**，不在宿主依赖基线里，
    manifest 也不声明它们（避免装不上时把插件一起卡住）。三个都缺时仍然能跑：
    提取会退化成正则清洗 + ``og:description``，Steam 公告走 RSS 不受影响。
    """

    names: list[str] = []
    if trafilatura is not None:
        names.append("trafilatura")
    if BeautifulSoup is not None:
        names.append("bs4")
    try:
        if importlib.util.find_spec("readability") is not None:
            names.append("readability")
    except (ImportError, ValueError):  # pragma: no cover - 环境异常时忽略
        pass
    return names

PLUGIN_ID = "polarbear.twitter-forwarder"
USER_AGENT = "MaiBot-TwitterForwarder/1.0 (+https://github.com/MaiM-with-u/maibot)"

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
CANDIDATE_RE = re.compile(r"^[A-Za-z0-9_]{4,15}$")
HANDLE_MAX_LENGTH = 15
TWEET_ID_RE = re.compile(r"/status(?:es)?/(\d+)")
ATOM_NS = "{http://www.w3.org/2005/Atom}"
RESERVED_PATH_SEGMENTS = {"i", "web", "home", "intent", "search", "hashtag", "explore", "status", "statuses", "compose"}
SUPPORTED_HOSTS = (
    "x.com",
    "twitter.com",
    "mobile.twitter.com",
    "fxtwitter.com",
    "fixupx.com",
    "vxtwitter.com",
    "nitter.net",
)

MAX_SEEN_IDS = 300
MAX_HANDLES_PER_MESSAGE = 5
MAX_NODES_PER_FORWARD = 20
MAX_TRANSLATION_CACHE = 300
MAX_LINK_CACHE = 120
STATE_VERSION = 1

# 目标语言描述 → 语言代码前缀，用于判断"这条推文本来就是目标语言"
LANG_TARGET_KEYS: dict[str, tuple[str, ...]] = {
    "zh": ("中文", "汉语", "简体", "繁體", "繁体", "chinese", "zh"),
    "en": ("英文", "英语", "english", "en"),
    "ja": ("日文", "日语", "日本語", "japanese", "ja"),
    "ko": ("韩文", "韩语", "한국", "korean", "ko"),
    "ru": ("俄文", "俄语", "russian", "ru"),
    "fr": ("法文", "法语", "french", "fr"),
    "de": ("德文", "德语", "german", "de"),
    "es": ("西班牙", "spanish", "es"),
}

# 斜杠命令的公共前缀：同时接受半角 "/" 和全角 "／"，并容忍开头的 @机器人
_LEAD = r"^\s*(?:@\S+\s+)?[/／]"


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class PollSection(PluginConfigBase):
    """轮询相关配置。"""

    __ui_label__ = "轮询"
    __ui_icon__ = "refresh-cw"
    __ui_order__ = 1

    interval_minutes: int = Field(default=10, description="轮询间隔（分钟），聊天里可用 /tw_interval 临时覆盖")
    initial_delay_seconds: int = Field(default=30, description="插件加载后首次轮询的延迟（秒）")
    request_timeout_seconds: int = Field(default=20, description="单次 HTTP 请求超时（秒）")
    retry_times: int = Field(default=2, description="单次请求失败后的重试次数")
    fetch_count: int = Field(default=20, description="每个推主每轮拉取的条目数（1-50）")
    max_tweets_per_poll: int = Field(default=3, description="每个推主每轮最多推送几条新推文")
    max_concurrency: int = Field(default=3, description="同时轮询的推主数量上限")


class TwitterSection(PluginConfigBase):
    """数据源与网络配置。"""

    __ui_label__ = "数据源"
    __ui_icon__ = "globe"
    __ui_order__ = 2

    api_base: str = Field(default="https://api.fxtwitter.com", description="FxTwitter API 地址，一般不用改")
    feed_base: str = Field(default="https://fxtwitter.com", description="FxTwitter 站点地址，用于 Atom 兜底")
    enable_feed_fallback: bool = Field(default=True, description="v2 接口失败时回退到 Atom feed 拉取")
    proxy: str = Field(
        default="http://127.0.0.1:7890",
        description="下载图片用的 HTTP 代理；pbs.twimg.com 国内直连不通，留空表示不用代理",
    )
    use_proxy_for_api: bool = Field(default=False, description="调用 fxtwitter 接口时也走代理（一般不需要）")
    user_agent: str = Field(default=USER_AGENT, description="请求使用的 User-Agent")


class PushSection(PluginConfigBase):
    """推送行为配置。"""

    __ui_label__ = "推送"
    __ui_icon__ = "send"
    __ui_order__ = 3

    extra_streams: list[str] = Field(
        default_factory=list,
        description="额外固定推送目标：填写聊天流 session_id，所有订阅都会同步推送到这些流",
    )
    include_reposts: bool = Field(default=True, description="是否推送转推（博主转发别人的推文）")
    include_replies: bool = Field(default=False, description="是否推送回复（博主回复别人）")
    push_latest_on_subscribe: bool = Field(default=True, description="新增订阅时立刻把该推主最新一条推过来")
    skip_sensitive: bool = Field(
        default=True,
        description="是否跳过被接口标记为「可能敏感」的推文（默认跳过，避免群里出现不宜内容）",
    )


class MediaSection(PluginConfigBase):
    """媒体下载配置。"""

    __ui_label__ = "媒体"
    __ui_icon__ = "image"
    __ui_order__ = 4

    download_images: bool = Field(default=True, description="是否下载并发送推文配图")
    max_images: int = Field(default=4, description="单条推文最多发送几张图")
    image_quality: str = Field(default="medium", description="图片尺寸：orig / large / medium / small")
    max_image_mb: float = Field(default=3.0, description="单张图片大小上限（MB），超限则跳过")
    max_total_mb: float = Field(default=6.0, description="单条推文图片总大小上限（MB），避免一次性发太多图")
    media_timeout_seconds: int = Field(default=30, description="单张图片下载超时（秒）")
    video_mode: str = Field(
        default="auto",
        description="视频处理：auto=尽量把原视频下载发过来；thumbnail=只发封面+链接；link=只发链接",
    )
    inline_video_mb: float = Field(
        default=10.0,
        description="以内联 base64 发送的视频大小上限（MB）。受插件 IPC 16MB 帧限制，请不要超过 11",
    )
    max_video_mb: float = Field(
        default=300.0,
        description="视频下载大小上限（MB），超过则回退为封面+链接",
    )
    video_timeout_seconds: int = Field(default=180, description="视频下载超时（秒）")
    big_video_docker_route: bool = Field(
        default=True,
        description="大于内联上限的视频：下载后用 docker 拷进 SnowLuma 容器，以容器内路径发送（需要当前用户能执行 docker）",
    )
    docker_container: str = Field(default="snowluma", description="大视频路由的目标容器名")
    video_keep_seconds: int = Field(
        default=180,
        description="发完之后容器内视频文件再保留多少秒才删（大视频上传慢，删太早会让 QQ 那边收不到；0=立刻删）",
    )


class DisplaySection(PluginConfigBase):
    """消息排版配置。"""

    __ui_label__ = "排版"
    __ui_icon__ = "layout"
    __ui_order__ = 5

    use_forward: bool = Field(default=True, description="true=图文打包成一条合并转发；false=先发文字再逐张发图")
    batch_forward: bool = Field(
        default=True,
        description="一次有多个新推文时，合并成一条聊天记录（合并转发），每条推文占其中一个节点",
    )
    batch_max_mb: float = Field(
        default=7.0,
        description="单条聊天记录里图片的总大小上限（MB），超过会自动拆成多条，避免超出插件 IPC 帧限制",
    )
    video_in_forward: bool = Field(
        default=True,
        description="一次有多条推文时，把视频也放进它自己的聊天记录节点里（关掉则视频单独成条发）",
    )
    forward_nickname: str = Field(default="推特转发", description="合并转发节点里显示的昵称")
    show_author: bool = Field(default=True, description="是否显示作者与时间")
    header_divider: str = Field(
        default="────────────────",
        description="作者行下面的分隔线；留空表示不画",
    )
    hide_expanded_url: bool = Field(
        default=True,
        description="已经抓出正文的链接，从推文正文里去掉，避免和下面的链接内容重复",
    )
    show_link: bool = Field(default=True, description="是否附带原推链接")
    show_stats: bool = Field(default=True, description="是否显示点赞/转发/浏览数据")
    max_text_chars: int = Field(default=600, description="正文最大字符数，0 表示不截断")


class TranslationSection(PluginConfigBase):
    """自动翻译配置。"""

    __ui_label__ = "翻译"
    __ui_icon__ = "languages"
    __ui_order__ = 6

    enabled: bool = Field(default=True, description="是否自动翻译推文正文")
    model_task: str = Field(
        default="replyer",
        description="用哪个模型任务做翻译（默认 replyer=回复模型；也可填 utils / planner 等）",
    )
    target_lang: str = Field(default="简体中文", description="翻译目标语言")
    translate_quote: bool = Field(default=True, description="是否一并翻译引用的推文")
    skip_if_target_lang: bool = Field(
        default=True,
        description="推文本来就是目标语言时跳过翻译（按接口给的 lang 判断，拿不到时按汉字比例判断）",
    )
    max_chars: int = Field(default=1200, description="超过这个长度的推文不翻译（省 token），0 表示不限制")
    max_concurrency: int = Field(default=3, description="同时翻译的条数上限")
    timeout_seconds: int = Field(default=45, description="单条翻译超时（秒）")
    prompt: str = Field(
        default=(
            "你是专业的推文翻译助手。把用户给出的推文翻译成{target_lang}。\n"
            "要求：\n"
            "1. 只输出译文，不要输出原文、不要解释、不要加任何前缀或标记；\n"
            "2. 保留原文的换行、语气和 emoji；\n"
            "3. 人名、产品名、账号名、话题标签保持原文；\n"
            "4. 如果原文已经是{target_lang}，原样输出。"
        ),
        description="翻译提示词，{target_lang} 会被替换成目标语言",
    )


class LinkSection(PluginConfigBase):
    """推文里链接的内容抓取。"""

    __ui_label__ = "链接内容"
    __ui_icon__ = "link"
    __ui_order__ = 7

    enabled: bool = Field(default=True, description="是否把推文里链接的正文也抓出来一起发")
    max_links: int = Field(default=1, description="每条推文最多展开几个链接")
    max_chars: int = Field(default=3000, description="抓到的正文最多保留多少字（0 表示不限制）")
    translate_chunk_chars: int = Field(
        default=1200,
        description="长正文分段翻译时每段的字符数，太小会丢上下文，太大可能撞上模型输出上限",
    )
    timeout_seconds: int = Field(default=15, description="单次抓取超时（秒）；直连失败会用代理再试一次")
    max_page_mb: float = Field(default=2.0, description="页面下载大小上限（MB）")
    proxy: str = Field(default="", description="抓取用的 HTTP 代理；留空表示直连（失败会自动改用 twitter.proxy 重试）")
    fallback_proxy: bool = Field(default=True, description="直连抓不到时，用 [twitter] 的代理再试一次")
    allow_private_hosts: bool = Field(
        default=False,
        description=(
            "是否允许抓取指向本机/内网的链接（SSRF 防护开关）。默认关闭："
            "推文里的外链如果解析到 127.0.0.1、10.x、192.168.x、169.254.x 等地址会被直接丢弃"
        ),
    )
    translate: bool = Field(default=True, description="抓到的正文是否也翻译（复用 [translation] 的目标语言）")
    link_language: str = Field(
        default="schinese",
        description="Steam 新闻这类支持语言的站点优先取哪种语言（schinese/english/japanese/koreana…）",
    )


class CommandSection(PluginConfigBase):
    """聊天命令权限配置。"""

    __ui_label__ = "命令"
    __ui_icon__ = "terminal"
    __ui_order__ = 6

    admin_only: bool = Field(default=False, description="true 时所有命令都只有管理员能使用")
    cross_chat_admin_only: bool = Field(
        default=True,
        description=(
            "跨聊天命令是否只允许管理员/本地操作员："
            "/tw_all、/tw_del、/tw_reset、/tw_check、/tw_interval。默认开启"
        ),
    )
    admins: list[str] = Field(
        default_factory=list,
        description='管理员 QQ 号列表，例如 ["123456789"]；机器人的本地操作员始终放行',
    )


class TwitterForwarderConfig(PluginConfigBase):
    """推特转发插件配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    poll: PollSection = Field(default_factory=PollSection)
    twitter: TwitterSection = Field(default_factory=TwitterSection)
    push: PushSection = Field(default_factory=PushSection)
    media: MediaSection = Field(default_factory=MediaSection)
    display: DisplaySection = Field(default_factory=DisplaySection)
    translation: TranslationSection = Field(default_factory=TranslationSection)
    link: LinkSection = Field(default_factory=LinkSection)
    command: CommandSection = Field(default_factory=CommandSection)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class TweetMedia:
    """推文里的一段媒体。"""

    kind: str  # photo / video / gif
    url: str  # 主资源地址（视频为 mp4）
    thumbnail_url: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0  # 秒
    formats: list = dataclass_field(default_factory=list)  # [(url, bitrate), ...]，码率按 bps

    def best_variant_candidates(self) -> list[tuple[str, int]]:
        """按码率从高到低返回 (url, bitrate) 候选列表，去重。"""

        candidates: list[tuple[str, int]] = []
        seen: set[str] = set()
        if self.url:
            candidates.append((self.url, 0))
            seen.add(self.url)
        for url, bitrate in sorted(self.formats or [], key=lambda item: -int(item[1] or 0)):
            if url and url not in seen:
                candidates.append((url, int(bitrate or 0)))
                seen.add(url)
        return candidates


@dataclass
class Tweet:
    """归一化后的推文。"""

    id: str
    url: str
    text: str
    created_ts: float
    author_name: str
    author_screen_name: str
    is_repost: bool = False
    reposted_by_name: str = ""
    reposted_by_screen_name: str = ""
    is_reply: bool = False
    sensitive: bool = False
    lang: str = ""
    quote_author: str = ""
    quote_text: str = ""
    translation: str = ""
    quote_translation: str = ""
    link_preview: str = ""
    expanded_links: list[str] = dataclass_field(default_factory=list)
    media: list[TweetMedia] = dataclass_field(default_factory=list)
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    views: int = 0
    source: str = "api"

    @property
    def has_video(self) -> bool:
        """是否包含视频。"""

        return any(item.kind in {"video", "gif"} for item in self.media)

    def video_media(self) -> list[TweetMedia]:
        """返回推文里的视频媒体（video/gif）。"""

        return [item for item in self.media if item.kind in {"video", "gif"}]

    def image_sources(self) -> list[str]:
        """返回可下载为图片的候选地址列表。"""

        urls: list[str] = []
        for item in self.media:
            if item.kind == "photo":
                urls.append(item.url)
            elif item.thumbnail_url:
                urls.append(item.thumbnail_url)
        return [url for url in urls if url]


@dataclass
class VideoPayload:
    """已经准备好、可以塞进消息里的视频段内容。"""

    data: dict[str, Any]  # {"binary_data_base64": ...} 或 {"file": "/app/data/twvideo/x.mp4"}
    size: int = 0  # 计入批次预算的内联字节数（容器路径引用算 0）
    url: str = ""
    container: str = ""
    container_path: str = ""
    host_path: Optional[Path] = None

    @property
    def is_inline(self) -> bool:
        """是否走内联 base64。"""

        return bool(self.data.get("binary_data_base64"))


@dataclass
class PollResult:
    """单个推主的轮询结果。"""

    handle: str
    pushed: int = 0
    fetched: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        """本次轮询是否成功。"""

        return not self.error


class FxTwitterError(RuntimeError):
    """FxTwitter 调用失败。"""


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def normalize_handle(raw: str) -> Optional[str]:
    """把用户输入归一化为小写的推主 handle。

    支持 ``@name``、``name``、``https://x.com/name``、``x.com/name/status/123`` 等形式。

    Args:
        raw: 用户原始输入。

    Returns:
        Optional[str]: 归一化后的 handle；无法识别时返回 ``None``。
    """

    text = (raw or "").strip().replace("＠", "@")
    if not text:
        return None

    lowered = text.lower()
    if "/" in text or lowered.startswith(("http://", "https://", "www.")):
        candidate = text if "://" in text else f"https://{text}"
        parsed = urlparse(candidate)
        host = (parsed.netloc or "").lower().split(":")[0].removeprefix("www.")
        known_host = host in SUPPORTED_HOSTS or host.endswith(tuple(f".{item}" for item in SUPPORTED_HOSTS))
        if not known_host:
            return None
        for segment in (parsed.path or "").split("/"):
            normalized_segment = segment.strip()
            if not normalized_segment or normalized_segment.lower() in RESERVED_PATH_SEGMENTS:
                continue
            text = normalized_segment
            break
        else:
            return None

    text = text.lstrip("@").strip()
    text = text.split("/")[0].split("?")[0].strip()
    if not text or text.lower() in RESERVED_PATH_SEGMENTS:
        return None
    # 纯数字是 Twitter 的用户 ID，不是 handle
    if text.isdigit():
        return None
    if not HANDLE_RE.match(text):
        return None
    return text.lower()


def parse_handle_list(raw: str) -> list[str]:
    """把一段文本里的多个 handle 解析出来并去重。"""

    handles, _ = parse_handle_input(raw)
    return handles


def parse_handle_input(raw: str) -> tuple[list[str], list[str]]:
    """解析用户输入，返回可用 handle 与每一条无法识别输入的原因。

    Args:
        raw: 命令后面跟的原始参数文本。

    Returns:
        tuple[list[str], list[str]]: ``(可用 handle 列表, 问题说明列表)``。
    """

    handles: list[str] = []
    problems: list[str] = []
    for chunk in re.split(r"[\s,，、;；]+", raw or ""):
        token = chunk.strip()
        if not token:
            continue
        handle = normalize_handle(token)
        if handle:
            if handle not in handles:
                handles.append(handle)
        else:
            problems.append(describe_handle_problem(token))
    return handles, problems


def describe_handle_problem(raw: str) -> str:
    """说明某个输入为什么不能当作推主用户名，方便直接回给用户。"""

    text = (raw or "").strip().replace("＠", "@").strip()
    if not text:
        return "空输入"
    stripped = text.lstrip("@").strip()
    if not stripped:
        return f"{text}：只有 @ 没有用户名"

    lowered = stripped.lower()
    if "/" in stripped or lowered.startswith(("http://", "https://", "www.")):
        return f"{text}：这不是 x.com / twitter.com 的推主链接"

    if stripped.isdigit():
        return f"{text}：纯数字是 X 的用户 ID，不是用户名"
    if lowered in RESERVED_PATH_SEGMENTS:
        return f"{text}：这是 X 的保留路径（如 /home、/i），不是用户名"
    if len(stripped) > HANDLE_MAX_LENGTH:
        return f"{text}：共 {len(stripped)} 个字符，超过 X 用户名的 {HANDLE_MAX_LENGTH} 字符上限"
    invalid_chars = sorted({char for char in stripped if not re.match(r"[A-Za-z0-9_]", char)})
    if invalid_chars:
        shown = " ".join(invalid_chars[:5])
        return f"{text}：含有用户名不能使用的字符「{shown}」"
    return f"{text}：无法识别的用户名"


def handle_repair_candidates(raw: str) -> list[str]:
    """给超长/多打字的输入猜几个候选用户名（逐个删掉一个下划线等）。

    只在解析失败时用来做"你是不是想订阅 @xxx"的提示，候选还要再经过接口校验。
    """

    base = (raw or "").strip().lstrip("@").strip().lower()
    if not base:
        return []

    candidates: list[str] = []
    for index, char in enumerate(base):
        if char != "_":
            continue
        candidate = base[:index] + base[index + 1 :]
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for candidate in (base.replace("_", ""), base.strip("_")):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    # 多打了首尾字符的情况
    if len(base) > HANDLE_MAX_LENGTH:
        for candidate in (base[1:], base[:-1]):
            if candidate and candidate not in candidates:
                candidates.append(candidate)

    return [candidate for candidate in candidates if CANDIDATE_RE.match(candidate)]


def format_count(value: int) -> str:
    """把数字格式化成中文习惯的紧凑写法。"""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return "0"
    if number >= 100_000_000:
        return f"{number / 100_000_000:.1f}亿".replace(".0亿", "亿")
    if number >= 10_000:
        return f"{number / 10_000:.1f}万".replace(".0万", "万")
    return str(number)


def format_time(timestamp: float) -> str:
    """把时间戳格式化为本地时间字符串。"""

    if not timestamp:
        return "未知"
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "未知"


def format_ago(timestamp: float) -> str:
    """把时间戳格式化为"多久之前"。"""

    if not timestamp:
        return "从未"
    delta = max(0.0, time.time() - float(timestamp))
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{delta / 3600:.1f} 小时前"
    return f"{delta / 86400:.1f} 天前"


def truncate_text(text: str, limit: int) -> str:
    """按字符数截断文本。"""

    normalized = (text or "").strip()
    if limit <= 0 or len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 1)].rstrip() + "…"


def truncate_at_paragraph(text: str, limit: int) -> str:
    """截断长文：尽量切在段落/换行处，避免半句话被砍掉。"""

    normalized = (text or "").strip()
    if limit <= 0 or len(normalized) <= limit:
        return normalized
    window = normalized[:limit]
    cut = window.rfind("\n")
    if cut >= int(limit * 0.6):
        return window[:cut].rstrip() + "\n…（内容较长，已截断）"
    return window.rstrip() + "…（内容较长，已截断）"


def split_text_chunks(text: str, limit: int) -> list[str]:
    """按段落把长文切成若干块（单段超长时再按句子/空格切），供分段翻译用。"""

    normalized = (text or "").strip()
    if not normalized:
        return []
    if limit <= 0 or len(normalized) <= limit:
        return [normalized]

    chunks: list[str] = []
    current = ""
    for paragraph in re.split(r"\n+", normalized):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(current) + len(paragraph) + 1 <= limit:
            current = f"{current}\n{paragraph}" if current else paragraph
            continue
        if current:
            chunks.append(current)
            current = ""
        # 单段本身就超长 → 继续按句子/空格切
        while len(paragraph) > limit:
            window = paragraph[:limit]
            cut = max(window.rfind("。"), window.rfind("！"), window.rfind("？"), window.rfind(". "), window.rfind(" "))
            if cut < int(limit * 0.4):
                cut = limit
            chunks.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        current = paragraph
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def protect_invalid_tags(raw_html: str) -> str:
    """把 ``<Part 1>`` 这类"不是合法标签"的尖括号内容转义成文本。

    RSS / 网页里的正文本身可能带尖括号（例如官方的 ``<Part 1>``），
    直接交给 HTML 解析器会被当成未知标签丢掉，导致译文里出现断句。
    这里只放行已知标签（以及 ``my-widget`` 这种带连字符的自定义元素），
    其余尖括号内容一律转义成文本。
    """

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner or inner.startswith(("/", "!", "?")):
            return match.group(0)
        name = re.split(r"[\s/>]", inner, maxsplit=1)[0]
        if not name or name.lower() in KNOWN_HTML_TAGS or "-" in name:
            return match.group(0)
        return f"&lt;{inner}&gt;"

    return re.sub(r"<([^<>]{1,80})>", replace, raw_html or "")


def strip_html(raw: str) -> str:
    """把 Atom feed 里的 HTML 片段转成纯文本。"""

    text = protect_invalid_tags(raw or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)<blockquote[^>]*>", "\n> ", text)
    text = re.sub(r"(?i)</blockquote\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def resize_twimg_url(url: str, quality: str) -> str:
    """把 pbs.twimg.com 的图片地址改写成指定尺寸。"""

    normalized = (url or "").strip()
    if not normalized or "pbs.twimg.com" not in normalized:
        return normalized
    if quality not in {"orig", "large", "medium", "small"}:
        return normalized
    base = normalized.split("?", maxsplit=1)[0]
    return f"{base}?name={quality}"


def parse_tweet_id(url: str) -> str:
    """从推文链接里取出推文 ID。"""

    match = TWEET_ID_RE.search(url or "")
    return match.group(1) if match else ""


def match_target_lang_code(target_lang: str) -> str:
    """把目标语言描述映射成语言代码前缀（zh / en / ja …），识别不了返回空串。"""

    target = (target_lang or "").strip().lower()
    if not target:
        return ""
    for code, keywords in LANG_TARGET_KEYS.items():
        if any(keyword in target for keyword in keywords):
            return code
    return ""


def looks_like_language(text: str, code: str) -> bool:
    """粗略判断文本是否已经是某种语言（接口没给 lang 时的兜底）。"""

    if not text or not code:
        return False
    if code == "zh":
        cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        return cjk >= 8 and cjk / max(1, len(text)) >= 0.3
    if code == "ko":
        return sum(1 for char in text if "\uac00" <= char <= "\ud7af") >= 8
    if code == "ja":
        return sum(1 for char in text if "\u3040" <= char <= "\u30ff") >= 8
    return False


def clean_translation(text: str) -> str:
    """清理模型输出里可能带的代码块、包裹引号和「译文：」前缀。"""

    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'", "“", "「", "『"}:
        cleaned = cleaned[1:-1].strip()
    cleaned = re.sub(r"^(译文|翻译|Translation)\s*[:：]\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


# --- 链接内容抓取 ---------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s<>\"'）)】\]]+", re.IGNORECASE)
STEAM_NEWS_RE = re.compile(r"^https?://store\.steampowered\.com/news/app/(\d+)/view/(\d+)", re.IGNORECASE)
SKIP_LINK_HOSTS = (
    "x.com",
    "twitter.com",
    "t.co",
    "twimg.com",
    "fxtwitter.com",
    "fixupx.com",
    "vxtwitter.com",
    "nitter.net",
    "t.me",
)
SKIP_LINK_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".mp4",
    ".mov",
    ".webm",
    ".mp3",
    ".m4a",
    ".zip",
    ".rar",
    ".7z",
    ".exe",
    ".apk",
)

# HTML 解析时放行的标签名；不在名单里又没有连字符的尖括号内容会被当成普通文本
KNOWN_HTML_TAGS = frozenset(
    {
        "a", "abbr", "address", "article", "aside", "audio", "b", "bdi", "bdo", "blockquote", "body", "br",
        "button", "caption", "center", "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn", "div",
        "dl", "dt", "em", "embed", "fieldset", "figcaption", "figure", "font", "footer", "form", "h1", "h2",
        "h3", "h4", "h5", "h6", "head", "header", "hr", "html", "i", "iframe", "img", "input", "ins", "kbd",
        "label", "legend", "li", "link", "main", "mark", "meta", "nav", "noscript", "ol", "option", "p", "picture",
        "pre", "q", "s", "samp", "script", "section", "select", "small", "source", "span", "strike", "strong",
        "style", "sub", "summary", "sup", "table", "tbody", "td", "textarea", "tfoot", "th", "thead", "time",
        "title", "tr", "track", "u", "ul", "var", "video", "wbr",
    }
)


def extract_links(text: str) -> list[str]:
    """从推文正文里挑出值得展开的链接（去掉推文自身、媒体和图片直链）。"""

    links: list[str] = []
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?、。，；：！？")
        host = (urlparse(url).netloc or "").lower().split(":")[0]
        if not host:
            continue
        normalized_host = host.removeprefix("www.")
        if any(normalized_host == item or normalized_host.endswith(f".{item}") for item in SKIP_LINK_HOSTS):
            continue
        if url.lower().split("?")[0].endswith(SKIP_LINK_SUFFIXES):
            continue
        if url not in links:
            links.append(url)
    return links


def html_to_text(raw_html: str) -> str:
    """把 HTML 片段转成纯文本（优先 bs4，缺失时退回正则清洗）。"""

    text = ""
    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(protect_invalid_tags(raw_html), "lxml")
            for tag in soup(["script", "style", "noscript", "iframe"]):
                tag.decompose()
            text = soup.get_text("\n", strip=True)
        except Exception:
            text = ""
    if not text:
        text = strip_html(raw_html)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_page_title(raw_html: str) -> str:
    """取页面标题：优先 og:title，其次 <title>。"""

    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\'](.*?)["\']',
        r"<title[^>]*>(.*?)</title>",
    )
    for pattern in patterns:
        match = re.search(pattern, raw_html or "", re.S | re.I)
        if match:
            title = html_module.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
            if title:
                return title
    return ""


def extract_article_text(raw_html: str) -> str:
    """从网页里抽正文：trafilatura → readability → 元描述，逐级兜底并过滤导航垃圾。"""

    # trafilatura 在解析失败时会往 WARNING 刷日志，提取期间临时压掉
    noisy_loggers = [logging.getLogger("trafilatura"), logging.getLogger("justext")]
    previous_levels = [(item, item.level) for item in noisy_loggers]
    try:
        for item in noisy_loggers:
            item.setLevel(logging.ERROR)

        if trafilatura is not None:
            try:
                extracted = trafilatura.extract(
                    raw_html,
                    include_comments=False,
                    include_tables=False,
                    favor_precision=True,
                )
                if extracted and len(extracted.strip()) >= 80 and not looks_like_boilerplate(extracted):
                    return re.sub(r"\n{3,}", "\n\n", extracted.strip())
            except Exception:
                pass

        try:
            from readability import Document  # type: ignore

            summary = Document(raw_html).summary()
            text = html_to_text(summary)
            if len(text) >= 80 and not looks_like_boilerplate(text):
                return text
        except Exception:
            pass
    finally:
        for item, level in previous_levels:
            item.setLevel(level)

    # 全都失败时，宁可给一句元描述，也不要一屏导航栏
    description = extract_meta_description(raw_html)
    if description:
        return description
    fallback = html_to_text(raw_html)
    return "" if looks_like_boilerplate(fallback) else fallback


def extract_meta_description(raw_html: str) -> str:
    """取页面描述：og:description / name=description。"""

    patterns = (
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\'](.*?)["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, raw_html or "", re.S | re.I)
        if match:
            description = html_module.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
            if len(description) >= 20:
                return description
    return ""


BOILERPLATE_MARKERS = (
    "隐私政策",
    "订户协议",
    "退款",
    "无障碍",
    "法律信息",
    "版权所有",
    "所有商标",
    "Privacy Policy",
    "Terms of Service",
    "All rights reserved",
    "Cookie",
    "Sign in",
    "登录",
    "注册",
)


def looks_like_boilerplate(text: str) -> bool:
    """粗判一段文本是不是"网页导航/菜单/页脚垃圾"而不是正文。"""

    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return True
    markers = sum(1 for marker in BOILERPLATE_MARKERS if marker in text)
    if markers >= 3:
        return True
    short_lines = sum(1 for line in lines if len(line) <= 12)
    if len(lines) >= 12 and short_lines / len(lines) >= 0.6:
        return True
    # 整页都是菜单项的特征：行数不少，但一行像正文的长句都没有
    lengths = sorted(len(line) for line in lines)
    median = lengths[len(lengths) // 2]
    return len(lines) >= 10 and median <= 25 and lengths[-1] <= 140


def extract_media_links(raw_html: str) -> list[str]:
    """从 HTML 里找出 YouTube 之类的视频链接（公告正文常常只有一个视频嵌入）。"""

    links: list[str] = []
    for video_id in re.findall(
        r"(?:youtube\.com/embed/|youtube-nocookie\.com/embed/|youtu\.be/|data-youtube=[\"'])([\w-]{6,})",
        raw_html or "",
    ):
        link = f"https://www.youtube.com/watch?v={video_id}"
        if link not in links:
            links.append(link)
    return links


def parse_atom_time(value: str) -> float:
    """解析 Atom 的时间字符串。"""

    text = (value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# FxTwitter 客户端
# ---------------------------------------------------------------------------


class FxTwitterClient:
    """极简 FxTwitter / FxEmbed 客户端。

    单个账号出错（私密号、已注销、限流）只影响这一次请求，不维护全局可用性标志，
    避免一个坏账号把整个轮询拖死。
    """

    def __init__(
        self,
        *,
        api_base: str,
        feed_base: str,
        proxy: str,
        use_proxy_for_api: bool,
        timeout: float,
        retry_times: int,
        user_agent: str,
        feed_fallback: bool,
    ) -> None:
        self.api_base = (api_base or "https://api.fxtwitter.com").rstrip("/")
        self.feed_base = (feed_base or "https://fxtwitter.com").rstrip("/")
        self.proxy = (proxy or "").strip() or None
        self.use_proxy_for_api = bool(use_proxy_for_api)
        self.timeout = max(5.0, float(timeout))
        self.retry_times = max(1, int(retry_times))
        self.user_agent = user_agent or USER_AGENT
        self.feed_fallback = bool(feed_fallback)
        self._session: Optional["aiohttp.ClientSession"] = None

    async def _ensure_session(self) -> "aiohttp.ClientSession":
        """惰性创建 aiohttp 会话。"""

        if aiohttp is None:
            raise FxTwitterError("运行环境缺少 aiohttp，无法发起网络请求")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json, text/xml, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                }
            )
        return self._session

    async def close(self) -> None:
        """关闭会话。"""

        if self._session is not None and not self._session.closed:
            with contextlib.suppress(Exception):
                await self._session.close()
        self._session = None

    async def _request_text(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        use_proxy: bool = False,
        timeout: Optional[float] = None,
    ) -> str:
        """带重试地发起 GET 请求并返回文本。"""

        session = await self._ensure_session()
        proxy = self.proxy if use_proxy else None
        request_timeout = aiohttp.ClientTimeout(total=float(timeout or self.timeout))
        last_error: Optional[Exception] = None

        for attempt in range(self.retry_times):
            try:
                async with session.get(url, params=params, proxy=proxy, timeout=request_timeout) as response:
                    body = await response.text()
                    if response.status == 404:
                        raise FxTwitterError("账号或推文不存在（HTTP 404）")
                    if response.status == 429:
                        raise FxTwitterError("触发 fxtwitter 限流（HTTP 429）")
                    if response.status >= 500:
                        raise FxTwitterError(f"fxtwitter 服务端错误（HTTP {response.status}）")
                    if response.status >= 400:
                        raise FxTwitterError(f"请求失败（HTTP {response.status}）")
                    return body
            except FxTwitterError as exc:
                last_error = exc
                # 404 / 限流这类确定性错误不重试，避免浪费请求
                if "404" in str(exc):
                    raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc
            if attempt + 1 < self.retry_times:
                await asyncio.sleep(1.2 * (attempt + 1))

        raise FxTwitterError(f"请求失败：{last_error}")

    async def _get_json(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        use_proxy: bool = False,
    ) -> dict[str, Any]:
        """请求 JSON 接口并校验业务状态码。"""

        url = f"{self.api_base}/{path.lstrip('/')}"
        body = await self._request_text(url, params=params, use_proxy=use_proxy)
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise FxTwitterError("接口返回的不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise FxTwitterError("接口返回格式异常")
        code = payload.get("code")
        if code is not None:
            try:
                if int(code) != 200:
                    raise FxTwitterError(f"接口返回 code={code}")
            except (TypeError, ValueError) as exc:
                raise FxTwitterError(f"接口返回 code={code}") from exc
        return payload

    async def get_profile(self, handle: str) -> dict[str, Any]:
        """获取推主资料。"""

        payload = await self._get_json(handle, use_proxy=self.use_proxy_for_api)
        user = payload.get("user")
        if not isinstance(user, dict):
            raise FxTwitterError("没有找到这个推主")
        return user

    async def fetch_timeline(self, handle: str, count: int) -> list[Tweet]:
        """获取推主最新时间线：优先 v2 接口，失败时回退 Atom feed。"""

        errors: list[str] = []
        try:
            tweets = await self._fetch_timeline_v2(handle, count)
            if tweets:
                return tweets
            errors.append("v2 接口返回空时间线")
        except FxTwitterError as exc:
            errors.append(f"v2 接口：{exc}")

        if self.feed_fallback:
            try:
                tweets = await self._fetch_timeline_feed(handle, count)
                if tweets:
                    return tweets
                errors.append("Atom feed 返回空时间线")
            except FxTwitterError as exc:
                errors.append(f"Atom feed：{exc}")

        raise FxTwitterError("；".join(errors) or "拉取时间线失败")

    async def _fetch_timeline_v2(self, handle: str, count: int) -> list[Tweet]:
        """通过 FxEmbed v2 接口拉取时间线。"""

        payload = await self._get_json(
            f"2/profile/{handle}/statuses",
            params={"count": max(1, min(50, int(count)))},
            use_proxy=self.use_proxy_for_api,
        )
        results = payload.get("results")
        if not isinstance(results, list):
            raise FxTwitterError("时间线返回格式异常")
        tweets = [tweet for tweet in (tweet_from_api(item) for item in results) if tweet is not None]
        tweets.sort(key=lambda item: item.created_ts, reverse=True)
        return tweets

    async def _fetch_timeline_feed(self, handle: str, count: int) -> list[Tweet]:
        """通过 Atom feed 兜底拉取时间线。"""

        url = f"{self.feed_base}/{handle}/feed.atom.xml"
        body = await self._request_text(
            url,
            params={"count": max(1, min(50, int(count)))},
            use_proxy=self.use_proxy_for_api,
        )
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise FxTwitterError("Atom feed 解析失败") from exc

        tweets: list[Tweet] = []
        for entry in root.findall(f"{ATOM_NS}entry"):
            tweet = tweet_from_atom(entry, handle)
            if tweet is not None:
                tweets.append(tweet)
        tweets.sort(key=lambda item: item.created_ts, reverse=True)
        return tweets

    async def head_size(self, url: str, *, timeout: float = 20.0, use_proxy: bool = True) -> Optional[int]:
        """HEAD 请求取 Content-Length（单位：字节）；失败返回 ``None``。"""

        if aiohttp is None or not url:
            return None
        session = await self._ensure_session()
        request_timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout)))
        try:
            async with session.head(
                url,
                proxy=self.proxy if use_proxy else None,
                timeout=request_timeout,
                allow_redirects=True,
            ) as response:
                if response.status != 200:
                    return None
                raw = str(response.headers.get("Content-Length") or "").strip()
                if not raw.isdigit():
                    return None
                return int(raw)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return None

    async def download_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout: float,
        expect: tuple[str, ...] = (),
    ) -> Optional[bytes]:
        """下载二进制内容并返回字节；失败、超限或类型不对返回 ``None``。

        Args:
            url: 下载地址。
            max_bytes: 大小上限（字节）。
            timeout: 超时（秒）。
            expect: 期望的 Content-Type 前缀；为空表示不校验类型。
        """

        if aiohttp is None or not url:
            return None
        session = await self._ensure_session()
        request_timeout = aiohttp.ClientTimeout(total=float(timeout))
        try:
            async with session.get(url, proxy=self.proxy, timeout=request_timeout) as response:
                if response.status != 200:
                    return None
                content_type = str(response.headers.get("Content-Type") or "")
                if content_type and expect and not content_type.startswith(expect):
                    return None
                declared_length = str(response.headers.get("Content-Length") or "").strip()
                if declared_length.isdigit() and int(declared_length) > max_bytes:
                    return None

                # 注意：aiohttp 的 content.read(n) 只保证"最多 n 字节"，
                # 数据没到齐就会提前返回，这里必须循环读完整个响应体。
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return None
        if not data or len(data) > max_bytes:
            return None
        return data

    async def download_to_file(
        self,
        url: str,
        dest: "Path",
        *,
        max_bytes: int,
        timeout: float,
    ) -> bool:
        """流式下载到本地文件；失败或超出大小上限时返回 ``False`` 并清理半截文件。"""

        if aiohttp is None or not url:
            return False
        dest = Path(dest)
        try:
            session = await self._ensure_session()
            request_timeout = aiohttp.ClientTimeout(total=float(timeout))
            async with session.get(url, proxy=self.proxy, timeout=request_timeout) as response:
                if response.status != 200:
                    return False
                declared_length = str(response.headers.get("Content-Length") or "").strip()
                if declared_length.isdigit() and int(declared_length) > max_bytes:
                    return False
                dest.parent.mkdir(parents=True, exist_ok=True)
                total = 0
                with open(dest, "wb") as file_obj:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            return False
                        file_obj.write(chunk)
                return total > 0
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False
        finally:
            if dest.exists():
                try:
                    if dest.stat().st_size > max_bytes or dest.stat().st_size == 0:
                        dest.unlink()
                except OSError:
                    pass

    async def fetch_text(
        self,
        url: str,
        *,
        use_proxy: bool = False,
        proxy: Optional[str] = None,
        timeout: float = 25.0,
        max_bytes: int = 2 * 1024 * 1024,
        validate: Optional[Any] = None,
        max_redirects: int = 5,
    ) -> str:
        """抓取网页文本（自动跟随跳转）；失败或类型不对返回空串。

        Args:
            proxy: 显式指定代理地址；``None`` 时按 ``use_proxy`` 决定是否用会话代理。
            validate: 可选的 ``async (url) -> (是否允许, 原因)`` 校验函数。
                传入时会**关闭自动跳转并逐跳校验**，用于抓取不可信的外链（防 SSRF）。
            max_redirects: 手动跟随跳转的最大次数。
        """

        if aiohttp is None or not url:
            return ""
        session = await self._ensure_session()
        resolved_proxy = proxy if proxy is not None else (self.proxy if use_proxy else None)
        request_timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout)))
        headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        target = str(url)

        try:
            for _ in range(max(1, max_redirects + 1)):
                if validate is not None:
                    allowed, reason = await validate(target)
                    if not allowed:
                        logger.warning("拒绝抓取不安全的链接（SSRF 防护）: %s → %s", target, reason)
                        return ""
                async with session.get(
                    target,
                    proxy=resolved_proxy,
                    timeout=request_timeout,
                    headers=headers,
                    allow_redirects=validate is None,
                ) as response:
                    if validate is not None and response.status in (301, 302, 303, 307, 308):
                        location = str(response.headers.get("Location") or "").strip()
                        if not location:
                            return ""
                        target = urljoin(target, location)
                        continue
                    if response.status != 200:
                        return ""
                    content_type = str(response.headers.get("Content-Type") or "").lower()
                    if content_type and not any(
                        marker in content_type for marker in ("text/html", "text/plain", "xml", "json")
                    ):
                        return ""
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            break
                        chunks.append(chunk)
                    payload = b"".join(chunks)
                break
            else:
                return ""
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return ""
        if not payload:
            return ""
        charset = "utf-8"
        if content_type:
            match = re.search(r"charset=([\w-]+)", content_type)
            if match:
                charset = match.group(1)
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")

    async def download_image(self, url: str, *, max_bytes: int, timeout: float) -> Optional[bytes]:
        """下载图片并返回字节内容；失败或超限返回 ``None``。"""

        return await self.download_bytes(
            url,
            max_bytes=max_bytes,
            timeout=timeout,
            expect=("image/",),
        )


def tweet_from_api(item: Any) -> Optional[Tweet]:
    """把 v2 接口返回的单条推文转成 :class:`Tweet`。"""

    if not isinstance(item, dict):
        return None
    tweet_id = str(item.get("id") or "").strip()
    if not tweet_id:
        return None

    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    reposted_by = item.get("reposted_by") if isinstance(item.get("reposted_by"), dict) else None
    quote = item.get("quote") if isinstance(item.get("quote"), dict) else None
    quote_author = ""
    quote_text = ""
    if quote:
        quote_author = str((quote.get("author") or {}).get("screen_name") or "")
        quote_text = str(quote.get("text") or "")

    created_ts = 0.0
    raw_ts = item.get("created_timestamp")
    if isinstance(raw_ts, (int, float)):
        created_ts = float(raw_ts)
    if not created_ts:
        created_ts = parse_twitter_time(str(item.get("created_at") or ""))

    media: list[TweetMedia] = []
    raw_media = item.get("media") if isinstance(item.get("media"), dict) else {}
    for entry in raw_media.get("all") or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "photo").strip().lower()
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        formats: list[tuple[str, int]] = []
        for variant in entry.get("formats") or []:
            if not isinstance(variant, dict):
                continue
            variant_url = str(variant.get("url") or "").strip()
            if not variant_url or str(variant.get("container") or "").strip().lower() != "mp4":
                continue
            try:
                bitrate = int(variant.get("bitrate") or 0)
            except (TypeError, ValueError):
                bitrate = 0
            formats.append((variant_url, bitrate))
        media.append(
            TweetMedia(
                kind=kind,
                url=url,
                thumbnail_url=str(entry.get("thumbnail_url") or ""),
                width=int(entry.get("width") or 0),
                height=int(entry.get("height") or 0),
                duration=float(entry.get("duration") or 0.0),
                formats=formats,
            )
        )

    return Tweet(
        id=tweet_id,
        url=str(item.get("url") or f"https://x.com/i/status/{tweet_id}"),
        text=str(item.get("text") or "").strip(),
        created_ts=created_ts,
        author_name=str(author.get("name") or author.get("screen_name") or ""),
        author_screen_name=str(author.get("screen_name") or ""),
        is_repost=bool(reposted_by),
        reposted_by_name=str((reposted_by or {}).get("name") or ""),
        reposted_by_screen_name=str((reposted_by or {}).get("screen_name") or ""),
        is_reply=bool(item.get("replying_to")),
        sensitive=bool(item.get("possibly_sensitive")),
        lang=str(item.get("lang") or "").strip().lower(),
        quote_author=quote_author,
        quote_text=quote_text,
        media=media,
        likes=_as_int(item.get("likes")),
        replies=_as_int(item.get("replies")),
        reposts=_as_int(item.get("reposts") if item.get("reposts") is not None else item.get("retweets")),
        views=_as_int(item.get("views")),
        source="api",
    )


def tweet_from_atom(entry: ET.Element, handle: str) -> Optional[Tweet]:
    """把 Atom feed 的 ``<entry>`` 转成 :class:`Tweet`。"""

    raw_id = (entry.findtext(f"{ATOM_NS}id") or "").strip()
    link_url = ""
    enclosure_url = ""
    enclosure_type = ""
    for link in entry.findall(f"{ATOM_NS}link"):
        rel = str(link.get("rel") or "alternate")
        href = str(link.get("href") or "").strip()
        if rel == "enclosure" and href:
            enclosure_url = href
            enclosure_type = str(link.get("type") or "")
        elif rel == "alternate" and href and not link_url:
            link_url = href

    tweet_url = link_url or raw_id
    tweet_id = parse_tweet_id(tweet_url) or parse_tweet_id(raw_id)
    if not tweet_id:
        return None

    content_text = strip_html(entry.findtext(f"{ATOM_NS}content") or "")
    title = (entry.findtext(f"{ATOM_NS}title") or "").strip()
    text = content_text or title

    created_ts = parse_atom_time(entry.findtext(f"{ATOM_NS}published") or "") or parse_atom_time(
        entry.findtext(f"{ATOM_NS}updated") or ""
    )

    media: list[TweetMedia] = []
    if enclosure_url:
        kind = "video" if enclosure_type.startswith("video/") else "photo"
        media.append(TweetMedia(kind=kind, url=enclosure_url, thumbnail_url=""))

    # feed 的 alternate 链接指向原推作者，与订阅的 handle 不一致时视为转推
    author_screen_name = ""
    parsed = urlparse(tweet_url)
    segments = [segment for segment in (parsed.path or "").split("/") if segment]
    if segments:
        author_screen_name = segments[0]
    is_repost = bool(author_screen_name) and author_screen_name.lower() != handle.lower()

    return Tweet(
        id=tweet_id,
        url=tweet_url,
        text=text,
        created_ts=created_ts,
        author_name=author_screen_name or handle,
        author_screen_name=author_screen_name or handle,
        is_repost=is_repost,
        reposted_by_name=handle if is_repost else "",
        reposted_by_screen_name=handle if is_repost else "",
        media=media,
        source="feed",
    )


def parse_twitter_time(value: str) -> float:
    """解析 Twitter 的 ``Fri Sep 11 17:53:28 +0000 2026`` 时间格式。"""

    text = (value or "").strip()
    if not text:
        return 0.0
    for pattern in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, pattern).timestamp()
        except ValueError:
            continue
    return 0.0


def _as_int(value: Any) -> int:
    """把接口返回的数字安全地转成 int。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --- docker 可执行文件查找 -------------------------------------------------

#: ``shutil.which`` 查不到时的兜底路径（snap 安装的 docker 常常不在服务的 PATH 里）。
DOCKER_FALLBACK_PATHS = (
    "/snap/bin/docker",
    "/usr/bin/docker",
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
)

_docker_path_cache: Optional[str] = None


def find_docker(*, refresh: bool = False) -> Optional[str]:
    """定位 docker 可执行文件，找不到返回 ``None``。

    systemd 服务里的 ``PATH`` 往往不含 ``/snap/bin``，导致 ``shutil.which("docker")``
    失败、大视频的容器借道被静默跳过。这里先查 PATH，再依次试常见安装位置。

    Args:
        refresh: 忽略缓存重新查找。
    """

    global _docker_path_cache
    if _docker_path_cache and not refresh and os.path.exists(_docker_path_cache):
        return _docker_path_cache

    found = shutil.which("docker")
    if not found:
        for candidate in DOCKER_FALLBACK_PATHS:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found = candidate
                break
    _docker_path_cache = found
    return found


# --- 出站 URL 安全（SSRF 防护） -------------------------------------------

#: 这些域名后缀一定指向本机 / 内网，直接拒绝
BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
    ".in-addr.arpa",
)

#: 允许抓取的协议
ALLOWED_URL_SCHEMES = ("http", "https")


def ip_is_blocked(ip: Any) -> bool:
    """判断一个 IP 是否属于本机 / 内网 / 保留地址（SSRF 防护用）。

    主判据是 ``not ip.is_global``：它一次覆盖 10/8、172.16/12、192.168/16、127/8、
    169.254/16（云 metadata）、0.0.0.0/8、100.64/10（CGNAT，Tailscale 也用这段）
    以及 IPv6 的回环/链路本地/唯一本地地址。后面几个显式判断只是兜住版本差异。
    """

    if not ip.is_global:
        return True
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    # ::ffff:127.0.0.1 这类 IPv4-mapped 地址要按 IPv4 再判一次
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return ip_is_blocked(mapped)
    return False


def host_is_blocked(host: str) -> bool:
    """按字面量判断主机名是否指向本机 / 内网（不做 DNS 解析）。

    ``localhost``、``*.local``、``*.internal`` 这类名字，以及 ``127.0.0.1``、
    ``10.0.0.1``、``169.254.169.254``（云 metadata）这类字面 IP 都会命中。
    """

    name = (host or "").strip().strip("[]").lower().rstrip(".")
    if not name:
        return True
    if name == "localhost" or name.endswith(BLOCKED_HOST_SUFFIXES):
        return True
    try:
        return ip_is_blocked(ipaddress.ip_address(name))
    except ValueError:
        return False


async def resolve_host_ips(host: str) -> list[str]:
    """解析主机名到 IP 列表（用于抓取前的 SSRF 校验）。"""

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def url_is_public(url: str) -> tuple[bool, str]:
    """判断 URL 是否可以安全抓取，返回 ``(是否允许, 不允许的原因)``。

    先看协议与字面主机名，再把域名解析成 IP 逐个检查：
    只要有一个落在内网/本机/保留段就拒绝（防止 DNS 指向内网，也顺带挡住
    ``http://2130706433/`` 这种十进制/八进制写法）。
    """

    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        return False, f"只允许 http/https，收到的是 {parsed.scheme or '(空)'}"
    host = parsed.hostname or ""
    if not host:
        return False, "URL 里没有主机名"
    if host_is_blocked(host):
        return False, f"{host} 指向本机或内网地址"
    try:
        addresses = await resolve_host_ips(host)
    except (OSError, asyncio.TimeoutError) as exc:
        return False, f"{host} 域名解析失败：{exc}"
    if not addresses:
        return False, f"{host} 没有解析到任何地址"
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip_is_blocked(ip):
            return False, f"{host} 解析到内网地址 {address}"
    return True, ""


# ---------------------------------------------------------------------------
# 状态存储
# ---------------------------------------------------------------------------


class StateStore:
    """订阅关系与运行状态的 JSON 持久化。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.handles: dict[str, dict[str, Any]] = {}
        self.streams: dict[str, dict[str, Any]] = {}
        self.runtime: dict[str, Any] = {}

    def load(self) -> None:
        """从磁盘读取状态，损坏时回退为空状态。"""

        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            LOGGER.warning("推特转发状态文件损坏，已按空状态启动: %s", self.path)
            return
        if not isinstance(payload, dict):
            return
        handles = payload.get("handles")
        streams = payload.get("streams")
        runtime = payload.get("runtime")
        self.handles = {str(key): value for key, value in handles.items() if isinstance(value, dict)} if isinstance(handles, dict) else {}
        self.streams = {str(key): value for key, value in streams.items() if isinstance(value, dict)} if isinstance(streams, dict) else {}
        self.runtime = dict(runtime) if isinstance(runtime, dict) else {}

    def save(self) -> None:
        """原子写入状态文件。"""

        payload = {
            "version": STATE_VERSION,
            "updated_at": time.time(),
            "handles": self.handles,
            "streams": self.streams,
            "runtime": self.runtime,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(self.path)
        except OSError as exc:
            LOGGER.error("推特转发状态写入失败: %s", exc)

    # -- 单条订阅 ---------------------------------------------------------

    @staticmethod
    def new_handle_entry(screen_name: str = "", display_name: str = "") -> dict[str, Any]:
        """构造一条新的订阅记录。"""

        return {
            "screen_name": screen_name,
            "display_name": display_name,
            "streams": [],
            "seen_ids": [],
            "last_seen_ts": 0.0,
            "last_check_at": 0.0,
            "last_new_at": 0.0,
            "last_error": "",
            "fail_count": 0,
            "added_at": time.time(),
            "total_pushed": 0,
        }

    def handle_entry(self, handle: str) -> dict[str, Any]:
        """取出（必要时创建）某个 handle 的订阅记录。"""

        entry = self.handles.get(handle)
        if not isinstance(entry, dict):
            entry = self.new_handle_entry(screen_name=handle)
            self.handles[handle] = entry
        entry.setdefault("streams", [])
        entry.setdefault("seen_ids", [])
        entry.setdefault("last_seen_ts", 0.0)
        entry.setdefault("last_check_at", 0.0)
        entry.setdefault("last_new_at", 0.0)
        entry.setdefault("last_error", "")
        entry.setdefault("fail_count", 0)
        entry.setdefault("total_pushed", 0)
        return entry

    def stream_entry(self, stream_id: str, label: str = "") -> dict[str, Any]:
        """取出（必要时创建）某个聊天流的记录。"""

        entry = self.streams.get(stream_id)
        if not isinstance(entry, dict):
            entry = {"label": label, "paused": False, "added_at": time.time()}
            self.streams[stream_id] = entry
        if label:
            entry["label"] = label
        entry.setdefault("paused", False)
        return entry

    def remove_stream(self, stream_id: str) -> None:
        """删除聊天流记录，并从所有 handle 上摘掉它。"""

        self.streams.pop(stream_id, None)
        for entry in self.handles.values():
            stream_list = entry.get("streams")
            if isinstance(stream_list, list) and stream_id in stream_list:
                stream_list.remove(stream_id)


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class TwitterForwarderPlugin(MaiBotPlugin):
    """基于 FxTwitter 的推特转发插件。"""

    config_model = TwitterForwarderConfig

    def __init__(self) -> None:
        """初始化运行时字段。"""

        super().__init__()
        self._state: Optional[StateStore] = None
        self._client: Optional[FxTwitterClient] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._label_task: Optional[asyncio.Task] = None
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._poll_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(3)
        self._stream_labels: dict[str, str] = {}
        self._translation_cache: dict[str, str] = {}
        self._link_cache: dict[str, str] = {}
        self.last_poll_at: float = 0.0
        self.last_poll_summary: str = "尚未轮询"

    # -- 生命周期 ---------------------------------------------------------

    async def on_load(self) -> None:
        """加载状态并启动后台轮询。"""

        self._state = StateStore(self._state_path())
        self._state.load()

        if not self.config.plugin.enabled:
            self._get_logger().info("推特转发插件已加载，但配置中处于禁用状态，未启动轮询")
            return

        if aiohttp is None:
            self._get_logger().error("推特转发插件缺少 aiohttp 依赖，无法启动轮询")
            return

        self._client = self._build_client()
        self._semaphore = asyncio.Semaphore(max(1, int(self.config.poll.max_concurrency)))
        self._cleanup_video_dir()
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._label_task = asyncio.create_task(self._refresh_stream_labels())
        asyncio.create_task(self._cleanup_container_video_dir())
        self._get_logger().info(
            "推特转发插件已启动：订阅 %d 个推主，轮询间隔 %d 分钟，代理=%s，视频模式=%s",
            len(self._state.handles),
            self._effective_interval_minutes(),
            self.config.twitter.proxy or "未配置",
            self.config.media.video_mode,
        )
        if self.config.media.video_mode == "auto" and float(self.config.media.max_video_mb or 0) > 0:
            docker = find_docker()
            if docker:
                self._get_logger().info(
                    "视频大文件通道就绪：docker=%s 容器=%s 内联上限=%.1fMB 外链上限=%.0fMB",
                    docker,
                    self.config.media.docker_container or "snowluma",
                    float(self.config.media.inline_video_mb or 0),
                    float(self.config.media.max_video_mb or 0),
                )
            else:
                self._get_logger().warning(
                    "找不到 docker 可执行文件（PATH=%s），超过内联上限 %.1fMB 的视频只能发封面",
                    os.environ.get("PATH", ""),
                    float(self.config.media.inline_video_mb or 0),
                )
        if bool(self.config.link.enabled):
            extractors = available_extractors()
            self._get_logger().info(
                "链接正文提取器：%s%s",
                "/".join(extractors) if extractors else "无",
                ""
                if extractors
                else "（trafilatura / bs4 / readability 都是可选增强，未安装时退化成正则清洗 + og:description）",
            )
            if not bool(self.config.link.allow_private_hosts):
                self._get_logger().debug("已启用链接 SSRF 防护：指向本机/内网的链接会被丢弃")

    async def on_unload(self) -> None:
        """停止后台任务并落盘状态。"""

        for task in (self._poll_task, self._label_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._poll_task = None
        self._label_task = None
        for task in list(self._cleanup_tasks):
            task.cancel()
        self._cleanup_tasks.clear()
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._save_state()
        self._get_logger().info("推特转发插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载：重建客户端并重启轮询任务。"""

        del config_data
        if scope != "self":
            return

        self._save_state()
        if not self.config.plugin.enabled:
            if self._poll_task is not None and not self._poll_task.done():
                self._poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._poll_task
            self._poll_task = None
            self._get_logger().info("推特转发配置已更新：插件被禁用，轮询已停止（version=%s）", version)
            return

        if self._client is not None:
            await self._client.close()
        self._client = self._build_client()
        self._semaphore = asyncio.Semaphore(max(1, int(self.config.poll.max_concurrency)))

        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._poll_task
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._get_logger().info(
            "推特转发配置已更新：轮询间隔 %d 分钟（version=%s）",
            self._effective_interval_minutes(),
            version,
        )

    # -- 运行辅助 ---------------------------------------------------------

    def _state_path(self) -> Path:
        """返回状态文件路径。"""

        try:
            return Path(self.ctx.paths.data_dir) / "state.json"
        except Exception:  # pragma: no cover - 上下文异常时退回插件目录
            return Path(__file__).with_name("data") / "state.json"

    def _build_client(self) -> FxTwitterClient:
        """按当前配置构造 FxTwitter 客户端。"""

        return FxTwitterClient(
            api_base=self.config.twitter.api_base,
            feed_base=self.config.twitter.feed_base,
            proxy=self.config.twitter.proxy,
            use_proxy_for_api=self.config.twitter.use_proxy_for_api,
            timeout=self.config.poll.request_timeout_seconds,
            retry_times=self.config.poll.retry_times,
            user_agent=self.config.twitter.user_agent,
            feed_fallback=self.config.twitter.enable_feed_fallback,
        )

    def _require_state(self) -> StateStore:
        """确保状态对象可用。"""

        if self._state is None:
            self._state = StateStore(self._state_path())
            self._state.load()
        return self._state

    def _save_state(self) -> None:
        """保存状态。"""

        if self._state is not None:
            self._state.save()

    def _effective_interval_minutes(self) -> int:
        """返回生效的轮询间隔（命令覆盖优先于配置）。"""

        state = self._require_state()
        override = state.runtime.get("interval_minutes")
        try:
            if override is not None:
                return max(1, min(1440, int(override)))
        except (TypeError, ValueError):
            pass
        return max(1, min(1440, int(self.config.poll.interval_minutes)))

    async def _refresh_stream_labels(self) -> None:
        """后台拉取聊天流名称，用于把 stream_id 显示成人看得懂的名字。"""

        await asyncio.sleep(5)
        try:
            streams = await self.ctx.chat.get_all_streams()
        except Exception as exc:
            self._get_logger().debug("获取聊天流列表失败（不影响推送）: %s", exc)
            return
        if not isinstance(streams, list):
            return
        labels: dict[str, str] = {}
        for item in streams:
            if not isinstance(item, dict):
                continue
            stream_id = str(item.get("session_id") or item.get("stream_id") or "").strip()
            if not stream_id:
                continue
            label = str(item.get("group_name") or item.get("user_nickname") or "").strip()
            labels[stream_id] = label or stream_id
        if labels:
            self._stream_labels = labels
            self._get_logger().debug("已解析 %d 个聊天流名称", len(labels))

    def _stream_label(self, stream_id: str) -> str:
        """返回聊天流显示名。"""

        state = self._require_state()
        entry = state.streams.get(stream_id) or {}
        label = str(entry.get("label") or "").strip() or self._stream_labels.get(stream_id, "")
        return label or stream_id

    # -- 轮询 -------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """后台轮询主循环。"""

        initial_delay = max(0, int(self.config.poll.initial_delay_seconds))
        try:
            await asyncio.sleep(initial_delay)
        except asyncio.CancelledError:
            raise

        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 防御性兜底
                self._get_logger().error("推特轮询异常: %s", exc, exc_info=True)
                self.last_poll_summary = f"轮询异常：{exc}"

            interval_seconds = self._effective_interval_minutes() * 60
            jitter = random.uniform(-0.05, 0.05) * interval_seconds
            try:
                await asyncio.sleep(max(30.0, interval_seconds + jitter))
            except asyncio.CancelledError:
                raise

    async def _poll_once(self, handles: Optional[Iterable[str]] = None) -> list[PollResult]:
        """执行一轮轮询。

        Args:
            handles: 需要轮询的 handle 列表；为 ``None`` 时轮询全部订阅。

        Returns:
            list[PollResult]: 每个推主的轮询结果。
        """

        state = self._require_state()
        client = self._client
        if client is None:
            return []

        if self._poll_lock.locked():
            self._get_logger().info("已有轮询正在进行，跳过本次请求")
            return []
        async with self._poll_lock:
            if handles is None:
                targets = list(state.handles.keys())
            else:
                targets = [handle for handle in handles if handle in state.handles]
            if not targets:
                self.last_poll_at = time.time()
                self.last_poll_summary = "没有订阅任何推主"
                self._get_logger().debug("本轮没有需要检查的推主，用 /tw_sub 订阅后才会开始推送")
                return []

            fetch_count = max(1, min(50, int(self.config.poll.fetch_count)))
            raw_results = await asyncio.gather(
                *(self._poll_handle(handle, fetch_count) for handle in targets),
                return_exceptions=True,
            )
            results: list[PollResult] = []
            for handle, item in zip(targets, raw_results):
                if isinstance(item, PollResult):
                    results.append(item)
                else:
                    self._get_logger().error("推主 @%s 轮询出现未处理异常: %s", handle, item, exc_info=item)
                    results.append(PollResult(handle=handle, error=str(item)))

            pushed = sum(item.pushed for item in results)
            failed = [item for item in results if item.error]
            self.last_poll_at = time.time()
            self.last_poll_summary = (
                f"检查 {len(results)} 个推主，推送 {pushed} 条新推文"
                + (f"，{len(failed)} 个失败" if failed else "")
            )
            self._save_state()
            self._get_logger().info("推特轮询完成：%s", self.last_poll_summary)
            for item in failed:
                self._get_logger().warning("推主 @%s 轮询失败：%s", item.handle, item.error)
            return results

    async def _poll_handle(self, handle: str, fetch_count: int) -> PollResult:
        """轮询单个推主并推送新推文。"""

        state = self._require_state()
        client = self._client
        entry = state.handle_entry(handle)
        result = PollResult(handle=handle)
        if client is None:
            result.error = "客户端未初始化"
            return result

        try:
            async with self._semaphore:
                tweets = await client.fetch_timeline(handle, fetch_count)
        except Exception as exc:
            entry["last_error"] = str(exc)
            entry["fail_count"] = int(entry.get("fail_count") or 0) + 1
            entry["last_check_at"] = time.time()
            result.error = str(exc)
            return result

        entry["fail_count"] = 0
        entry["last_error"] = ""
        entry["last_check_at"] = time.time()
        result.fetched = len(tweets)
        if not tweets:
            return result

        for tweet in tweets:
            if tweet.author_screen_name:
                entry["screen_name"] = tweet.author_screen_name
            if tweet.author_name and not tweet.is_repost:
                entry["display_name"] = tweet.author_name

        seen_ids = [str(item) for item in (entry.get("seen_ids") or [])]
        seen_set = set(seen_ids)
        last_seen_ts = float(entry.get("last_seen_ts") or 0.0)

        # 首次订阅：只建立基线，不补推历史
        if not seen_ids and last_seen_ts <= 0:
            entry["seen_ids"] = self._bounded_ids(seen_ids, [item.id for item in tweets])
            entry["last_seen_ts"] = max((item.created_ts for item in tweets), default=time.time())
            self._get_logger().info("推主 @%s 完成首次基线，已记录 %d 条历史推文", handle, len(tweets))
            return result

        fresh = [item for item in tweets if item.id not in seen_set and item.created_ts > last_seen_ts]
        fresh.sort(key=lambda item: item.created_ts)
        fresh = [item for item in fresh if self._accept_tweet(item)]

        max_push = max(1, int(self.config.poll.max_tweets_per_poll))
        to_push = fresh[:max_push]
        # 因为本轮条数上限被推迟的推文不能标记为已见，否则下一轮就补不上了
        deferred_ids = {item.id for item in fresh[max_push:]}
        delivered_ids: set[str] = set()

        # 一次拿到的新推文打包成一条「聊天记录」发出，每条推文一个节点
        delivered_tweets = await self._broadcast_many(to_push, handle)
        for tweet in delivered_tweets:
            delivered_ids.add(tweet.id)
            entry["last_seen_ts"] = max(float(entry.get("last_seen_ts") or 0.0), tweet.created_ts)
        if delivered_tweets:
            result.pushed = len(delivered_tweets)
            entry["last_new_at"] = time.time()
            entry["total_pushed"] = int(entry.get("total_pushed") or 0) + len(delivered_tweets)

        # 投递失败的推文同样不标记已见，下一轮会重试
        pending_ids = deferred_ids | {item.id for item in to_push if item.id not in delivered_ids}
        entry["seen_ids"] = self._bounded_ids(
            seen_ids,
            [item.id for item in tweets if item.id not in pending_ids],
        )
        return result

    @staticmethod
    def _bounded_ids(existing: list[str], incoming: list[str]) -> list[str]:
        """把新推文 ID 合入已见集合，并限制长度。"""

        merged = list(existing)
        merged_set = set(merged)
        for tweet_id in incoming:
            if tweet_id and tweet_id not in merged_set:
                merged.append(tweet_id)
                merged_set.add(tweet_id)
        if len(merged) > MAX_SEEN_IDS:
            merged = merged[-MAX_SEEN_IDS:]
        return merged

    def _accept_tweet(self, tweet: Tweet) -> bool:
        """按配置过滤不需要推送的推文。"""

        if tweet.is_repost and not self.config.push.include_reposts:
            return False
        if tweet.is_reply and not self.config.push.include_replies:
            return False
        if tweet.sensitive and self.config.push.skip_sensitive:
            return False
        return True

    def _targets_for_handle(self, handle: str) -> list[str]:
        """返回某个推主需要推送到的聊天流列表。"""

        state = self._require_state()
        entry = state.handle_entry(handle)
        targets: list[str] = []
        for stream_id in entry.get("streams") or []:
            normalized = str(stream_id).strip()
            if not normalized or normalized in targets:
                continue
            stream_entry = state.streams.get(normalized) or {}
            if bool(stream_entry.get("paused")):
                continue
            targets.append(normalized)
        for stream_id in self.config.push.extra_streams or []:
            normalized = str(stream_id).strip()
            if normalized and normalized not in targets:
                targets.append(normalized)
        return targets

    # -- 投递 -------------------------------------------------------------

    async def _broadcast_many(self, tweets: list[Tweet], handle: str) -> list[Tweet]:
        """把一批新推文推送给订阅该推主的所有聊天流。

        Returns:
            list[Tweet]: 至少成功送达一个聊天流的推文列表。
        """

        if not tweets:
            return []
        targets = self._targets_for_handle(handle)
        if not targets:
            self._get_logger().info("推主 @%s 有 %d 条新推文，但没有有效的推送目标，已跳过", handle, len(tweets))
            return []

        # 图片只下载一次，多个聊天流复用
        images_map: dict[str, list[bytes]] = {}
        for tweet in tweets:
            images_map[tweet.id] = await self._collect_images(tweet)

        delivered: dict[str, Tweet] = {}
        for stream_id in targets:
            try:
                for tweet in await self._deliver_many(tweets, stream_id, images_map):
                    delivered[tweet.id] = tweet
            except Exception as exc:
                self._get_logger().error(
                    "向 %s 推送 %d 条推文失败: %s", stream_id, len(tweets), exc, exc_info=True
                )
        return list(delivered.values())

    async def _collect_images(self, tweet: Tweet) -> list[bytes]:
        """下载推文配图（失败或超限的单张图片会被跳过）。"""

        client = self._client
        if client is None or not self.config.media.download_images:
            return []
        if tweet.has_video and str(self.config.media.video_mode).lower() == "link":
            return []

        max_images = max(0, int(self.config.media.max_images))
        if max_images <= 0:
            return []

        max_bytes = max(64 * 1024, int(float(self.config.media.max_image_mb) * 1024 * 1024))
        total_budget = max(64 * 1024, int(float(self.config.media.max_total_mb) * 1024 * 1024))
        quality = str(self.config.media.image_quality or "medium").strip().lower()
        timeout = max(5.0, float(self.config.media.media_timeout_seconds))

        images: list[bytes] = []
        total = 0
        for url in tweet.image_sources()[:max_images]:
            data = await client.download_image(resize_twimg_url(url, quality), max_bytes=max_bytes, timeout=timeout)
            if not data:
                self._get_logger().debug("图片下载失败或超限，已跳过: %s", url)
                continue
            if total + len(data) > total_budget:
                self._get_logger().info("图片总大小超过上限，后续图片已跳过: %s", url)
                break
            images.append(data)
            total += len(data)
        return images

    # -- 视频 -------------------------------------------------------------

    async def _pick_video_candidate(self, tweet: Tweet, max_bytes: int) -> Optional[tuple[str, int]]:
        """选择合适的视频变体。

        从码率最高到最低逐个 HEAD 查大小，返回第一个不超过 ``max_bytes`` 的
        ``(url, size)``；HEAD 拿不到时就按 码率×时长 估算；实在没信息就交给
        下载环节兜底（返回 ``(url, 0)``，由下载时的上限判断丢弃）。
        """

        client = self._client
        if client is None:
            return None
        video = next(iter(tweet.video_media()), None)
        if video is None:
            return None
        for url, bitrate in video.best_variant_candidates():
            size = await client.head_size(url)
            if size is not None:
                if 0 < size <= max_bytes:
                    return url, size
                continue
            estimated = int(bitrate * video.duration / 8) if bitrate and video.duration else 0
            if estimated and estimated <= max_bytes:
                return url, estimated
            if not video.formats and bitrate == 0:
                # 只有一个主地址且没有任何码率信息 → 试下载，超限由下载环节丢弃
                return url, 0
        return None

    async def _send_video_segment(self, stream_id: str, text: str, payload: dict[str, Any]) -> bool:
        """发送「文字 + 视频」混合消息。

        payload 为视频段的附加内容：``{"binary_data_base64": ...}``（内联）
        或 ``{"file": ...}``（容器内本地路径）。
        """

        video_segment = {"type": "video", "data": {"type": "video", **payload}}
        segments = [{"type": "text", "content": text}, video_segment]
        try:
            if await self.ctx.send.hybrid(segments, stream_id):
                return True
        except Exception as exc:
            self._get_logger().warning("发送「文字+视频」失败: %s", exc)

        # 兜底：文字、视频分开发
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            self._get_logger().warning("发送视频配套文字失败: %s", exc)
        try:
            return bool(await self.ctx.send.hybrid([video_segment], stream_id))
        except Exception as exc:
            self._get_logger().warning("单独发送视频失败: %s", exc)
            return False

    async def _prepare_video(self, tweet: Tweet, *, budget_bytes: Optional[int] = None) -> Optional[VideoPayload]:
        """把推文里的视频准备成可以放进消息的内容。

        先试内联 base64（同时受 ``inline_video_mb`` 和批次剩余预算限制），
        再试 docker 借道容器本地路径。两条路都不行返回 ``None``。

        Args:
            tweet: 目标推文。
            budget_bytes: 本批次还能容纳多少内联字节；``None`` 表示不受批次限制。
        """

        client = self._client
        if client is None or not tweet.video_media():
            return None
        logger = self._get_logger()
        max_video_mb = float(self.config.media.max_video_mb or 0)
        if max_video_mb <= 0:
            logger.info("视频上限 max_video_mb=0，跳过视频只发封面: %s", tweet.id)
            return None

        max_video_bytes = int(max_video_mb * 1024 * 1024)
        inline_mb = float(self.config.media.inline_video_mb or 0)
        inline_bytes = int(min(inline_mb, 11.0) * 1024 * 1024) if inline_mb > 0 else 0
        if budget_bytes is not None:
            inline_bytes = max(0, min(inline_bytes, int(budget_bytes)))
        timeout = max(15.0, float(self.config.media.video_timeout_seconds or 120))

        # 1) 内联 base64
        if inline_bytes > 0:
            picked = await self._pick_video_candidate(tweet, inline_bytes)
            if picked is not None:
                url, _size = picked
                data = await client.download_bytes(url, max_bytes=inline_bytes, timeout=timeout)
                if data:
                    return VideoPayload(
                        data={"binary_data_base64": base64.b64encode(data).decode("ascii")},
                        size=len(data),
                        url=url,
                    )
                logger.warning("视频内联下载失败或超限，尝试大文件路由: %s", url)
            else:
                logger.info(
                    "视频内联通道没有合适的变体（内联上限 %.1fMB，本批次剩余 %.1fMB）: %s",
                    inline_bytes / 1024 / 1024,
                    (budget_bytes or 0) / 1024 / 1024,
                    tweet.id,
                )

        # 2) docker 借道容器本地路径
        if not self.config.media.big_video_docker_route:
            logger.info("大视频容器路由已关闭(big_video_docker_route=false)，只发封面: %s", tweet.id)
            return None
        docker = find_docker()
        if not docker:
            logger.warning(
                "找不到 docker 可执行文件（PATH=%s 且常见路径都不存在），大视频无法借道容器，只发封面: %s",
                os.environ.get("PATH", ""),
                tweet.id,
            )
            return None
        picked = await self._pick_video_candidate(tweet, max_video_bytes)
        if picked is None:
            logger.warning(
                "视频超过上限 %.0fMB，没有可用变体，只发封面: %s", max_video_mb, tweet.id
            )
            return None
        url, _size = picked
        container = str(self.config.media.docker_container or "snowluma").strip()
        host_path = Path(self.ctx.paths.data_dir) / "videos" / f"{tweet.id}_{int(time.time() * 1000)}.mp4"
        try:
            if not await client.download_to_file(url, host_path, max_bytes=max_video_bytes, timeout=timeout):
                logger.warning("大视频下载失败或超限 %.0fMB，只发封面: %s", max_video_mb, url)
                with contextlib.suppress(OSError):
                    host_path.unlink(missing_ok=True)
                return None
            container_path = f"/app/data/twvideo/{host_path.name}"
            await self._docker_exec(container, "mkdir", "-p", "/app/data/twvideo")
            if await self._docker_cp(host_path, container, container_path):
                return VideoPayload(
                    data={"file": container_path},
                    size=0,
                    url=url,
                    container=container,
                    container_path=container_path,
                    host_path=host_path,
                )
            logger.warning("docker cp 未成功，大视频只发封面: %s", url)
        except Exception as exc:
            logger.warning("大视频容器路由失败: %s", exc, exc_info=True)
        with contextlib.suppress(OSError):
            host_path.unlink(missing_ok=True)
        return None

    async def _cleanup_video_payload(self, payload: VideoPayload, *, container_delay: Optional[float] = None) -> None:
        """清理视频临时产物。

        本地副本立刻删（已经 ``docker cp`` 进去了）；**容器里的文件延迟一会儿再删**：
        适配器拿到的是容器内路径，NapCat 传大视频可能在 ``send`` 返回之后还在读这个文件，
        删太早会让 QQ 那边收到失败或损坏的视频。
        """

        delay = self._video_keep_seconds() if container_delay is None else container_delay
        if payload.container and payload.container_path:
            if delay > 0:
                self._schedule_container_cleanup(payload.container, payload.container_path, delay)
            else:
                await self._docker_exec(payload.container, "rm", "-f", payload.container_path)
        if payload.host_path is not None:
            with contextlib.suppress(OSError):
                payload.host_path.unlink(missing_ok=True)

    def _video_keep_seconds(self) -> float:
        """容器内视频文件的保留时长（秒）。"""

        try:
            return max(0.0, float(self.config.media.video_keep_seconds or 0))
        except (TypeError, ValueError):
            return 180.0

    def _schedule_container_cleanup(self, container: str, container_path: str, delay: float) -> None:
        """延迟删除容器内的视频文件（放在后台任务里，不阻塞投递）。"""

        async def worker() -> None:
            try:
                await asyncio.sleep(delay)
                await self._docker_exec(container, "rm", "-f", container_path)
                self._get_logger().debug("已清理容器内视频: %s", container_path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 防御性兜底
                self._get_logger().warning("延迟清理容器内视频失败: %s", exc)

        try:
            task = asyncio.create_task(worker())
        except RuntimeError:  # 没有运行中的事件循环（理论上不会发生）
            return
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _cleanup_video_payloads(self, payloads: Iterable[VideoPayload]) -> None:
        """批量清理视频临时产物。"""

        for payload in payloads:
            try:
                await self._cleanup_video_payload(payload)
            except Exception as exc:
                self._get_logger().warning("清理视频临时文件失败: %s", exc)

    async def _deliver_video(self, tweet: Tweet, stream_id: str, text: str) -> bool:
        """把原视频单独成条发出去（不进聊天记录）；发不出去返回 ``False``。"""

        payload = await self._prepare_video(tweet)
        if payload is None:
            return False
        try:
            if await self._send_video_segment(stream_id, text, dict(payload.data)):
                self._get_logger().info(
                    "已%s发送视频 %s", "内联" if payload.is_inline else "通过容器路径", payload.url
                )
                return True
            return False
        finally:
            await self._cleanup_video_payload(payload)

    async def _docker_exec(self, container: str, *args: str, timeout: float = 30.0) -> None:
        """在容器里执行一条命令；失败只记一条日志。"""

        docker = find_docker()
        if not docker:
            self._get_logger().warning("docker exec 跳过：找不到 docker 可执行文件")
            return
        command = [docker, "exec", container, *args]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            if process.returncode not in (0, None):
                detail = (stderr or b"").decode("utf-8", "replace").strip()[:300]
                self._get_logger().warning("docker exec %s 返回 %s: %s", " ".join(command[2:]), process.returncode, detail)
        except (asyncio.TimeoutError, OSError) as exc:
            self._get_logger().warning("docker exec 异常 %s: %s", " ".join(command[2:]), exc)

    async def _docker_cp(self, host_path: Path, container: str, container_path: str, timeout: float = 300.0) -> bool:
        """把本地文件拷进容器；成功返回 ``True``。

        先试 ``docker cp``；失败（snap 版 docker 有私有 ``/tmp``，
        也可能读不到家目录里的隐藏路径）就改用 ``docker exec -i`` + stdin 把
        字节流灌进容器——这条路是宿主进程自己读文件，不受 snap 沙箱限制。
        """

        logger = self._get_logger()
        docker = find_docker()
        if not docker:
            logger.warning("docker cp 跳过：找不到 docker 可执行文件")
            return False

        # 方式一：docker cp
        try:
            process = await asyncio.create_subprocess_exec(
                docker, "cp", str(host_path), f"{container}:{container_path}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            if process.returncode in (0, None):
                return True
            detail = (stderr or b"").decode("utf-8", "replace").strip()[:300]
            logger.warning("docker cp 到 %s 失败 (%s): %s，改用 stdin 管道重试", container_path, process.returncode, detail)
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("docker cp 到 %s 异常: %s，改用 stdin 管道重试", container_path, exc)

        # 方式二：把文件经 stdin 送进容器
        try:
            with open(host_path, "rb") as handle:
                process = await asyncio.create_subprocess_exec(
                    docker, "exec", "-i", container, "sh", "-c", f"cat > {shlex.quote(container_path)}",
                    stdin=handle,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            if process.returncode in (0, None):
                logger.info("已通过 stdin 管道把视频送进容器: %s", container_path)
                return True
            detail = (stderr or b"").decode("utf-8", "replace").strip()[:300]
            logger.warning("stdin 管道写入 %s 失败 (%s): %s", container_path, process.returncode, detail)
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("stdin 管道写入 %s 异常: %s", container_path, exc)
        return False

    def _cleanup_video_dir(self) -> None:
        """清理视频临时目录里遗留的旧文件（插件崩溃/重启时可能残留）。"""

        try:
            video_dir = Path(self.ctx.paths.data_dir) / "videos"
            if not video_dir.exists():
                return
            cutoff = time.time() - 24 * 3600
            for path in video_dir.iterdir():
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue
        except Exception:
            pass

    async def _cleanup_container_video_dir(self) -> None:
        """清掉容器里遗留的旧视频。

        延迟删除的任务在插件卸载/重载时会被取消，文件就留在容器里了，
        这里按「保留时长 ×2（至少 10 分钟）」扫一遍。留这么久是为了不误删
        正在上传的大视频——上传超过 10 分钟还没结束的话本身也早就失败了。
        """

        if not bool(self.config.media.big_video_docker_route):
            return
        if not find_docker():
            return
        minutes = max(10, int(self._video_keep_seconds() / 60 * 2))
        container = str(self.config.media.docker_container or "snowluma").strip()
        try:
            await self._docker_exec(
                container,
                "sh", "-c",
                f"find /app/data/twvideo -type f -mmin +{minutes} -delete 2>/dev/null || true",
            )
        except Exception as exc:  # pragma: no cover - 防御性兜底
            self._get_logger().debug("清理容器内旧视频失败: %s", exc)

    # -- 链接内容 ---------------------------------------------------------

    async def _url_allowed(self, url: str) -> tuple[bool, str]:
        """抓取前的 SSRF 校验；``link.allow_private_hosts`` 打开时一律放行。"""

        if bool(self.config.link.allow_private_hosts):
            return True, ""
        return await url_is_public(url)

    async def _fetch_link_page(self, url: str, *, timeout: float, max_bytes: int) -> str:
        """抓取链接页面：直连优先，连不上自动换代理重试一次。

        推文里的链接是不可信输入，所以会先做 SSRF 校验，并在每次跳转时重新校验；
        解析到本机/内网地址的链接直接丢弃（``link.allow_private_hosts`` 可放开）。
        """

        client = self._client
        if client is None:
            return ""
        allowed, reason = await self._url_allowed(url)
        if not allowed:
            self._get_logger().warning("链接内容跳过（SSRF 防护）: %s → %s", url, reason)
            return ""

        explicit_proxy = str(self.config.link.proxy or "").strip()
        if explicit_proxy:
            return await client.fetch_text(
                url, proxy=explicit_proxy, timeout=timeout, max_bytes=max_bytes, validate=self._url_allowed
            )

        page = await client.fetch_text(url, timeout=timeout, max_bytes=max_bytes, validate=self._url_allowed)
        if page:
            return page

        if bool(self.config.link.fallback_proxy):
            fallback_proxy = str(self.config.twitter.proxy or "").strip()
            if fallback_proxy:
                self._get_logger().debug("直连抓取不到内容，改用代理重试: %s", url)
                page = await client.fetch_text(
                    url,
                    proxy=fallback_proxy,
                    timeout=timeout,
                    max_bytes=max_bytes,
                    validate=self._url_allowed,
                )
                if page:
                    self._get_logger().info("代理抓取成功: %s", url)
        return page

    async def _fetch_steam_news(self, appid: str, gid: str, *, timeout: float) -> tuple[str, str]:
        """从 Steam 新闻 RSS 里取出指定公告的标题与正文（页面正文是 JS 渲染的，抓不到）。"""

        client = self._client
        if client is None:
            return "", ""
        language = str(self.config.link.link_language or "").strip()
        url = f"https://store.steampowered.com/feeds/news/app/{appid}/"
        if language:
            url = f"{url}?l={language}"
        feed = await self._fetch_link_page(url, timeout=timeout, max_bytes=2 * 1024 * 1024)
        if not feed:
            return "", ""

        for item in re.findall(r"<item>(.*?)</item>", feed, re.S):
            if gid not in item:
                continue
            title_match = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
            body_match = re.search(r"<description>(.*?)</description>", item, re.S)
            title = html_module.unescape(title_match.group(1)).strip() if title_match else ""
            body_html = body_match.group(1) if body_match else ""
            body_html = body_html.removeprefix("<![CDATA[").removesuffix("]]>")
            # RSS 里的 HTML 是转义过的，先还原再转文本
            decoded_html = html_module.unescape(body_html)
            body = html_to_text(decoded_html)
            if not body:
                # 有些公告正文就是个视频嵌入，把视频链接搬出来
                media_links = extract_media_links(decoded_html)
                body = "\n".join(f"🎬 {link}" for link in media_links[:2])
            return title, body
        return "", ""

    async def _fetch_link_preview(self, url: str) -> tuple[str, str]:
        """抓取链接内容，返回 ``(标题, 正文)``；失败返回两个空串。"""

        client = self._client
        if client is None:
            return "", ""
        timeout = max(5.0, float(self.config.link.timeout_seconds or 25))
        max_bytes = int(max(0.2, float(self.config.link.max_page_mb or 2.0)) * 1024 * 1024)

        steam_match = STEAM_NEWS_RE.match(url)
        if steam_match:
            appid, gid = steam_match.group(1), steam_match.group(2)
            title, body = await self._fetch_steam_news(appid, gid, timeout=timeout)
            if body:
                return title, body
            if title:
                # 公告确实只有标题（正文是视频嵌入之类）时，不再去抓那个 SPA 页面——
                # 抓到的只会是 Steam 的导航栏
                return title, ""

        page = await self._fetch_link_page(url, timeout=timeout, max_bytes=max_bytes)
        if not page:
            return "", ""
        return extract_page_title(page), extract_article_text(page)

    async def _attach_link_previews(self, tweets: list[Tweet]) -> None:
        """给带链接的推文附上链接正文（需要的话先翻译）。"""

        if not tweets or not bool(self.config.link.enabled):
            return
        max_links = max(0, int(self.config.link.max_links or 0))
        if max_links <= 0:
            return
        max_chars = max(80, int(self.config.link.max_chars or 700))
        target_lang = str(self.config.translation.target_lang or "")
        semaphore = asyncio.Semaphore(max(1, int(self.config.translation.max_concurrency or 3)))
        translate_link = bool(self.config.link.translate) and bool(self.config.translation.enabled)

        async def build_preview(tweet: Tweet) -> None:
            links = extract_links(tweet.text)[:max_links]
            if not links:
                return
            url = links[0]
            cache_key = f"{url}|link|{target_lang if translate_link else 'raw'}"
            cached = self._link_cache.get(cache_key)
            if cached is not None:
                tweet.link_preview = cached
                tweet.expanded_links.append(url)
                return
            try:
                async with semaphore:
                    title, body = await self._fetch_link_preview(url)
            except Exception as exc:
                self._get_logger().warning("抓取链接内容失败（%s）: %s", url, exc)
                return
            if not body:
                # 只有标题没有正文（例如抓到的是个菜单页）就不加这个块，免得刷屏
                self._get_logger().debug("链接正文为空，跳过链接内容块: %s", url)
                return
            # 先翻译全文，最后才按长度上限截断，避免"翻到一半被砍"
            if body and translate_link:
                async with semaphore:
                    body = await self._translate_long_text(body)
            if body:
                body = truncate_at_paragraph(body, max_chars)
            title = truncate_text(title, 90)
            if body and title and body.startswith(title[:20]):
                title = ""
            preview = "\n".join(part for part in (f"📄 链接内容 · {title}" if title else "📄 链接内容", body) if part)
            tweet.link_preview = preview
            tweet.expanded_links.append(url)
            self._remember_link_preview(cache_key, preview)

        try:
            await asyncio.gather(*(build_preview(tweet) for tweet in tweets))
        except Exception as exc:  # pragma: no cover - 防御性兜底
            self._get_logger().warning("批量抓取链接内容异常: %s", exc)

    def _remember_link_preview(self, key: str, value: str) -> None:
        """写入链接内容缓存，并限制规模。"""

        self._link_cache[key] = value
        overflow = len(self._link_cache) - MAX_LINK_CACHE
        if overflow > 0:
            for old_key in list(self._link_cache.keys())[:overflow]:
                self._link_cache.pop(old_key, None)

    # -- 翻译 -------------------------------------------------------------

    def _remember_translation(self, key: str, value: str) -> None:
        """写入译文缓存，并限制缓存规模。"""

        self._translation_cache[key] = value
        overflow = len(self._translation_cache) - MAX_TRANSLATION_CACHE
        if overflow > 0:
            for old_key in list(self._translation_cache.keys())[:overflow]:
                self._translation_cache.pop(old_key, None)

    def _should_skip_translation(self, text: str, lang_hint: str) -> bool:
        """判断文本是否已经是目标语言，是的话不必翻译。"""

        if not bool(self.config.translation.skip_if_target_lang):
            return False
        target_code = match_target_lang_code(self.config.translation.target_lang)
        if not target_code:
            return False
        hint = (lang_hint or "").strip().lower()
        if hint:
            return hint.startswith(target_code)
        return looks_like_language(text, target_code)

    async def _translate_text(
        self,
        text: str,
        *,
        lang_hint: str = "",
        respect_length_limit: bool = True,
    ) -> str:
        """把一段文本翻译成目标语言。

        任何失败（超时、模型报错、输出为空）都返回原文，保证推送不因翻译挂掉。

        Args:
            respect_length_limit: 是否套用 ``translation.max_chars`` 长度上限；
                链接正文有自己的长度上限，传 ``False`` 交给上层控制。
        """

        normalized = (text or "").strip()
        if not normalized:
            return ""
        config = self.config.translation
        if not config.enabled:
            return normalized

        max_chars = max(0, int(config.max_chars or 0))
        if respect_length_limit and max_chars and len(normalized) > max_chars:
            self._get_logger().debug("文本 %d 字符超过翻译上限，保留原文", len(normalized))
            return normalized
        if self._should_skip_translation(normalized, lang_hint):
            self._get_logger().debug("文本已是目标语言（lang=%s），跳过翻译", lang_hint or "未知")
            return normalized

        model_task = str(config.model_task or "replyer").strip() or "replyer"
        timeout = max(5.0, float(config.timeout_seconds or 45))
        system_prompt = str(config.prompt or "").replace("{target_lang}", str(config.target_lang or "简体中文"))
        prompt = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": normalized},
        ]
        try:
            result = await asyncio.wait_for(
                self.ctx.llm.generate(prompt, model=model_task, rpc_timeout_ms=int(timeout * 1000)),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError:
            self._get_logger().warning("翻译超时（>%ss），保留原文", timeout)
            return normalized
        except Exception as exc:
            self._get_logger().warning("翻译调用失败，保留原文: %s", exc)
            return normalized

        if not isinstance(result, dict):
            return normalized
        if result.get("success") is False:
            self._get_logger().warning("翻译未成功，保留原文: %s", result.get("error") or result)
            return normalized

        translated = clean_translation(str(result.get("response") or result.get("content") or ""))
        if not translated:
            self._get_logger().warning("翻译结果为空，保留原文")
            return normalized
        return translated

    async def _translate_long_text(self, text: str, *, lang_hint: str = "") -> str:
        """长文分段翻译：按段落切开分别翻，避免一次输出撞上模型 max_tokens 被截断。"""

        normalized = (text or "").strip()
        if not normalized:
            return ""
        chunk_size = max(200, int(self.config.link.translate_chunk_chars or 1200))
        chunks = split_text_chunks(normalized, chunk_size)
        if len(chunks) <= 1:
            return await self._translate_text(normalized, lang_hint=lang_hint, respect_length_limit=False)

        semaphore = asyncio.Semaphore(max(1, int(self.config.translation.max_concurrency or 3)))

        async def translate_chunk(chunk: str) -> str:
            async with semaphore:
                return await self._translate_text(chunk, lang_hint=lang_hint, respect_length_limit=False)

        self._get_logger().debug("长文分 %d 段翻译（每段 ≤ %d 字符）", len(chunks), chunk_size)
        translated = await asyncio.gather(*(translate_chunk(chunk) for chunk in chunks))
        return "\n".join(part for part in translated if part)

    async def _translate_tweets(self, tweets: list[Tweet]) -> None:
        """就地给推文写入译文（结果会缓存，多个群共用、重试不重复调用模型）。"""

        if not tweets or not bool(self.config.translation.enabled):
            return
        target = str(self.config.translation.target_lang or "")
        semaphore = asyncio.Semaphore(max(1, int(self.config.translation.max_concurrency or 3)))
        translate_quote = bool(self.config.translation.translate_quote)

        async def translate_body(tweet: Tweet) -> None:
            if not (tweet.text or "").strip():
                return
            key = f"{tweet.id}|body|{target}"
            cached = self._translation_cache.get(key)
            if cached is None:
                async with semaphore:
                    cached = await self._translate_text(tweet.text, lang_hint=tweet.lang)
                if cached and cached != (tweet.text or "").strip():
                    self._remember_translation(key, cached)
            if cached and cached != (tweet.text or "").strip():
                tweet.translation = cached

        async def translate_quote(tweet: Tweet) -> None:
            if not (tweet.quote_text or "").strip():
                return
            key = f"{tweet.id}|quote|{target}"
            cached = self._translation_cache.get(key)
            if cached is None:
                async with semaphore:
                    cached = await self._translate_text(tweet.quote_text)
                if cached and cached != (tweet.quote_text or "").strip():
                    self._remember_translation(key, cached)
            if cached and cached != (tweet.quote_text or "").strip():
                tweet.quote_translation = cached

        try:
            await asyncio.gather(*(translate_body(tweet) for tweet in tweets))
            if translate_quote:
                await asyncio.gather(*(translate_quote(tweet) for tweet in tweets))
        except Exception as exc:  # pragma: no cover - 防御性兜底
            self._get_logger().warning("批量翻译异常，相关推文保留原文: %s", exc)

    async def _deliver(self, tweet: Tweet, stream_id: str, images: Optional[list[bytes]] = None) -> bool:
        """把一条推文发送到指定聊天流（单条的便捷入口，内部走批量逻辑）。"""

        return bool(await self._deliver_many([tweet], stream_id, {tweet.id: images} if images is not None else None))

    async def _deliver_many(
        self,
        tweets: list[Tweet],
        stream_id: str,
        images_map: Optional[dict[str, list[bytes]]] = None,
    ) -> list[Tweet]:
        """把一批推文投递到一个聊天流，返回真正送出去的推文列表。

        排版规则：
        * 一律打包成「聊天记录」（合并转发），**每条推文占其中一个节点**，
          **最新的推文排在最上面**（单条推文同样走聊天记录）；
        * 视频默认放进它自己的节点（`display.video_in_forward`），关掉则视频单独成条；
        * 图片/视频总体积超过 `batch_max_mb` 或节点数超过上限时，自动拆成多条聊天记录；
        * 打包失败则逐条回退成普通图文，绝不因为格式问题丢推文。
        """

        if not tweets:
            return []

        # 先翻译（结果会缓存到 Tweet 上，多群推送只翻译一次），再抓链接正文
        await self._translate_tweets(tweets)
        await self._attach_link_previews(tweets)

        delivered: list[Tweet] = []
        pending: list[Tweet] = []
        video_payloads: dict[str, VideoPayload] = {}
        video_mode = str(self.config.media.video_mode or "auto").strip().lower()
        batch_budget = max(1.0, float(self.config.display.batch_max_mb or 7.0)) * 1024 * 1024
        video_tweets = [tweet for tweet in tweets if tweet.has_video and video_mode == "auto"]
        # 关掉合并转发格式时不做"视频进节点"，让视频走单独成条
        use_forward_format = bool(self.config.display.use_forward)

        if video_tweets and bool(self.config.display.video_in_forward) and use_forward_format:
            # 视频塞进各自的聊天记录节点：预算按队列依次扣减
            remaining_budget = batch_budget
            for tweet in tweets:
                if tweet.has_video and video_mode == "auto":
                    try:
                        payload = await self._prepare_video(tweet, budget_bytes=remaining_budget)
                    except Exception as exc:
                        payload = None
                        self._get_logger().warning("视频准备异常，改为聊天记录里的封面: %s", exc)
                    if payload is not None:
                        video_payloads[tweet.id] = payload
                        remaining_budget = max(0, remaining_budget - payload.size)
                        pending.append(tweet)
                        continue
                    self._get_logger().warning(
                        "视频没能随推文发出，这条改为封面 + 链接: %s", tweet.id
                    )
                pending.append(tweet)
        else:
            # 视频单独成条（关掉了 video_in_forward 或不用合并转发格式）
            for tweet in tweets:
                if tweet.has_video and video_mode == "auto":
                    try:
                        if await self._deliver_video(tweet, stream_id, self._render_tweet(tweet, [])):
                            delivered.append(tweet)
                            continue
                        self._get_logger().warning(
                            "视频没能单独发出，这条改为封面 + 链接: %s", tweet.id
                        )
                    except Exception as exc:
                        self._get_logger().warning("视频投递异常，改为聊天记录里的封面: %s", exc)
                pending.append(tweet)

        # 最新的排在最上面
        pending.sort(key=lambda item: item.created_ts, reverse=True)

        try:
            if not pending:
                return delivered

            # 关掉批量或关掉合并转发格式时，逐条发
            if not bool(self.config.display.batch_forward) or not use_forward_format:
                for tweet in pending:
                    images = [] if tweet.id in video_payloads else (images_map or {}).get(tweet.id)
                    if await self._deliver_plain(tweet, stream_id, images):
                        delivered.append(tweet)
                return delivered

            # 打包成若干条「聊天记录」
            chunk: list[Tweet] = []
            chunk_images: dict[str, list[bytes]] = {}
            chunk_payloads: dict[str, VideoPayload] = {}
            chunk_bytes = 0

            async def flush() -> None:
                nonlocal chunk, chunk_images, chunk_payloads, chunk_bytes
                if not chunk:
                    return
                batch = list(chunk)
                batch_images = dict(chunk_images)
                batch_payloads = dict(chunk_payloads)
                if await self._send_forward_nodes(batch, stream_id, batch_images, batch_payloads):
                    delivered.extend(batch)
                else:
                    self._get_logger().warning("聊天记录发送失败，%d 条推文改为逐条发送", len(batch))
                    for item in batch:
                        if item.id in batch_payloads:
                            # 视频节点没法退化成普通消息，重新单独发一次视频
                            payload = batch_payloads[item.id]
                            if await self._send_video_segment(
                                stream_id, self._render_tweet(item, []), dict(payload.data)
                            ):
                                delivered.append(item)
                            continue
                        if await self._deliver_plain(item, stream_id, batch_images.get(item.id)):
                            delivered.append(item)
                chunk = []
                chunk_images = {}
                chunk_payloads = {}
                chunk_bytes = 0

            for tweet in pending:
                payload = video_payloads.get(tweet.id)
                if payload is not None:
                    images: list[bytes] = []
                    size = payload.size
                else:
                    images = (images_map or {}).get(tweet.id)
                    if images is None:
                        images = await self._collect_images(tweet)
                    size = sum(len(item) for item in images)
                if chunk and (len(chunk) >= MAX_NODES_PER_FORWARD or chunk_bytes + size > batch_budget):
                    await flush()
                chunk.append(tweet)
                chunk_images[tweet.id] = images
                if payload is not None:
                    chunk_payloads[tweet.id] = payload
                chunk_bytes += size

            await flush()
            return delivered
        finally:
            if video_payloads:
                await self._cleanup_video_payloads(video_payloads.values())

    async def _send_forward_nodes(
        self,
        tweets: list[Tweet],
        stream_id: str,
        images_map: dict[str, list[bytes]],
        payloads_map: Optional[dict[str, VideoPayload]] = None,
    ) -> bool:
        """把若干推文打包成一条聊天记录（合并转发），每条推文一个节点。"""

        payloads_map = payloads_map or {}
        nodes: list[dict[str, Any]] = []
        for tweet in tweets:
            payload = payloads_map.get(tweet.id)
            if payload is not None:
                # QQ 协议要求「视频必须是消息里唯一的元素」，
                # 所以文字单独一个节点、视频自己一个节点，整体仍属于同一条聊天记录。
                nodes.append(
                    {
                        "user_id": "0",
                        "nickname": self._node_nickname(tweet),
                        "segments": [{"type": "text", "content": self._render_tweet(tweet, [])}],
                    }
                )
                nodes.append(
                    {
                        "user_id": "0",
                        "nickname": self._node_nickname(tweet),
                        "segments": [{"type": "video", "data": {"type": "video", **payload.data}}],
                    }
                )
            else:
                images = images_map.get(tweet.id) or []
                nodes.append(
                    {
                        "user_id": "0",
                        "nickname": self._node_nickname(tweet),
                        "segments": self._build_segments(self._render_tweet(tweet, images), images),
                    }
                )
        if not nodes:
            return False
        try:
            ok = bool(await self.ctx.send.forward(nodes, stream_id))
        except Exception as exc:
            self._get_logger().warning("发送聊天记录异常: %s", exc)
            return False
        if ok and payloads_map:
            self._get_logger().info(
                "聊天记录已发出，其中 %d 个视频节点：%s",
                len(payloads_map),
                ", ".join(
                    f"{tweet.id}={'容器路径' if not payloads_map[tweet.id].is_inline else '内联'}"
                    for tweet in tweets
                    if tweet.id in payloads_map
                ),
            )
        return ok

    async def _deliver_plain(
        self,
        tweet: Tweet,
        stream_id: str,
        images: Optional[list[bytes]] = None,
    ) -> bool:
        """只用「文字 + 配图」发送一条推文，不尝试视频。"""

        if images is None:
            images = await self._collect_images(tweet)
        text = self._render_tweet(tweet, images)

        if images and self.config.display.use_forward:
            if await self._send_forward_nodes([tweet], stream_id, {tweet.id: images}):
                return True
            self._get_logger().warning("合并转发失败，回退为普通图文发送: stream=%s", stream_id)

        sent = await self.ctx.send.text(text, stream_id)
        for data in images:
            try:
                await self.ctx.send.image(base64.b64encode(data).decode("ascii"), stream_id)
            except Exception as exc:
                self._get_logger().warning("图片发送失败: %s", exc)
        return bool(sent)

    def _node_nickname(self, tweet: Tweet) -> str:
        """合并转发节点使用的昵称。"""

        base = str(self.config.display.forward_nickname or "推特转发").strip()
        author = tweet.author_name or tweet.author_screen_name
        return f"{base} · {author}" if author else base

    @staticmethod
    def _build_segments(text: str, images: list[bytes]) -> list[dict[str, Any]]:
        """构造合并转发的消息段。"""

        segments: list[dict[str, Any]] = [{"type": "text", "content": text}]
        for data in images:
            segments.append({"type": "image", "content": base64.b64encode(data).decode("ascii")})
        return segments

    def _body_text(self, tweet: Tweet) -> str:
        """返回展示用正文：有译文用译文，去掉已展开的链接，并收敛空行。"""

        source = (tweet.translation or tweet.text or "").strip()
        if source and tweet.link_preview and bool(self.config.display.hide_expanded_url):
            for url in tweet.expanded_links:
                if url:
                    source = source.replace(url, "")
        source = re.sub(r"[ \t]+\n", "\n", source)
        source = re.sub(r"\n{3,}", "\n\n", source)
        return source.strip()

    def _render_tweet(self, tweet: Tweet, images: Optional[list[bytes]] = None) -> str:
        """把推文渲染成聊天消息文本。

        排版分四段：作者头 → 正文（含引用、配图说明）→ 链接内容 → 尾部（原推链接、互动数据），
        链接内容与尾部各自前面空一行，避免糊成一片。
        """

        display = self.config.display
        lines: list[str] = []

        if tweet.is_repost:
            who = tweet.reposted_by_name or tweet.reposted_by_screen_name or "某人"
            handle_part = f" (@{tweet.reposted_by_screen_name})" if tweet.reposted_by_screen_name else ""
            lines.append(f"🔁 {who}{handle_part} 转推")

        if display.show_author:
            name = tweet.author_name or tweet.author_screen_name or "未知"
            handle_part = f" (@{tweet.author_screen_name})" if tweet.author_screen_name else ""
            lines.append(f"🐦 {name}{handle_part} · {format_time(tweet.created_ts)}")
            divider = str(display.header_divider or "").strip()
            if divider:
                lines.append(divider)

        if tweet.sensitive:
            lines.append("⚠️ 该推文被标记为可能包含敏感内容")

        limit = max(0, int(display.max_text_chars))
        body = self._body_text(tweet)
        body = truncate_text(body, limit) if limit else body
        lines.append(body or "（无正文）")

        if tweet.quote_text:
            quote_source = tweet.quote_translation or tweet.quote_text
            quote = truncate_text(quote_source, 140)
            quote_author = f"@{tweet.quote_author} " if tweet.quote_author else ""
            lines.append(f"┌ 引用 {quote_author}{quote}")

        image_count = len(images or [])
        if image_count:
            lines.append(f"🖼 配图 {image_count} 张")
        elif tweet.has_video:
            video_mode = str(self.config.media.video_mode or "auto").strip().lower()
            if video_mode == "link":
                lines.append("🎬 含视频，点开原推可看")
            elif video_mode == "thumbnail":
                lines.append("🎬 视频封面（原视频点开原推查看）")
            # auto 模式下原视频会尽量直接发过来，这里不加额外说明

        if tweet.link_preview:
            lines.append("")
            lines.append(tweet.link_preview)

        footer: list[str] = []
        if display.show_link:
            footer.append(f"🔗 {tweet.url}")
        if display.show_stats:
            footer.append(
                f"❤️ {format_count(tweet.likes)} · 🔁 {format_count(tweet.reposts)}"
                f" · 💬 {format_count(tweet.replies)} · 👁 {format_count(tweet.views)}"
            )
        if footer:
            lines.append("")
            lines.extend(footer)

        return "\n".join(line for line in lines if line is not None).strip()

    # -- 命令辅助 ---------------------------------------------------------

    #: 会看到/改到「别的聊天」或全局轮询状态的命令，默认只允许管理员与本地操作员
    CROSS_CHAT_COMMANDS = frozenset({"all", "del", "reset", "check", "interval"})

    def _is_allowed(self, kwargs: dict[str, Any], *, cross_chat: bool = False) -> bool:
        """判断调用者是否有权使用命令。

        Args:
            cross_chat: 该命令是否会影响全局订阅或轮询状态（如 ``/tw_del``、``/tw_interval``）。
                这类命令默认（``command.cross_chat_admin_only``）只允许管理员与本地操作员，
                避免群里的普通成员动到别的群。
        """

        command = self.config.command
        need_admin = bool(command.admin_only)
        if cross_chat and bool(getattr(command, "cross_chat_admin_only", True)):
            need_admin = True
        if not need_admin:
            return True
        if bool(kwargs.get("is_local_operator")):
            return True
        user_id = str(kwargs.get("user_id") or "").strip()
        admins = {str(item).strip() for item in (command.admins or []) if str(item).strip()}
        return bool(user_id) and user_id in admins

    @staticmethod
    def _deny_message(*, cross_chat: bool = False) -> str:
        """权限不足时的回复。"""

        if cross_chat:
            return (
                "这条命令会影响所有聊天的订阅或全局轮询状态，默认只有管理员能用。"
                "可以在插件配置的「命令」里把自己加进 admins，或把 cross_chat_admin_only 关掉。"
            )
        return "你没有权限使用这个命令。可在插件配置的「命令」里把自己加进 admins，或把 admin_only 关掉。"

    async def _reply(self, stream_id: str, text: str) -> None:
        """向聊天流回复文本。"""

        target = str(stream_id or "").strip()
        if not target:
            return
        try:
            await self.ctx.send.text(text, target)
        except Exception as exc:
            self._get_logger().error("命令回复发送失败: %s", exc, exc_info=True)

    async def _suggest_handle(self, token: str) -> str:
        """输入不像合法用户名时，猜一个真的存在的用户名。

        做法是拿"删掉某个下划线"之类的候选去问接口，接口认得才提示，
        所以不会瞎猜。
        """

        client = self._client
        if client is None:
            return ""
        for candidate in handle_repair_candidates(token)[:3]:
            try:
                profile = await client.get_profile(candidate)
            except Exception:
                continue
            return str(profile.get("screen_name") or candidate)
        return ""

    async def _resolve_handles(
        self,
        raw: str,
        stream_id: str,
        usage: str,
        command: str = "/tw_sub",
    ) -> tuple[list[str], list[str]]:
        """解析命令参数里的用户名。

        一个都解析不出来时，直接回复"哪里不对 + 猜一个正确写法"，并返回空列表。

        Returns:
            tuple[list[str], list[str]]: ``(可用用户名, 被跳过输入的原因)``。
        """

        if not (raw or "").strip():
            await self._reply(stream_id, f"没有写用户名哦。\n{usage}")
            return [], []

        handles, problems = parse_handle_input(raw)
        if handles:
            return handles, problems

        lines = ["❌ 没能识别出可用的用户名："]
        for problem in problems[:4]:
            lines.append(f"• {problem}")
        for problem in problems[:2]:
            token = problem.split("：", 1)[0]
            suggestion = await self._suggest_handle(token)
            if suggestion:
                lines.append(f"👉 你是不是想订阅 @{suggestion}？可以发：{command} {suggestion}")
                break
        lines.append(usage)
        await self._reply(stream_id, "\n".join(lines))
        return [], problems

    def _close_match(self, handle: str) -> str:
        """在已订阅的推主里找一个最接近的名字，用于纠正手误。"""

        state = self._require_state()
        if not state.handles or handle in state.handles:
            return ""
        matches = difflib.get_close_matches(handle, list(state.handles.keys()), n=1, cutoff=0.72)
        return matches[0] if matches else ""

    async def _ensure_subscribed(self, handle: str) -> tuple[dict[str, Any], bool]:
        """确保状态里存在该 handle 的订阅记录。

        Returns:
            tuple[dict, bool]: ``(订阅记录, 是否为新建)``。
        """

        state = self._require_state()
        is_new = handle not in state.handles
        entry = state.handle_entry(handle)
        return entry, is_new

    def _help_text(self) -> str:
        """帮助文本。"""

        return (
            "🐦 推特转发插件命令\n"
            "── 订阅管理 ──\n"
            "/tw_sub <用户名…>　订阅推主到当前聊天（可用 @名 或推文链接）\n"
            "/tw_unsub <用户名…>　取消当前聊天对该推主的订阅\n"
            "/tw_list　查看当前聊天的订阅\n"
            "/tw_all　查看全部订阅与推送目标\n"
            "/tw_on / /tw_off　开启 / 暂停当前聊天的推送\n"
            "── 运行控制 ──\n"
            "/tw_check [用户名]　立刻检查一次（不带参数=检查全部）\n"
            "/tw_test <用户名> [条数]　把最新推文立刻推过来预览（不影响订阅状态）\n"
            "/tw_interval [分钟|reset]　查看或调整轮询间隔\n"
            "/tw_reset <用户名>　重建基线：下次轮询不补推历史\n"
            "/tw_del <用户名>　彻底删除该推主的全部订阅\n"
            "/tw_status　查看运行状态\n"
            "/tw_help　显示本帮助"
        )

    # -- 命令：订阅管理 ---------------------------------------------------

    @Command(
        "tw_sub",
        description="订阅推特推主，新推文会转发到当前聊天",
        pattern=_LEAD + r"tw_sub\b(?:\s+(?P<handles>.+?))?\s*$",
        permission="public",
    )
    async def handle_sub(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_sub 命令。"""

        if not self._is_allowed(kwargs):
            await self._reply(stream_id, self._deny_message())
            return False, "没有权限", True

        raw = str((kwargs.get("matched_groups") or {}).get("handles") or "").strip()
        handles, problems = await self._resolve_handles(
            raw,
            stream_id,
            "用法：/tw_sub <用户名>，例如 /tw_sub elonmusk 或 /tw_sub https://x.com/elonmusk",
            "/tw_sub",
        )
        if not handles:
            return False, "用户名无效", True

        target = str(stream_id or "").strip()
        if not target:
            await self._reply(stream_id, "这条命令没有可用的聊天流，无法订阅。")
            return False, "缺少 stream_id", True

        state = self._require_state()
        state.stream_entry(target, self._stream_labels.get(target, ""))
        lines: list[str] = []
        if problems:
            lines.append("⚠️ 已跳过：" + "；".join(problems[:3]))

        for handle in handles[:MAX_HANDLES_PER_MESSAGE]:
            entry, is_new = await self._ensure_subscribed(handle)
            stream_list = entry.setdefault("streams", [])
            if target in stream_list:
                lines.append(f"• @{handle} 已经在订阅列表里了")
                continue

            if is_new:
                ok, message = await self._bootstrap_new_handle(handle, entry)
                if not ok:
                    state.handles.pop(handle, None)
                    lines.append(f"• @{handle} 订阅失败：{message}")
                    continue

            stream_list.append(target)
            display = entry.get("display_name") or entry.get("screen_name") or handle
            lines.append(f"• 已订阅 {display} (@{handle})")

            if is_new and self.config.push.push_latest_on_subscribe:
                preview = await self._push_latest(handle, target, count=1)
                if preview:
                    lines.append("　└ 已把最新一条推文推送到本聊天")

        self._save_state()
        await self._reply(stream_id, "🐦 订阅结果\n" + "\n".join(lines))
        return True, f"处理了 {len(handles)} 个订阅", True

    async def _bootstrap_new_handle(self, handle: str, entry: dict[str, Any]) -> tuple[bool, str]:
        """首次订阅时校验账号并建立基线。"""

        client = self._client
        if client is None:
            return False, "插件未启动（配置里可能是禁用状态）"

        try:
            profile = await client.get_profile(handle)
        except Exception as exc:
            return False, f"找不到这个推主（{exc}）"

        entry["screen_name"] = str(profile.get("screen_name") or handle)
        entry["display_name"] = str(profile.get("name") or "")
        entry["avatar_url"] = str(profile.get("avatar_url") or "")
        entry["protected"] = bool(profile.get("protected"))
        entry["baseline_at"] = time.time()

        if entry["protected"]:
            return False, "该账号是私密账号，看不到推文"

        fetch_count = max(1, min(50, int(self.config.poll.fetch_count)))
        try:
            tweets = await client.fetch_timeline(handle, fetch_count)
        except Exception as exc:
            # 资料能拿到但时间线失败时，仍允许订阅（下一轮再补基线）
            self._get_logger().warning("推主 @%s 首次拉取时间线失败: %s", handle, exc)
            return True, "已订阅（首次拉取时间线失败，将在下轮重试）"

        entry["seen_ids"] = self._bounded_ids([], [item.id for item in tweets])
        entry["last_seen_ts"] = max((item.created_ts for item in tweets), default=time.time())
        return True, "ok"

    @Command(
        "tw_unsub",
        description="取消当前聊天对某个推主的订阅",
        pattern=_LEAD + r"tw_unsub\b(?:\s+(?P<handles>.+?))?\s*$",
    )
    async def handle_unsub(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_unsub 命令。"""

        if not self._is_allowed(kwargs):
            await self._reply(stream_id, self._deny_message())
            return False, "没有权限", True

        handles, problems = await self._resolve_handles(
            str((kwargs.get("matched_groups") or {}).get("handles") or ""),
            stream_id,
            "用法：/tw_unsub <用户名>，例如 /tw_unsub elonmusk",
            "/tw_unsub",
        )
        if not handles:
            return False, "用户名无效", True

        state = self._require_state()
        target = str(stream_id or "").strip()
        lines: list[str] = []
        if problems:
            lines.append("⚠️ 已跳过：" + "；".join(problems[:3]))

        for handle in handles[:MAX_HANDLES_PER_MESSAGE]:
            entry = state.handles.get(handle)
            if not isinstance(entry, dict):
                hint = self._close_match(handle)
                lines.append(f"• @{handle} 本来就没订阅" + (f"，你是不是想取消 @{hint}？" if hint else ""))
                continue
            stream_list = entry.get("streams") or []
            if target not in stream_list:
                lines.append(f"• @{handle} 没有被本聊天订阅")
                continue
            stream_list.remove(target)
            if stream_list:
                lines.append(f"• 已取消 @{handle}（仍有 {len(stream_list)} 个聊天在订阅）")
            else:
                state.handles.pop(handle, None)
                lines.append(f"• 已取消 @{handle}（已无聊天订阅，订阅记录一并删除）")

        self._save_state()
        await self._reply(stream_id, "🐦 退订结果\n" + "\n".join(lines))
        return True, "处理了退订", True

    @Command("tw_list", description="查看当前聊天的推特订阅", pattern=_LEAD + r"tw_list\s*$")
    async def handle_list(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_list 命令。"""

        if not self._is_allowed(kwargs):
            await self._reply(stream_id, self._deny_message())
            return False, "没有权限", True

        state = self._require_state()
        target = str(stream_id or "").strip()
        stream_entry = state.streams.get(target) or {}
        paused = bool(stream_entry.get("paused"))
        subscribed = [
            (handle, entry)
            for handle, entry in sorted(state.handles.items())
            if target in (entry.get("streams") or [])
        ]
        if not subscribed:
            await self._reply(stream_id, "本聊天还没有订阅任何推主，用 /tw_sub <用户名> 添加。")
            return True, "无订阅", True

        lines = [f"🐦 本聊天订阅了 {len(subscribed)} 个推主（推送{'已暂停' if paused else '已开启'}）"]
        for handle, entry in subscribed[:30]:
            display = entry.get("display_name") or handle
            last_new = float(entry.get("last_new_at") or 0.0)
            suffix = f"，上次新推 {format_ago(last_new)}" if last_new else ""
            lines.append(f"• {display} (@{handle}){suffix}")
        if len(subscribed) > 30:
            lines.append(f"… 还有 {len(subscribed) - 30} 个")
        lines.append("用 /tw_unsub <用户名> 退订，/tw_off 可暂停推送。")
        await self._reply(stream_id, "\n".join(lines))
        return True, "已列出订阅", True

    @Command("tw_all", description="查看全部推特订阅与推送目标", pattern=_LEAD + r"tw_all\s*$")
    async def handle_all(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_all 命令。"""

        if not self._is_allowed(kwargs, cross_chat=True):
            await self._reply(stream_id, self._deny_message(cross_chat=True))
            return False, "没有权限", True

        state = self._require_state()
        if not state.handles:
            await self._reply(stream_id, "目前没有任何订阅。")
            return True, "无订阅", True

        lines = [f"🐦 全部订阅：{len(state.handles)} 个推主"]
        for handle, entry in sorted(state.handles.items()):
            display = entry.get("display_name") or handle
            streams = entry.get("streams") or []
            labels = "、".join(self._stream_label(str(item)) for item in streams[:3]) or "无推送目标"
            if len(streams) > 3:
                labels += f" 等 {len(streams)} 个"
            lines.append(f"• {display} (@{handle}) → {labels}")
            if entry.get("last_error"):
                lines.append(f"　└ ⚠️ {truncate_text(str(entry.get('last_error')), 80)}")
        extra = [str(item) for item in (self.config.push.extra_streams or []) if str(item).strip()]
        if extra:
            lines.append("固定推送目标：" + "、".join(self._stream_label(item) for item in extra))
        await self._reply(stream_id, "\n".join(lines[:60]))
        return True, "已列出全部订阅", True

    @Command("tw_on", description="恢复当前聊天的推特推送", pattern=_LEAD + r"tw_on\s*$")
    async def handle_on(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_on 命令。"""

        if not self._is_allowed(kwargs):
            await self._reply(stream_id, self._deny_message())
            return False, "没有权限", True
        state = self._require_state()
        target = str(stream_id or "").strip()
        entry = state.stream_entry(target, self._stream_labels.get(target, ""))
        entry["paused"] = False
        self._save_state()
        await self._reply(stream_id, "✅ 已恢复本聊天的推特推送。")
        return True, "已开启推送", True

    @Command("tw_off", description="暂停当前聊天的推特推送", pattern=_LEAD + r"tw_off\s*$")
    async def handle_off(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_off 命令。"""

        if not self._is_allowed(kwargs):
            await self._reply(stream_id, self._deny_message())
            return False, "没有权限", True
        state = self._require_state()
        target = str(stream_id or "").strip()
        entry = state.stream_entry(target, self._stream_labels.get(target, ""))
        entry["paused"] = True
        self._save_state()
        await self._reply(stream_id, "⏸ 已暂停本聊天的推特推送（订阅关系保留，/tw_on 恢复）。")
        return True, "已暂停推送", True

    # -- 命令：运行控制 ---------------------------------------------------

    @Command(
        "tw_check",
        description="立刻检查一次推特更新",
        pattern=_LEAD + r"tw_check\b(?:\s+(?P<handle>\S+))?\s*$",
    )
    async def handle_check(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_check 命令。"""

        if not self._is_allowed(kwargs, cross_chat=True):
            await self._reply(stream_id, self._deny_message(cross_chat=True))
            return False, "没有权限", True

        state = self._require_state()
        raw = str((kwargs.get("matched_groups") or {}).get("handle") or "").strip()
        if raw:
            handle = normalize_handle(raw)
            if not handle:
                await self._reply(stream_id, "❌ " + describe_handle_problem(raw))
                return False, "用户名无效", True
            if handle not in state.handles:
                hint = self._close_match(handle)
                message = f"没有订阅 @{handle}，先用 /tw_sub {handle} 订阅。"
                if hint:
                    message = f"没有订阅 @{handle}，你是不是想查 @{hint}？"
                await self._reply(stream_id, message)
                return False, "未订阅", True
            targets: Optional[list[str]] = [handle]
        else:
            if not state.handles:
                await self._reply(stream_id, "还没有订阅任何推主。")
                return False, "无订阅", True
            targets = None

        await self._reply(stream_id, "🔍 正在检查推特更新，请稍候…")
        if self._poll_lock.locked():
            await self._reply(stream_id, "上一轮检查还在进行中，稍后再试。")
            return True, "轮询被跳过", True

        results = await self._poll_once(handles=targets)
        if not results:
            await self._reply(stream_id, "这次没有可检查的推主，用 /tw_sub 订阅后再试。")
            return True, "没有可检查的目标", True

        pushed = sum(item.pushed for item in results)
        lines = [f"✅ 检查完成：{len(results)} 个推主，新增推送 {pushed} 条"]
        for item in results:
            if item.error:
                lines.append(f"• @{item.handle} 失败：{truncate_text(item.error, 90)}")
        if pushed == 0:
            lines.append("（没有发现新推文）")
        await self._reply(stream_id, "\n".join(lines))
        return True, "检查完成", True

    @Command(
        "tw_test",
        description="立刻推送某个推主的最新推文用于预览",
        pattern=_LEAD + r"tw_test\b(?:\s+(?P<rest>.+?))?\s*$",
    )
    async def handle_test(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_test 命令：拉取最新推文并直接推送到当前聊天。"""

        if not self._is_allowed(kwargs):
            await self._reply(stream_id, self._deny_message())
            return False, "没有权限", True

        raw = str((kwargs.get("matched_groups") or {}).get("rest") or "").strip()
        parts = [item for item in re.split(r"[\s,，]+", raw) if item]
        if not parts:
            await self._reply(stream_id, "没有写用户名哦。\n用法：/tw_test <用户名> [条数]，例如 /tw_test elonmusk 2")
            return False, "缺少用户名", True

        handles, _problems = await self._resolve_handles(
            parts[0],
            stream_id,
            "用法：/tw_test <用户名> [条数]，例如 /tw_test elonmusk 2",
            "/tw_test",
        )
        if not handles:
            return False, "用户名无效", True
        handle = handles[0]

        count = 1
        if len(parts) > 1:
            try:
                count = max(1, min(5, int(parts[1])))
            except ValueError:
                count = 1

        pushed = await self._push_latest(handle, stream_id, count=count)
        if pushed:
            await self._reply(stream_id, f"✅ 已推送 @{handle} 的最新 {pushed} 条推文（不计入订阅状态）。")
        else:
            await self._reply(stream_id, f"没能取到 @{handle} 的推文，检查用户名、网络或代理设置。")
        return True, "测试推送完成", True

    async def _push_latest(self, handle: str, stream_id: str, *, count: int = 1) -> int:
        """拉取指定推主的最新推文并推送到聊天流，不修改订阅状态。

        Returns:
            int: 实际推送成功的条数。
        """

        client = self._client
        target = str(stream_id or "").strip()
        if client is None or not target:
            return 0
        try:
            tweets = await client.fetch_timeline(handle, max(1, min(20, count * 3)))
        except Exception as exc:
            self._get_logger().warning("预览 @%s 的推文失败: %s", handle, exc)
            return 0

        accepted = [item for item in tweets if self._accept_tweet(item)]
        accepted.sort(key=lambda item: item.created_ts, reverse=True)
        # 取最新的 count 条，按时间正序打包成一条聊天记录
        selected = list(reversed(accepted[: max(1, count)]))
        if not selected:
            return 0
        try:
            return len(await self._deliver_many(selected, target))
        except Exception as exc:
            self._get_logger().error("预览推文发送失败: %s", exc, exc_info=True)
            return 0

    @Command(
        "tw_interval",
        description="查看或设置推特轮询间隔（分钟）",
        pattern=_LEAD + r"tw_interval\b(?:\s+(?P<value>\S+))?\s*$",
    )
    async def handle_interval(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_interval 命令。"""

        if not self._is_allowed(kwargs, cross_chat=True):
            await self._reply(stream_id, self._deny_message(cross_chat=True))
            return False, "没有权限", True

        state = self._require_state()
        base = max(1, min(1440, int(self.config.poll.interval_minutes)))
        raw = str((kwargs.get("matched_groups") or {}).get("value") or "").strip()
        override = state.runtime.get("interval_minutes")

        if not raw:
            current = self._effective_interval_minutes()
            source = f"命令覆盖，配置值为 {base} 分钟" if override is not None else "来自配置文件"
            await self._reply(
                stream_id,
                f"⏱ 当前轮询间隔：{current} 分钟（{source}）\n用法：/tw_interval 10 设置间隔，/tw_interval reset 恢复配置值。",
            )
            return True, "查询间隔", True

        if raw.lower() in {"reset", "default", "默认", "重置"}:
            state.runtime.pop("interval_minutes", None)
            self._save_state()
            await self._reply(stream_id, f"✅ 已恢复为配置文件的轮询间隔：{base} 分钟。")
            return True, "已重置间隔", True

        try:
            minutes = int(float(raw))
        except ValueError:
            await self._reply(stream_id, "间隔必须是分钟数（例如 /tw_interval 10）或 reset。")
            return False, "参数无效", True

        minutes = max(1, min(1440, minutes))
        state.runtime["interval_minutes"] = minutes
        self._save_state()
        await self._reply(stream_id, f"✅ 轮询间隔已设为 {minutes} 分钟（保存于插件状态，重启后依然生效）。")
        return True, "已设置间隔", True

    @Command(
        "tw_reset",
        description="重建某个推主的推送基线",
        pattern=_LEAD + r"tw_reset\b(?:\s+(?P<handles>.+?))?\s*$",
    )
    async def handle_reset(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_reset 命令。"""

        if not self._is_allowed(kwargs, cross_chat=True):
            await self._reply(stream_id, self._deny_message(cross_chat=True))
            return False, "没有权限", True

        handles, problems = await self._resolve_handles(
            str((kwargs.get("matched_groups") or {}).get("handles") or ""),
            stream_id,
            "用法：/tw_reset <用户名>，重置后下次轮询不会补推历史推文。",
            "/tw_reset",
        )
        if not handles:
            return False, "用户名无效", True

        state = self._require_state()
        lines: list[str] = []
        if problems:
            lines.append("⚠️ 已跳过：" + "；".join(problems[:3]))
        for handle in handles[:MAX_HANDLES_PER_MESSAGE]:
            entry = state.handles.get(handle)
            if not isinstance(entry, dict):
                hint = self._close_match(handle)
                lines.append(f"• @{handle} 没有订阅记录" + (f"，你是不是想重置 @{hint}？" if hint else ""))
                continue
            entry["seen_ids"] = []
            entry["last_seen_ts"] = 0.0
            lines.append(f"• @{handle} 基线已重建")
        self._save_state()
        await self._reply(stream_id, "🐦 重置结果\n" + "\n".join(lines))
        return True, "已重置基线", True

    @Command(
        "tw_del",
        description="彻底删除某个推主的全部订阅",
        pattern=_LEAD + r"tw_del\b(?:\s+(?P<handles>.+?))?\s*$",
    )
    async def handle_del(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_del 命令。"""

        if not self._is_allowed(kwargs, cross_chat=True):
            await self._reply(stream_id, self._deny_message(cross_chat=True))
            return False, "没有权限", True

        handles, problems = await self._resolve_handles(
            str((kwargs.get("matched_groups") or {}).get("handles") or ""),
            stream_id,
            "用法：/tw_del <用户名>，会删除该推主在所有聊天的订阅。",
            "/tw_del",
        )
        if not handles:
            return False, "用户名无效", True

        state = self._require_state()
        lines: list[str] = []
        if problems:
            lines.append("⚠️ 已跳过：" + "；".join(problems[:3]))
        for handle in handles[:MAX_HANDLES_PER_MESSAGE]:
            if state.handles.pop(handle, None) is None:
                hint = self._close_match(handle)
                lines.append(f"• @{handle} 没有订阅记录" + (f"，你是不是想删除 @{hint}？" if hint else ""))
            else:
                lines.append(f"• 已删除 @{handle} 的全部订阅")
        self._save_state()
        await self._reply(stream_id, "🐦 删除结果\n" + "\n".join(lines))
        return True, "已删除订阅", True

    @Command("tw_status", description="查看推特转发插件运行状态", pattern=_LEAD + r"tw_status\s*$")
    async def handle_status(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_status 命令。"""

        if not self._is_allowed(kwargs):
            await self._reply(stream_id, self._deny_message())
            return False, "没有权限", True

        state = self._require_state()
        target = str(stream_id or "").strip()
        stream_entry = state.streams.get(target) or {}
        subscribed_here = sum(1 for entry in state.handles.values() if target in (entry.get("streams") or []))
        interval = self._effective_interval_minutes()
        source = "命令覆盖" if state.runtime.get("interval_minutes") is not None else "配置文件"

        lines = [
            "🐦 推特转发运行状态",
            f"• 插件状态：{'已启用' if self.config.plugin.enabled else '已禁用'}",
            f"• 订阅推主：{len(state.handles)} 个（本聊天 {subscribed_here} 个）",
            f"• 本聊天推送：{'已暂停' if stream_entry.get('paused') else '已开启'}",
            f"• 轮询间隔：{interval} 分钟（{source}）",
            f"• 上次轮询：{format_ago(self.last_poll_at)}",
            f"• 上次结果：{self.last_poll_summary}",
            f"• 数据源：{self.config.twitter.api_base}",
            f"• 媒体代理：{self.config.twitter.proxy or '未配置'}",
        ]
        if self._client is None:
            lines.append("• ⚠️ 网络客户端未初始化，可能是配置里禁用了插件或缺少 aiohttp")
        await self._reply(stream_id, "\n".join(lines))
        return True, "状态已发送", True

    @Command("tw_help", description="显示推特转发插件帮助", pattern=_LEAD + r"tw(?:_help)?\s*$")
    async def handle_help(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /tw_help 命令。"""

        if not self._is_allowed(kwargs):
            await self._reply(stream_id, self._deny_message())
            return False, "没有权限", True
        await self._reply(stream_id, self._help_text())
        return True, "帮助已发送", True

    # -- Tool / HomeCard --------------------------------------------------

    @Tool(
        "twitter_latest",
        description="查询某个 X/Twitter 推主的最新推文内容",
        parameters=[
            ToolParameterInfo(name="handle", param_type=ToolParamType.STRING, description="推主用户名，例如 elonmusk", required=True),
            ToolParameterInfo(name="count", param_type=ToolParamType.INTEGER, description="返回条数，默认 3，最多 10", required=False),
        ],
    )
    async def tool_twitter_latest(self, handle: str = "", count: int = 3, **kwargs: Any) -> dict[str, Any]:
        """供 LLM 调用的推特查询工具。"""

        del kwargs
        normalized = normalize_handle(str(handle or ""))
        if not normalized:
            return {"name": "twitter_latest", "content": f"无法识别用户名：{handle}"}

        client = self._client
        if client is None:
            return {"name": "twitter_latest", "content": "推特转发插件当前未启用，无法查询。"}

        try:
            limit = max(1, min(10, int(count or 3)))
        except (TypeError, ValueError):
            limit = 3

        try:
            tweets = await client.fetch_timeline(normalized, max(limit * 2, 10))
        except Exception as exc:
            return {"name": "twitter_latest", "content": f"查询 @{normalized} 失败：{exc}"}

        if not tweets:
            return {"name": "twitter_latest", "content": f"@{normalized} 最近没有可读取的推文。"}

        tweets.sort(key=lambda item: item.created_ts, reverse=True)
        chunks: list[str] = []
        for tweet in tweets[:limit]:
            body = truncate_text(tweet.text, 300) or "（无正文）"
            chunks.append(f"[{format_time(tweet.created_ts)}] {body}\n{tweet.url}")
        return {"name": "twitter_latest", "content": f"@{normalized} 的最新推文：\n\n" + "\n\n".join(chunks)}

    @HomeCard(
        "twitter_forwarder_card",
        title="推特转发",
        description="查看订阅的推主与轮询状态，并进入插件配置。",
        content=[
            {
                "type": "markdown",
                "content": "按固定间隔轮询订阅推主的最新推文，发现新推就转发到群里，并支持用斜杠命令直接管理订阅。",
            },
            {
                "type": "key_value",
                "entries": {
                    "默认轮询间隔": "10 分钟（可用 /tw_interval 调整）",
                    "数据源": "api.fxtwitter.com",
                    "订阅命令": "/tw_sub <用户名>",
                    "查看状态": "/tw_status",
                },
            },
            {
                "type": "list",
                "items": [
                    "/tw_sub 订阅推主　/tw_unsub 退订　/tw_list 查看本群订阅",
                    "/tw_check 立刻检查　/tw_test 预览最新推文",
                    "/tw_interval 调整轮询间隔　/tw_all 查看全部订阅",
                ],
            },
            {
                "type": "actions",
                "actions": [
                    {"label": "打开插件配置", "url": "/plugin-config?plugin=polarbear.twitter-forwarder"},
                    {"label": "查看插件列表", "url": "/plugins"},
                ],
            },
        ],
        link_url="/plugin-config?plugin=polarbear.twitter-forwarder",
        link_label="配置推特转发",
        width="medium",
        order=140,
    )
    async def home_card(self) -> None:
        """WebUI 首页卡片。"""

        return None


def create_plugin() -> TwitterForwarderPlugin:
    """创建插件实例。"""

    return TwitterForwarderPlugin()
