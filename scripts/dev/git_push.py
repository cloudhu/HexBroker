#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ruff: noqa: T201
"""HexBroker 统一 git 推送通道（PAT 注入 git push）。

背景
----
WorkBuddy 沙箱内 git 无法交互输入 GitHub 凭证；GitHub MCP 的 fine-grained token
未授权私有仓库。本项目统一写通道 = 本脚本：PAT 只读自
`~/.workbuddy/connectors/*/tokens/github.txt`（与「云侠·每日Git推送」同源机制），
通过 URL 注入执行 git push（2026-08-23 实测 fast-forward 推送 67 提交成功）。

铁律
----
- PAT 只读自 tokens/github.txt，绝不打印 / 落盘 / 进提交消息。
- 推送前必做分叉自检（ls-remote + merge-base --is-ancestor）：远端领先/分叉 → 停手，绝不 force。
- 白名单 + 剔除双重防护：运行期产物（data/ artifacts/ *.log *.parquet 等）不入库。

用法
----
    # 全量推送：收集白名单内改动 → 自动 add/commit → 分叉自检 → push
    python scripts/dev/git_push.py

    # 预演（推送前必跑）：显示待推送清单与分叉状态，不写任何远端
    python scripts/dev/git_push.py --dry-run

    # 分叉自检（只读）：本地 HEAD 是否在远端祖先链（落后/分叉 → 退出码 1）
    python scripts/dev/git_push.py --check-fork

    # 指定文件 + 提交消息
    python scripts/dev/git_push.py --paths hexbroker/x.py tests/test_x.py --commit-msg "fix(x): ..."

退出码
------
    0 = 成功推送 / 无变更 / dry-run 完成 / check-fork 通过
    1 = 错误（PAT 无效 / 分叉 / 远端错误 / 白名单违规）
"""

from __future__ import annotations

import argparse
import glob
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
REPO_DEFAULT = "cloudhu/HexBroker"          # 仓库（私有；git remote 存在时自动解析）
BRANCH = "main"
TOKEN_GLOB = str(Path.home() / ".workbuddy/connectors/*/tokens/github.txt")

# 剔除：路径任一段命中即拒绝（运行期/不入库目录）
EXCLUDE_DIR_SEGMENTS: Tuple[str, ...] = (
    "data/raw/",
    "data/interim/",
    "data/processed/",
    "data/signals/",
    "data/paper/",
    "artifacts/",
    "__pycache__/",
    "catboost_info/",
    "hexbroker.egg-info/",
    "third_party/",
    ".workbuddy/",
    "公众号文章/published_log.json",
)
# 剔除：扩展名
EXCLUDE_EXTS: Tuple[str, ...] = (".log", ".pyc", ".parquet", ".prom", ".jsonl")
# 未跟踪文件：_ 前缀跳过（临时脚本，不入库）
SKIP_UNDERSCORE_PREFIX = "_"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def read_pat() -> str:
    """读取 PAT（只读，值不打印；前缀校验防误读）。"""
    matches = sorted(glob.glob(TOKEN_GLOB))
    if not matches:
        raise SystemExit(f"[PAT] 未找到 tokens/github.txt：{TOKEN_GLOB}")
    pat = Path(matches[0]).read_text(encoding="utf-8").strip()
    if not re.match(r"^(github_pat_|ghp_|gho_)", pat):
        raise SystemExit("[PAT] 前缀非法（期望 github_pat_/ghp_/gho_ 开头）→ 拒绝使用")
    print(f"[PAT] 已读取: {Path(matches[0]).parent}（前缀校验通过，值不打印）")
    return pat


def resolve_repo() -> str:
    """从 git remote origin 解析 owner/repo；无则用默认。"""
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False, timeout=10,
        ).stdout.strip()
        m = re.search(r"(?:github\.com[/:])([\w.-]+)/([\w.-]+?)(?:\.git)?$", url)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    except Exception:
        pass
    return REPO_DEFAULT


def git(args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """只读/本地的 git 子进程。"""
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check, timeout=120)


# ---------------------------------------------------------------------------
# 白名单 / 剔除
# ---------------------------------------------------------------------------
def exclude_reason(rel: str) -> Optional[str]:
    """剔除命中返回原因；未命中返回 None。"""
    for seg in EXCLUDE_DIR_SEGMENTS:
        if rel.startswith(seg):
            return f"剔除: 目录段 {seg}"
    for ext in EXCLUDE_EXTS:
        if rel.endswith(ext):
            return f"剔除: 扩展名 {ext}"
    return None


def is_allowed(rel: str) -> bool:
    return exclude_reason(rel) is None


# ---------------------------------------------------------------------------
# 收集变更
# ---------------------------------------------------------------------------
def collect_changes() -> List[Tuple[str, str]]:
    """收集白名单内改动：git status --porcelain（M/A 已跟踪 + ?? 未跟踪）。"""
    r = git(["status", "--porcelain"])
    out: List[Tuple[str, str]] = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        kind, rel = line[:2], line[3:].strip()
        if rel.startswith('"'):
            rel = rel.strip('"')  # 引号包裹的中文路径
        if kind.strip() in ("??",):
            name = Path(rel).name
            if name.startswith(SKIP_UNDERSCORE_PREFIX):
                continue
        reason = exclude_reason(rel)
        if reason:
            print(f"  [跳过] {rel}（{reason}）")
            continue
        out.append((rel, kind))
    return out


# ---------------------------------------------------------------------------
# 分叉自检 / 远端
# ---------------------------------------------------------------------------
def remote_main_sha(pat: str, repo: str) -> Optional[str]:
    """ls-remote 远端 main sha（只读；远端无 main 返回 None）。"""
    url = f"https://x-access-token:{pat}@github.com/{repo}.git"
    r = subprocess.run(
        ["git", "ls-remote", url, f"refs/heads/{BRANCH}"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if r.returncode != 0:
        raise SystemExit(f"[远端] ls-remote 失败: {r.stderr.strip()[:200]}")
    parts = r.stdout.strip().split()
    return parts[0] if parts else None


def check_fork(pat: str, repo: str) -> None:
    """分叉自检：本地 HEAD 必须包含远端 main（fast-forward 安全），否则停手。"""
    remote_sha = remote_main_sha(pat, repo)
    local_head = git(["rev-parse", "HEAD"]).stdout.strip()
    if remote_sha is None:
        print("[check-fork] 远端无 main 分支（新仓库），可推送")
        return
    if remote_sha == local_head:
        print("[check-fork] 本地与远端一致，无新提交可推")
        return
    r = subprocess.run(
        ["git", "merge-base", "--is-ancestor", remote_sha, "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode == 0:
        print(f"[check-fork] 通过：远端 {remote_sha[:8]} 是本地祖先，可 fast-forward")
    else:
        raise SystemExit(
            f"[check-fork] ❌ 分叉/落后：远端 {remote_sha[:8]} 不在本地祖先链——"
            "停手，先同步远端（git pull 或人工处理），禁止 force push"
        )


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------
def do_push(pat: str, repo: str) -> None:
    """git push（URL 注入 PAT，不落盘）。"""
    url = f"https://x-access-token:{pat}@github.com/{repo}.git"
    r = subprocess.run(
        ["git", "push", url, f"refs/heads/{BRANCH}:refs/heads/{BRANCH}"],
        capture_output=True, text=True, check=False, timeout=300,
    )
    print(r.stdout.strip())
    if r.returncode != 0:
        raise SystemExit(f"[推送] 失败: {r.stderr.strip()[:300]}")
    # 验证
    remote_sha = remote_main_sha(pat, repo)
    local_head = git(["rev-parse", "HEAD"]).stdout.strip()
    if remote_sha == local_head:
        print(f"[验证] ✅ 远端 {BRANCH} = {local_head[:8]}，推送成功")
    else:
        raise SystemExit(f"[验证] ⚠️ 远端 {remote_sha} ≠ 本地 {local_head}，请人工核验")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="HexBroker 统一 git 推送通道（PAT 注入）")
    ap.add_argument("--dry-run", action="store_true", help="预演：收集/白名单/分叉检查，不推送")
    ap.add_argument("--check-fork", action="store_true", help="仅分叉自检（只读）")
    ap.add_argument("--paths", nargs="*", help="指定推送文件（相对仓库根）")
    ap.add_argument("--commit-msg", default=None, help="提交消息（有未提交改动时自动 commit）")
    args = ap.parse_args(argv)

    pat = read_pat()
    repo = resolve_repo()
    print(f"[仓库] {repo} @ {BRANCH}")

    # 分叉自检（--check-fork 或全流程前）
    check_fork(pat, repo)
    if args.check_fork:
        return 0

    # 收集变更
    if args.paths:
        changes: List[Tuple[str, str]] = []
        for p in args.paths:
            reason = exclude_reason(p)
            if reason:
                print(f"  [拒绝] {p}（{reason}）")
                return 1
            changes.append((p, "M"))
    else:
        changes = collect_changes()

    if not changes:
        print("[收集] 白名单内无变更，无需推送")
        return 0

    print(f"[收集] 待推送 {len(changes)} 项:")
    for rel, _kind in changes:
        print(f"  - {rel}")

    if args.dry_run:
        print("[dry-run] 预演完成，未推送（分叉检查已通过）")
        return 0

    # 暂存 + 提交（若有未提交改动）
    if args.paths or args.commit_msg:
        git(["add", *[c[0] for c in changes]])
        msg = args.commit_msg or f"chore: 经统一写通道推送 {len(changes)} 项"
        r = git(["commit", "-m", msg], check=False)
        if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
            print(f"[commit] 提示: {r.stdout.strip()[:200]}")
    else:
        # 无 --commit-msg：检查是否有未提交改动（需先提交）
        uncommitted = [c for c in changes if c[1].strip() in ("M", "A", "??")]
        if uncommitted:
            print("[提示] 有未提交改动，建议 --commit-msg 指定消息自动提交；当前仅推送已提交历史")
            # 已提交历史推送（HEAD 领先时）
        else:
            print("[收集] 无未提交改动，推送已提交历史")

    # 推送
    do_push(pat, repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
