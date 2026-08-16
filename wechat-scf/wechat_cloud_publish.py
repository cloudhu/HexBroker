#!/usr/bin/env python3
"""
微信公众号云函数发布客户端 v1.2

完整流程（三段顺序铁律：①LLM 通俗易懂润色 → ②后处理混淆 → ③发布）：
0. （上游）自动化任务中的 LLM 步骤先完成「通俗易懂润色」，产出 *-公众号版.md（如 A1 Step 10.5 / A5 Step 8.6）
1. 后处理 (transform_for_wechat.py) — 中文化策略名/术语、技术通俗化、个股代码与名称混淆、版权声明
2. 渲染 HTML (wenyan render) — 本地渲染，不调微信 API，不需要 IP 白名单
3. 读取封面图 → base64
4. POST 到腾讯云 SCF 云函数（事件函数 + 函数 URL）→ 固定 IP 调微信 API → 创建草稿
5. 返回 media_id

优势：
- 本地只需 wenyan render（不触发 IP 白名单检查）
- 所有微信 API 调用从云函数固定 IP 发出（只需白名单加一次）
- 彻底告别手动更新 IP 白名单

使用方式：
  python wechat_cloud_publish.py -f "文章.md" --title "标题" --cover cover.jpg
  python wechat_cloud_publish.py -f "文章.md" -t lapis -h solarized-light --no-footnote
  python wechat_cloud_publish.py --check-ip          # 查看云函数出口 IP
  python wechat_cloud_publish.py -f "文章.md" --dry-run  # 仅渲染预览不发布

配置：
  首次使用需设置云函数 URL（部署后获得）：
  方式1: 环境变量  set SCF_PUBLISH_URL=https://xxx.gz.tencentscf.com
  方式2: 配置文件  编辑同目录 scf_config.json

  v1.2 更新：改用 SCF 事件函数 + 函数 URL（触发器已下线，函数 URL 是函数级配置）
  函数 URL 格式: https://<app-id>-<url-id>.<region>.tencentscf.com
"""

import argparse
import base64
import datetime
import json
import os
import subprocess
import sys
import time
import re
import shutil
import urllib.request
import urllib.error

# ============ 配置 ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# transform_for_wechat.py 和 output/ 在 skills 目录，不在工作目录——自动查找
SKILLS_PUBLISHER_DIR = os.path.join(
    os.path.expanduser("~"), ".workbuddy", "skills", "wechat-publisher"
)
SKILLS_SCRIPTS_DIR = os.path.join(SKILLS_PUBLISHER_DIR, "scripts")
TRANSFORM_SCRIPT = os.path.join(SCRIPT_DIR, "transform_for_wechat.py")
if not os.path.exists(TRANSFORM_SCRIPT):
    TRANSFORM_SCRIPT = os.path.join(SKILLS_SCRIPTS_DIR, "transform_for_wechat.py")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "scf_config.json")
# output 目录和默认封面在 skills 目录
OUTPUT_DIR = os.path.join(SKILLS_PUBLISHER_DIR, "output", "cloud")
DEFAULT_COVER = os.path.join(SKILLS_PUBLISHER_DIR, "assets", "default-cover.jpg")

# wenyan CLI 路径（优先用托管工作区的版本）
WENYAN_MANAGED = os.path.join(
    os.path.expanduser("~"),
    ".workbuddy", "binaries", "node", "workspace",
    "node_modules", ".bin", "wenyan.cmd"
)

# 微信凭证：仅从环境变量读取；缺失则由配置文件 scf_config.json 提供（L-4 卫生项：
# 绝不在源码中硬编码真实密钥，私钥值只存在于 gitignored 的 scf_config.json / 部署环境变量）。
WECHAT_APP_ID = os.environ.get("WECHAT_APP_ID")
WECHAT_APP_SECRET = os.environ.get("WECHAT_APP_SECRET")


def load_config():
    """加载配置文件（fail-secure：凭证缺失即报错，禁止硬编码兜底）"""
    defaults = {
        "scf_url": "",              # 云函数函数 URL 地址
        "scf_api_key": os.environ.get("SCF_API_KEY"),
        "wechat_app_id": WECHAT_APP_ID,
        "wechat_app_secret": WECHAT_APP_SECRET,
        "default_theme": "lapis",
        "default_highlight": "solarized-light",
        "default_author": "云侠AI量化",
    }

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            defaults.update(user_config)
        except (json.JSONDecodeError, IOError):
            pass

    # 环境变量覆盖
    if os.environ.get("SCF_PUBLISH_URL"):
        defaults["scf_url"] = os.environ["SCF_PUBLISH_URL"]
    if os.environ.get("SCF_API_KEY"):
        defaults["scf_api_key"] = os.environ["SCF_API_KEY"]
    if os.environ.get("WECHAT_APP_ID"):
        defaults["wechat_app_id"] = os.environ["WECHAT_APP_ID"]
    if os.environ.get("WECHAT_APP_SECRET"):
        defaults["wechat_app_secret"] = os.environ["WECHAT_APP_SECRET"]

    # fail-secure：三类凭证缺一不可，缺失即拒绝运行（对齐 mx_moni 的不兜底做法）
    missing = [k for k in ("wechat_app_id", "wechat_app_secret", "scf_api_key")
               if not defaults.get(k)]
    if missing:
        raise RuntimeError(
            "微信发布凭证缺失: " + ", ".join(missing) +
            "。请在环境变量或 scf_config.json 中配置，禁止源码硬编码。"
        )

    return defaults


def save_config(config):
    """保存配置文件"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_wenyan_cmd():
    """获取 wenyan CLI 可执行文件路径"""
    if os.path.exists(WENYAN_MANAGED):
        return WENYAN_MANAGED
    wenyan = shutil.which("wenyan")
    return wenyan if wenyan else "wenyan"


# ============ 步骤函数 ============

def _already_transformed(input_file: str) -> bool:
    """判断输入文件是否已经是转换后的产物（含 frontmatter / 版权声明）。

    用于幂等：若本地 公众号版.md 已被改写为转换后版本，再跑发布管线时跳过
    transform 步骤，避免重复叠加 frontmatter / 版权声明（v1.5 新增）。
    """
    try:
        with open(input_file, "r", encoding="utf-8") as f:
            head = f.read(2000)
        stripped = head.lstrip()
        if stripped.startswith("---") and "title:" in head[:300]:
            return True
        if "本文由「云侠」AI量化交易系统自动生成" in head:
            return True
    except Exception:
        pass
    return False


def step_postprocess(input_file, title=None, cover=None, output_dir=None, no_obfuscate=False):
    """Step 2: 后处理（代码/名称混淆 + 中文润色）

    说明：「通俗易懂」的文章级润色由上游 LLM 步骤完成（A1 Step 10.5 / A5 Step 8.6），
    本步骤是对已润色 Markdown 的「后处理」——调用 transform_for_wechat.py 完成
    中文化、技术通俗化、个股代码与公司名称混淆、版权声明、Frontmatter。
    """
    print("📝 Step 2: 后处理 Markdown (transform_for_wechat.py — 混淆代码/名称)...")

    # v1.5 幂等：输入文件已是转换后产物则直接复用，不重复转换
    if _already_transformed(input_file):
        print("   ℹ️  输入文件已含 frontmatter/版权声明（疑似已转换版本），跳过转换直接复用")
        # 优先用传入 --title；否则从 frontmatter 提取；再否则用文件名兜底，避免返回 None 触发云函数 title[:64] 下标报错
        t = title
        if not t:
            try:
                with open(input_file, "r", encoding="utf-8") as f:
                    _c = f.read(2000)
                _m = re.search(r'^title:\s*(.+)', _c, re.MULTILINE)
                t = _m.group(1).strip() if _m else None
            except Exception:
                t = None
        if not t:
            t = os.path.splitext(os.path.basename(input_file))[0]
        return input_file, t

    if output_dir is None:
        output_dir = OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)

    basename = os.path.splitext(os.path.basename(input_file))[0]
    transformed_file = os.path.join(output_dir, f"{basename}-cloud.md")

    cmd = [sys.executable, TRANSFORM_SCRIPT, input_file, transformed_file]
    if title:
        cmd.extend(["--title", title])
    if cover:
        cmd.extend(["--cover", cover])
    if no_obfuscate:
        cmd.append("--no-obfuscate")

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        print(f"❌ 后处理失败: {result.stderr}")
        return None, None

    print(result.stdout.strip())

    # 从 frontmatter 提取 title
    detected_title = title
    if not detected_title:
        with open(transformed_file, "r", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r'^title:\s*(.+)', content, re.MULTILINE)
        detected_title = m.group(1).strip() if m else os.path.splitext(os.path.basename(input_file))[0]

    return transformed_file, detected_title


def step_render(md_file, theme="lapis", highlight="solarized-light", footnote=False):
    """Step 2: 渲染 HTML (wenyan render)"""
    print("🎨 Step 2: 渲染 HTML (wenyan render)...")

    wenyan = get_wenyan_cmd()

    # wenyan render 在 Windows 上无法处理含中文的路径，复制到纯英文临时文件
    render_file = md_file
    tmp_file = None
    try:
        md_file_str = str(md_file)
        if not md_file_str.isascii():
            import tempfile
            tmp_dir = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills", "wechat-publisher", "output", "cloud")
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_file = os.path.join(tmp_dir, "wenyan_render_input.md")
            shutil.copy2(md_file, tmp_file)
            render_file = tmp_file
            print(f"   ℹ️  路径含中文，已复制到临时文件: {tmp_file}")
    except Exception:
        pass

    cmd = [wenyan, "render", "-f", render_file, "-t", theme, "-h", highlight]
    if not footnote:
        cmd.append("--no-footnote")

    # Windows 上 .cmd 文件需要 shell=True + 字符串命令（list 模式引号处理有 bug）
    is_cmd = str(wenyan).lower().endswith(".cmd")
    if is_cmd:
        cmd_str = " ".join(f'"{c}"' for c in cmd)
        result = subprocess.run(
            cmd_str, capture_output=True, text=True,
            encoding="utf-8", errors="replace", shell=True,
        )
    else:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    if result.returncode != 0:
        print(f"❌ wenyan render 失败 (returncode={result.returncode})")
        if result.stderr:
            print(f"   stderr: {result.stderr[:500]}")
        return None

    html = result.stdout.strip()
    if not html:
        print("❌ wenyan render 输出为空")
        if result.stderr:
            print(f"   stderr: {result.stderr[:500]}")
        return None
    print(f"✅ HTML 渲染完成 ({len(html):,} 字符)")
    return html


def step_prepare_cover(cover_path, input_file):
    """Step 3: 准备封面图 base64"""
    print("🖼️  Step 3: 准备封面图...")

    # 如果未指定，尝试默认路径
    if not cover_path:
        article_dir = os.path.dirname(os.path.abspath(input_file))
        candidates = [
            os.path.join(article_dir, "cover.jpg"),
            os.path.join(article_dir, "assets", "default-cover.jpg"),
            DEFAULT_COVER,
        ]
        for c in candidates:
            if os.path.exists(c):
                cover_path = c
                break

    if cover_path and os.path.exists(cover_path):
        with open(cover_path, "rb") as f:
            img_data = f.read()
        cover_b64 = base64.b64encode(img_data).decode("ascii")
        print(f"✅ 封面图: {cover_path} ({len(img_data):,} bytes → {len(cover_b64):,} base64)")
        return cover_b64, None
    else:
        # 使用网络默认图
        default_url = "https://picsum.photos/1080/864"
        print(f"⚠️  未找到本地封面图，使用网络默认图: {default_url}")
        return None, default_url


def step_publish_scf(config, html, title, cover_b64, cover_url, author, digest):
    """Step 4: POST 到云函数发布"""
    print("☁️  Step 4: 通过云函数发布...")

    scf_url = config["scf_url"]
    if not scf_url:
        print("❌ 未配置云函数 URL！")
        print(f"   请编辑配置文件: {CONFIG_FILE}")
        print(f'   设置 "scf_url" 为云函数函数 URL 地址')
        print(f"   格式: https://xxx.gz.tencentscf.com")
        return None

    # 构造请求
    payload = {
        "html": html,
        "title": title,
        "app_id": config["wechat_app_id"],
        "app_secret": config["wechat_app_secret"],
        "author": author,
        "digest": digest,
    }
    if cover_b64:
        payload["cover_base64"] = cover_b64
    elif cover_url:
        payload["cover_url"] = cover_url

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # 函数 URL 格式: https://xxx.gz.tencentscf.com
    scf_url = config["scf_url"].rstrip("/")
    # 兼容旧格式：去掉 /release 后缀（旧 API 网关）
    if scf_url.endswith("/release"):
        scf_url = scf_url[:-len("/release")]

    req = urllib.request.Request(scf_url + "/", data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=UTF-8")
    req.add_header("Authorization", f"Bearer {config['scf_api_key']}")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        print(f"❌ 云函数返回 HTTP {e.code}: {error_body}")
        try:
            return json.loads(error_body)
        except:
            return {"success": False, "error": error_body}
    except Exception as e:
        print(f"❌ 云函数调用失败: {e}")
        return None


def check_scf_ip(config):
    """检查云函数出口 IP"""
    scf_url = config["scf_url"]
    if not scf_url:
        print("❌ 未配置云函数 URL")
        return None

    # 构造 IP 查询 URL
    # 函数 URL 格式: https://xxx.gz.tencentscf.com
    base = scf_url.rstrip("/")
    # 兼容旧格式：去掉 /release 后缀
    if base.endswith("/release"):
        base = base[:-len("/release")]
    ip_url = base + "/ip"

    try:
        req = urllib.request.Request(ip_url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("ip")
    except Exception as e:
        print(f"❌ 获取 IP 失败: {e}")
        return None


# ============ 发布去重（代码层硬不变量 · 2026-07-17 新增）============
# 公众号 addDraft 每次调用都会新建草稿，绝不覆盖；此前 A8 仅靠 prompt 内 Bash 脚本检查
# published_log.json 做去重，模型一旦漏跑或日志未写成功就会重复发布（07-15 x4、07-17 x2）。
# 这里把去重下沉到代码层：传入 --tag 后启用「每日每标签仅发一次」硬拦截（fail-closed，
# 命中直接 sys.exit(43)，绝不调用微信 API）。日志与发布在同一代码路径原子写入，可靠。
PUBLISH_LOG_PATH = os.path.join(
    os.path.dirname(SCRIPT_DIR),  # wechat-scf 的父目录 = HexBroker 仓库根 (E:\Workspace\HexBroker)
    "公众号文章", "published_log.json",
)
_PUBLISH_LOCK_PATH = PUBLISH_LOG_PATH + ".lock"


def _atomic_write_publish_log(log):
    tmp = PUBLISH_LOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PUBLISH_LOG_PATH)


_LOCK_STALE_SEC = 300  # 锁文件超过该时长视为陈旧（持锁进程被 kill 导致 finally 未执行）


def _with_publish_lock(fn):
    """简易跨平台文件锁，防止并发写 published_log.json 竞争（每日单次运行基本不会触发）。

    2026-08-03 修复：原实现无 stale-lock 回收。08-02 一次进程中断残留了
    published_log.json.lock → 次日（08-03 A1）发布成功后 record_publish 抛
    FileExistsError，去重记录未落盘 → --tag 守卫形同虚设（可重复推送草稿）。
    现增加陈旧锁自动回收。
    """
    deadline = time.time() + 10
    while True:
        try:
            fd = os.open(_PUBLISH_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            break
        except FileExistsError:
            # 陈旧锁回收：mtime 超过阈值即判定持锁进程已死，强制清除后重试
            try:
                age = time.time() - os.path.getmtime(_PUBLISH_LOCK_PATH)
                if age > _LOCK_STALE_SEC:
                    print(f"   ⚠️  检测到陈旧锁（{int(age)}s 未释放），自动回收: {_PUBLISH_LOCK_PATH}")
                    os.remove(_PUBLISH_LOCK_PATH)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise
            time.sleep(0.2)
    try:
        fn()
    finally:
        os.close(fd)
        try:
            os.remove(_PUBLISH_LOCK_PATH)
        except OSError:
            pass


def already_published_today(tag: str) -> bool:
    """tag 今日是否已发布（仅当 published_log.json 含 date==今天 且 tag==tag 的记录）。"""
    if not os.path.exists(PUBLISH_LOG_PATH):
        return False
    try:
        with open(PUBLISH_LOG_PATH, "r", encoding="utf-8") as f:
            log = json.load(f)
    except (json.JSONDecodeError, IOError):
        return False
    today = datetime.date.today().isoformat()
    return any(
        isinstance(r, dict) and r.get("date") == today and r.get("tag") == tag
        for r in log
    )


def record_publish(tag: str, title: str, file_path: str, media_id: str) -> None:
    """发布成功后原子追加一条去重记录（与发布在同一代码路径，确保不漏写）。"""
    def _do():
        try:
            log = json.load(open(PUBLISH_LOG_PATH, "r", encoding="utf-8")) if os.path.exists(PUBLISH_LOG_PATH) else []
        except (json.JSONDecodeError, IOError):
            log = []
        if not isinstance(log, list):
            log = []
        log.append({
            "date": datetime.date.today().isoformat(),
            "tag": tag,
            "title": title,
            "file": file_path,
            "media_id": media_id,
            "published_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        _atomic_write_publish_log(log)

    # 发布已成功，记账绝不能静默失败：加锁失败则降级为无锁直写并醒目告警。
    # （漏记 = 次日/重复触发时 --tag 守卫失效 → 重复推送草稿，属 P0）
    try:
        _with_publish_lock(_do)
    except Exception as e:  # noqa: BLE001 - 记账必须尽力完成
        print(f"   ⚠️  加锁失败({e.__class__.__name__}: {e})，降级为无锁写入去重日志")
        try:
            _do()
        except Exception as e2:  # noqa: BLE001
            print(f"   ❌ 去重日志写入失败：{e2}\n"
                  f"      请手动补写 published_log.json（tag={tag}, media_id={media_id}），"
                  f"否则重复触发时会再次推送草稿！")


# ============ 主入口 ============

def main():
    parser = argparse.ArgumentParser(
        description="通过腾讯云 SCF 云函数发布公众号文章（固定 IP，无需手动白名单）"
    )
    parser.add_argument("-f", "--file", help="Markdown 文件路径")
    parser.add_argument("--title", help="文章标题（默认从文件提取）")
    parser.add_argument("--cover", help="封面图路径")
    parser.add_argument("-t", "--theme", help="wenyan 主题（默认 lapis）")
    parser.add_argument("-H", "--highlight", help="代码高亮主题（默认 solarized-light）")
    parser.add_argument("--footnote", action="store_true", help="启用脚注")
    parser.add_argument("--no-footnote", action="store_true", help="禁用脚注")
    parser.add_argument("--author", help="作者名")
    parser.add_argument("--digest", help="文章摘要")
    parser.add_argument("--dry-run", action="store_true", help="仅渲染 HTML 不发布")
    parser.add_argument("--check-ip", action="store_true", help="查看云函数出口 IP")
    parser.add_argument("--set-url", help="设置云函数 URL 并保存到配置文件")
    parser.add_argument("--no-obfuscate", action="store_true", default=False,
                        help="不混淆股票代码和公司名称（用于故事类文章保留真实名称）")
    parser.add_argument("--tag", default=None,
                        help="发布去重标签（如 A8）；传入后启用代码层『每日每标签仅发一次』硬拦截，"
                             "防止重复推送草稿箱（fail-closed，命中即退出码 43，绝不调用微信 API）")
    args = parser.parse_args()

    # 代码层发布去重（仅当传入 --tag 时启用；--dry-run 预览不拦截）
    if args.tag and not args.dry_run:
        if already_published_today(args.tag):
            print(f"🛑 代码层硬拦截：tag={args.tag} 今日已发布（见 published_log.json），"
                  f"禁止重复发布。退出码 43。")
            sys.exit(43)

    config = load_config()

    # --set-url: 设置云函数地址
    if args.set_url:
        config["scf_url"] = args.set_url
        save_config(config)
        print(f"✅ 云函数 URL 已保存: {args.set_url}")
        print(f"   配置文件: {CONFIG_FILE}")
        return

    # --check-ip: 查看出口 IP
    if args.check_ip:
        print("🔍 检查云函数出口 IP...")
        ip = check_scf_ip(config)
        if ip:
            print(f"\n✅ 云函数出口 IP: {ip}")
            print(f"   请将此 IP 添加到微信公众号后台 IP 白名单")
            print(f"   路径: 公众号后台 → 设置与开发 → 基本配置 → IP 白名单")
        else:
            print("\n❌ 无法获取 IP，请先配置云函数 URL:")
            print(f'   python {os.path.basename(__file__)} --set-url https://xxx.gz.tencentscf.com')
        return

    # 正常发布流程
    if not args.file:
        parser.error("发布文章需要 -f 参数指定文件路径")

    if not os.path.exists(args.file):
        print(f"❌ 文件不存在: {args.file}")
        sys.exit(1)

    theme = args.theme or config["default_theme"]
    highlight = args.highlight or config["default_highlight"]
    author = args.author or config["default_author"]
    footnote = args.footnote and not args.no_footnote

    print("=" * 55)
    print("  ☁️  云侠公众号 · 云函数发布")
    print("=" * 55)

    # Step 2: 后处理（上游 LLM 已完成通俗易懂润色；此处做代码/名称混淆等后处理）
    transformed_file, title = step_postprocess(
        args.file, args.title, args.cover, no_obfuscate=args.no_obfuscate
    )
    if not transformed_file:
        sys.exit(1)

    # Step 2: 渲染 HTML
    print()
    html = step_render(transformed_file, theme, highlight, footnote)
    if not html:
        sys.exit(1)

    # Dry-run 模式
    if args.dry_run:
        output_dir = os.path.dirname(transformed_file)
        html_file = os.path.join(output_dir, f"{os.path.splitext(os.path.basename(args.file))[0]}-preview.html")
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n📋 Dry-run: HTML 已保存到 {html_file}")
        return

    # Step 3: 封面图
    print()
    cover_b64, cover_url = step_prepare_cover(args.cover, args.file)

    # Step 4: 发布
    print()
    result = step_publish_scf(
        config, html, title, cover_b64, cover_url, author, args.digest or ""
    )

    # 结果
    print()
    print("=" * 55)
    if result and result.get("success"):
        print(f"🎉 发布成功！")
        print(f"   📱 Media ID: {result.get('media_id', 'N/A')}")
        print(f"   📋 请到公众号后台草稿箱查看: https://mp.weixin.qq.com/")
        # 发布成功后立即写入去重日志（与发布同一代码路径，确保不漏写）
        if args.tag:
            record_publish(args.tag, title, args.file, result.get("media_id", "N/A"))
            print(f"📝 已写入发布去重日志（tag={args.tag}）")
    else:
        print(f"❌ 发布失败")
        if result:
            print(f"   错误: {result.get('error', '未知错误')}")
            # IP 白名单问题
            err_str = str(result.get("error", ""))
            if "ip" in err_str.lower() or "whitelist" in err_str.lower():
                ip = check_scf_ip(config)
                if ip:
                    print(f"\n   💡 云函数出口 IP: {ip}")
                    print(f"   请将此 IP 添加到微信公众号白名单后重试")
        else:
            print(f"   未收到云函数响应")
        sys.exit(1)

    print("=" * 55)


if __name__ == "__main__":
    main()
