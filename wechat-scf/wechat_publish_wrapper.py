#!/usr/bin/env python3
"""
微信公众号一键发布包装脚本

自动流程：
1. 检测公网 IP → 如果变化，打开浏览器+复制IP，等用户更新白名单
2. IP 确认后 → 调用 wenyan publish 发布
3. 发布成功 → 更新 IP 记录

使用方式：
  python wechat_publish_wrapper.py -f "文章.md" -t lapis -h solarized-light --no-footnote
  python wechat_publish_wrapper.py -f "文章.md"  # 使用默认主题

等同于 wenyan publish，但多了 IP 预检。
"""

import subprocess
import sys
import os
import re

# 同目录下的 IP 检测脚本
IP_CHECK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wechat_ip_check.py")
PYTHON = sys.executable

def run_ip_check():
    """运行 IP 检测，返回 (need_action, current_ip)"""
    result = subprocess.run(
        [PYTHON, IP_CHECK_SCRIPT],
        capture_output=True,
        text=True,
        encoding="utf-8"
    )
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    # 返回码: 0=OK, 2=IP变化需处理, 1=错误
    if result.returncode == 0:
        return False, None  # IP 没变，可以直接发布
    elif result.returncode == 2:
        return True, None   # IP 变了，需要用户操作
    else:
        return False, None  # 错误，但仍尝试发布

def run_wenyan_publish(args):
    """调用 wenyan publish"""
    # 提取 -f 参数值作为文件路径
    cmd = ["wenyan", "publish"] + args

    # 设置环境变量（微信公众号凭证）
    env = os.environ.copy()
    env["WECHAT_APP_ID"] = "wx9ec3e51bf59160d1"
    env["WECHAT_APP_SECRET"] = "1402e638c0af149a5e0732a15aa649a8"

    print(f"\n🚀 正在发布...")
    print(f"   命令: {' '.join(cmd)}")
    print()

    result = subprocess.run(cmd, env=env)
    return result.returncode

def main():
    # 解析参数，直接透传给 wenyan publish
    args = sys.argv[1:]

    if not args or "-f" not in args:
        print("用法: python wechat_publish_wrapper.py -f \"文章.md\" [-t theme] [-h highlight] [--no-footnote]")
        print("      所有参数透传给 wenyan publish")
        sys.exit(1)

    # Step 1: IP 检测
    print("=" * 50)
    print("📋 Step 1: 公网 IP 检测")
    print("=" * 50)

    need_action, _ = run_ip_check()

    if need_action:
        print("\n" + "=" * 50)
        print("⏸️  需要手动更新白名单")
        print("=" * 50)
        print("浏览器已打开公众号后台，请添加新 IP 到白名单后，")
        print("按 Enter 继续发布，或按 Ctrl+C 取消...")

        try:
            input()
        except KeyboardInterrupt:
            print("\n❌ 已取消发布")
            sys.exit(1)

    # Step 2: 发布
    print("\n" + "=" * 50)
    print("📋 Step 2: 发布到公众号草稿箱")
    print("=" * 50)

    retcode = run_wenyan_publish(args)

    if retcode == 0:
        print("\n✅ 发布成功！")
    else:
        print(f"\n❌ 发布失败（退出码: {retcode}）")
        # 检查是否是 IP 白名单问题
        print("   如果是 IP 白名单错误，请运行:")
        print(f"   python {IP_CHECK_SCRIPT}")
        print("   更新白名单后重试")

    sys.exit(retcode)

if __name__ == "__main__":
    main()
