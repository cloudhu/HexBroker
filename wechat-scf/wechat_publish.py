#!/usr/bin/env python3
"""
云侠公众号一键发布脚本 v1.0

完整流程：
1. 润色（transform_for_wechat.py）
2. 生成frontmatter（标题+封面）
3. wenyan-cli发布到微信公众号草稿箱

Usage:
    python wechat_publish.py <input.md> [--title TITLE] [--cover COVER] [--theme THEME] [--highlight HIGHLIGHT] [--dry-run]
"""

import subprocess
import argparse
import os
import sys
import json
import shutil
from datetime import datetime


# 默认配置
DEFAULT_THEME = "lapis"
DEFAULT_HIGHLIGHT = "solarized-light"

# 文件路径
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRANSFORM_SCRIPT = os.path.join(SKILL_DIR, "scripts", "transform_for_wechat.py")

# 托管 Node 工作区中的 wenyan CLI（按运行时隔离规则，不装 -g）
WENYAN_MANAGED = os.path.join(
    os.path.expanduser("~"),
    ".workbuddy", "binaries", "node", "workspace",
    "node_modules", ".bin", "wenyan.cmd"
)


def get_wenyan_cmd() -> str:
    """获取 wenyan CLI 可执行文件路径。"""
    if os.path.exists(WENYAN_MANAGED):
        return WENYAN_MANAGED
    wenyan_on_path = shutil.which("wenyan")
    if wenyan_on_path:
        return wenyan_on_path
    return "wenyan"

# 封面图生成（使用简单的占位图）
DEFAULT_COVER_DIR = os.path.join(SKILL_DIR, "assets")


def generate_default_cover(output_dir: str) -> str:
    """生成默认封面图URL。
    
    使用Unsplash/picsum提供的免费图片URL，wenyan-cli会自动下载并上传到微信图床。
    尺寸要求：1080×864像素（微信公众号封面标准尺寸）。
    """
    # 封面图候选URL（金融/交易主题风格）
    cover_urls = [
        # Unsplash金融主题
        "https://images.unsplash.com/photo-1611974789855-9c2a0a423164?w=1080&h=864&fit=crop",
        # picsum随机（每次不同）
        "https://picsum.photos/1080/864",
    ]
    
    # 首选：尝试使用本地预设封面图
    assets_dir = os.path.join(output_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    cover_path = os.path.join(assets_dir, "default-cover.jpg")
    
    if os.path.exists(cover_path):
        print(f"✅ 使用本地封面图: {cover_path}")
        return cover_path
    
    # 备选：技能目录中的预设封面
    preset_cover = os.path.join(DEFAULT_COVER_DIR, "default-cover.jpg")
    if os.path.exists(preset_cover):
        import shutil
        shutil.copy2(preset_cover, cover_path)
        print(f"✅ 使用预设封面图: {cover_path}")
        return cover_path
    
    # 最终：使用网络URL
    print(f"⚠️ 未找到本地封面图，使用网络占位图")
    return cover_urls[0]


def find_latest_report(report_type: str = "morning") -> str:
    """从交易系统目录中找最新的报告文件。
    
    report_type: "morning" (晨间计划) / "review" (收盘复盘) / "auction" (竞价追踪) / "weekly" (周度选股)
    """
    base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(SKILL_DIR))),
                            "云侠交易系统")
    
    if report_type == "morning":
        search_dir = os.path.join(base_dir, "交易计划")
        pattern = "晨间计划"
    elif report_type == "review":
        search_dir = os.path.join(base_dir, "复盘")
        pattern = "复盘"
    elif report_type == "auction":
        search_dir = os.path.join(base_dir, "交易计划")
        pattern = "竞价追踪"
    elif report_type == "weekly":
        search_dir = os.path.join(base_dir, "周度选股")
        pattern = "周度选股报告"
    elif report_type == "news":
        search_dir = os.path.join(base_dir, "热点追踪")
        pattern = "热点资讯追踪"
    else:
        return None
    
    if not os.path.exists(search_dir):
        return None
    
    # 找最新日期的文件
    files = [f for f in os.listdir(search_dir) if pattern in f and f.endswith('.md')]
    if not files:
        return None
    
    # 按日期排序（文件名含YYYY-MM-DD）
    files.sort(reverse=True)
    return os.path.join(search_dir, files[0])


def transform_and_publish(input_file: str, title: str = None, cover: str = None,
                          theme: str = DEFAULT_THEME, highlight: str = DEFAULT_HIGHLIGHT,
                          dry_run: bool = False) -> bool:
    """完整的润色+发布流程。
    
    Returns: True if published successfully
    """
    # 1. 准备输出文件路径
    today = datetime.now().strftime("%Y-%m-%d")
    output_dir = os.path.join(SKILL_DIR, "output", today)
    os.makedirs(output_dir, exist_ok=True)
    
    basename = os.path.splitext(os.path.basename(input_file))[0]
    transformed_file = os.path.join(output_dir, f"{basename}-公众号版.md")
    
    # 2. 生成封面图
    if cover is None:
        cover = generate_default_cover(output_dir)
    
    # 3. 执行润色
    print(f"📝 Step 1: 润色内容...")
    cmd = [
        sys.executable, TRANSFORM_SCRIPT,
        input_file, transformed_file,
        "--cover", cover,
    ]
    if title:
        cmd.extend(["--title", title])
    
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
    if result.returncode != 0:
        print(f"❌ 润色失败: {result.stderr}")
        return False
    
    print(result.stdout)
    
    # 4. 验证润色结果
    with open(transformed_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 检查frontmatter是否完整
    if not content.startswith('---'):
        print("⚠️ Frontmatter缺失，wenyan-cli可能报错")
    
    # 检查title和cover
    has_title = 'title:' in content[:200]
    has_cover = 'cover:' in content[:200]
    if not has_title or not has_cover:
        print("⚠️ Frontmatter中缺少title或cover字段")
    
    # 5. 发布（或dry-run）
    if dry_run:
        print(f"\n📋 Dry-run模式：不执行发布")
        print(f"   润色后的文件: {transformed_file}")
        print(f"   主题: {theme}")
        print(f"   代码高亮: {highlight}")
        print(f"\n💡 手动发布命令:")
        wenyan_cmd = get_wenyan_cmd()
        print(f"   \"{wenyan_cmd}\" publish -f \"{transformed_file}\" -t {theme} -h {highlight}")
        return True
    
    print(f"\n📤 Step 2: 发布到微信公众号草稿箱...")
    wenyan_cmd = get_wenyan_cmd()
    cmd = [wenyan_cmd, "publish", "-f", transformed_file, "-t", theme, "-h", highlight]
    
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
    
    if result.returncode == 0:
        print(f"✅ 发布成功！")
        print(f"   📱 请前往微信公众号后台草稿箱查看: https://mp.weixin.qq.com/")
        if result.stdout:
            print(f"   输出: {result.stdout[:500]}")
        return True
    else:
        print(f"❌ 发布失败: {result.stderr}")
        print(f"   输出: {result.stdout[:500]}")
        print(f"\n💡 常见问题排查:")
        print(f"   1. IP不在白名单 → 获取公网IP: curl ifconfig.me → 添加到公众号后台")
        print(f"   2. API凭证错误 → 检查 ~/.wenyan-md/credential.json")
        print(f"   3. Frontmatter缺失 → 检查md文件开头是否含 title + cover")
        print(f"   4. 封面图无效 → 需要 1080×864 像素或有效URL")
        return False


def main():
    parser = argparse.ArgumentParser(description='云侠公众号一键发布脚本')
    parser.add_argument('input', nargs='?', help='输入Markdown文件路径（如不指定则自动查找最新报告）')
    parser.add_argument('--type', choices=['morning', 'review', 'auction', 'weekly', 'news'], default='morning',
                        help='报告类型：morning(晨间) / review(复盘) / auction(竞价) / weekly(周度选股) / news(热点资讯)')
    parser.add_argument('--title', help='文章标题（默认从文件提取）')
    parser.add_argument('--cover', help='封面图路径或URL')
    parser.add_argument('--theme', default=DEFAULT_THEME, help=f'wenyan主题（默认: {DEFAULT_THEME}）')
    parser.add_argument('--highlight', default=DEFAULT_HIGHLIGHT,
                        help=f'代码高亮主题（默认: {DEFAULT_HIGHLIGHT}）')
    parser.add_argument('--dry-run', action='store_true', help='仅润色不发布')
    
    args = parser.parse_args()
    
    # 确定输入文件
    if args.input:
        input_file = args.input
    else:
        input_file = find_latest_report(args.type)
        if input_file is None:
            print(f"❌ 未找到最新{args.type}报告文件")
            print(f"   请手动指定输入文件路径")
            sys.exit(1)
        print(f"📂 自动找到报告: {input_file}")
    
    if not os.path.exists(input_file):
        print(f"❌ 输入文件不存在: {input_file}")
        sys.exit(1)
    
    # 执行润色+发布
    success = transform_and_publish(
        input_file=input_file,
        title=args.title,
        cover=args.cover,
        theme=args.theme,
        highlight=args.highlight,
        dry_run=args.dry_run,
    )
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
