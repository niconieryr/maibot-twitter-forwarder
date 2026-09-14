"""真机验证：大视频「容器借道」链路是否打通（1.6.1 修的 bug）。

不往 QQ 发任何消息，只验证：
  1) find_docker() 在本机（systemd 服务的 PATH 环境）能找到 /snap/bin/docker；
  2) 真实视频经代理下载到宿主；
  3) docker cp 进 SnowLuma 容器后，容器内确实能看到这个文件（大小一致）；
  4) 清理路径能把容器内文件删掉。

    ~/maimai/MaiBot/.venv/bin/python check_docker_route.py [handle ...]
    # 想模拟「服务里没有 /snap/bin」的情形，可以先跑：
    #   env PATH=/usr/local/bin:/usr/bin:/bin ... check_docker_route.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("tw_docker_check", BASE / "plugin.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("check-docker")

HANDLES = sys.argv[1:] or ["limbuscompany_b", "LimbusCompany_B", "opencode"]
CONTAINER = "snowluma"

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


def docker_ls(path: str) -> str:
    """在容器里看文件；返回 ``ls -l`` 原始输出。"""

    exe = mod.find_docker() or "docker"
    try:
        out = subprocess.run(
            [exe, "exec", CONTAINER, "ls", "-l", path],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:  # pragma: no cover
        return f"<异常 {exc}>"
    return (out.stdout or out.stderr).strip()


def docker_size(path: str) -> int:
    """容器内文件大小；拿不到返回 -1。"""

    exe = mod.find_docker() or "docker"
    try:
        out = subprocess.run(
            [exe, "exec", CONTAINER, "stat", "-c", "%s", path],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # pragma: no cover
        return -1
    text = (out.stdout or "").strip()
    return int(text) if text.isdigit() else -1


async def main() -> int:
    # 宿主临时文件放在家目录下：snap 版 docker 读得到（和插件真实 data_dir 同类路径）
    data_dir = Path.home() / "tw-docker-check"
    data_dir.mkdir(parents=True, exist_ok=True)
    # snap 版 docker 有私有 /tmp，宿主 /tmp 里的文件它 lstat 不到 —— 专门用它验证 stdin 兜底
    tmp_dir = Path(tempfile.gettempdir()) / "tw-docker-check"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    print("=== 1) docker 可执行文件查找 ===")
    print(f"  当前 PATH = {os.environ.get('PATH', '')}")
    exe = mod.find_docker(refresh=True)
    check(bool(exe), f"find_docker() 找到 docker：{exe}")
    if not exe:
        print("\n找不到 docker，后面的链路没法验证。")
        return 1
    print(f"  {subprocess.run([exe, 'version', '--format', '{{.Server.Version}}'], capture_output=True, text=True).stdout.strip()}")

    plugin = mod.create_plugin()
    cfg = plugin.get_default_config()
    # 关键：把内联上限压到几乎为 0，强制走容器借道
    cfg["media"]["inline_video_mb"] = 0.01
    cfg["media"]["max_video_mb"] = 300
    cfg["media"]["big_video_docker_route"] = True
    cfg["media"]["docker_container"] = CONTAINER
    cfg["media"]["video_timeout_seconds"] = 300
    cfg["twitter"]["proxy"] = "http://127.0.0.1:7890"
    plugin.set_plugin_config(cfg)
    plugin._set_context(FakeCtx(data_dir))
    client = plugin._build_client()
    plugin._client = client

    try:
        tweet = None
        for handle in HANDLES:
            print(f"\n=== 2) 拉取 @{handle} 的推文找带视频的 ===")
            try:
                tweets = await client.fetch_timeline(handle, 40)
            except Exception as exc:
                print(f"  拉取失败：{exc}")
                continue
            videos = [item for item in tweets if item.has_video]
            print(f"  共 {len(tweets)} 条，其中 {len(videos)} 条含视频")
            for item in videos:
                media = item.video_media()[0]
                picked = await plugin._pick_video_candidate(item, 300 * 1024 * 1024)
                if picked is None:
                    continue
                print(f"  → 选中 {item.id}（{picked[1] / 1024 / 1024:.2f} MB，时长 {media.duration}s）")
                tweet = item
                break
            if tweet is not None:
                break

        if tweet is None:
            print("\n没找到可用的视频推文，跳过链路验证。")
            return 1

        print("\n=== 3) 走 _prepare_video（内联上限 0.01MB → 必然走容器） ===")
        payload = await plugin._prepare_video(tweet)
        check(payload is not None, "拿到了视频 payload（说明容器链路成功）")
        if payload is None:
            return 1
        check(not payload.is_inline, f"没有走内联（is_inline={payload.is_inline}）")
        check(bool(payload.container_path), f"使用容器内路径：{payload.container_path}")
        check(payload.data.get("file") == payload.container_path, f"发给适配器的是容器内路径：{payload.data}")
        host_size = payload.host_path.stat().st_size if payload.host_path and payload.host_path.exists() else -1
        check(host_size > 0, f"宿主临时文件已就位：{host_size / 1024 / 1024:.2f} MB")

        ls_out = docker_ls(payload.container_path)
        print(f"  容器内 ls -l → {ls_out}")
        check("No such file" not in ls_out and "cannot access" not in ls_out, "容器里确实有这个视频文件")
        inner_size = docker_size(payload.container_path)
        check(inner_size == host_size, f"容器内大小与宿主一致（{inner_size} vs {host_size}）")

        print("\n=== 4) docker cp 读不到文件时（snap 私有 /tmp）退回 stdin 管道 ===")
        tmp_copy = tmp_dir / f"{tweet.id}-tmpcopy.mp4"
        shutil.copy2(payload.host_path, tmp_copy)
        print(f"  宿主副本：{tmp_copy}（{tmp_copy.stat().st_size / 1024 / 1024:.2f} MB）")
        tmp_target = f"/app/data/twvideo/{tmp_copy.name}"
        tmp_payload = mod.VideoPayload(
            data={"file": tmp_target}, size=0, url=payload.url,
            container=CONTAINER, container_path=tmp_target, host_path=tmp_copy,
        )
        ok = await plugin._docker_cp(tmp_copy, CONTAINER, tmp_target)
        check(ok, "从 /tmp 拷贝时 stdin 管道兜底成功")
        tmp_size = docker_size(tmp_target)
        check(tmp_size == tmp_copy.stat().st_size, f"兜底写进容器的文件大小一致（{tmp_size}）")

        print("\n=== 5) 清理容器内与宿主临时文件 ===")
        await plugin._cleanup_video_payload(payload)
        ls_out2 = docker_ls(payload.container_path)
        print(f"  清理后 ls -l → {ls_out2}")
        check("No such file" in ls_out2 or "cannot access" in ls_out2, "容器内文件已被删除")
        check(not (payload.host_path and payload.host_path.exists()), "宿主临时文件已被删除")
        await plugin._cleanup_video_payload(tmp_payload)
        check("No such file" in docker_ls(tmp_target) or "cannot access" in docker_ls(tmp_target), "兜底路径的容器文件也被清理")
        check(not tmp_copy.exists(), "兜底用的 /tmp 副本也被删除")
    finally:
        await client.close()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print("  -", item)
        return 1
    print("容器借道链路全部通过 ✅")
    return 0


sys.exit(asyncio.run(main()))
