#!/usr/bin/env python3
"""
微信公众号 IP 白名单自动检测工具

功能：
1. 检测当前公网 IP
2. 与上次成功发布时的 IP 对比
3. 如果 IP 变化：
   - 自动打开微信公众号白名单设置页面
   - 将新 IP 复制到剪贴板
   - 提示用户粘贴并保存
4. 如果 IP 未变：输出 "OK"，可直接发布

使用方式：
  python wechat_ip_check.py              # 检查IP（发布前调用）
  python wechat_ip_check.py --quiet      # 静默模式（仅输出OK或CHANGED）
  python wechat_ip_check.py --reset      # 重置记录的IP

集成到发布流程：
  python wechat_ip_check.py && wenyan publish -f article.md ...
"""

import json
import os
import sys
import urllib.request
import subprocess
import webbrowser
from pathlib import Path

# ===== 配置 =====
IP_RECORD_FILE = os.path.join(os.environ.get("USERPROFILE", "."), ".workbuddy", "wechat_ip_record.json")
WECHAT_WHITELIST_URL = "https://mp.weixin.qq.com/cgi-bin/settingpage?t=setting/index&action=index&token="
# 直接打开公众号后台（用户需自行登录后导航到白名单设置）
WECHAT_CONSOLE_URL = "https://mp.weixin.qq.com/"

def get_public_ip():
    """获取当前公网 IP"""
    # 多个备用源，防止某个挂了
    sources = [
        "https://ifconfig.me",
        "https://api.ipify.org",
        "https://icanhazip.com",
    ]
    for url in sources:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ip = resp.read().decode("utf-8").strip()
                if ip and len(ip) < 50:  # 简单校验
                    return ip
        except Exception:
            continue
    return None

def load_recorded_ip():
    """读取上次记录的 IP"""
    try:
        with open(IP_RECORD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("ip")
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def save_recorded_ip(ip):
    """保存当前 IP"""
    os.makedirs(os.path.dirname(IP_RECORD_FILE), exist_ok=True)
    data = {"ip": ip, "updated": str(__import__("datetime").datetime.now())}
    with open(IP_RECORD_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def copy_to_clipboard(text):
    """将文本复制到剪贴板（Windows）"""
    try:
        process = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=True)
        process.communicate(text.encode("utf-16le"))
        return True
    except Exception:
        return False

def main():
    quiet = "--quiet" in sys.argv
    reset = "--reset" in sys.argv

    if reset:
        try:
            os.remove(IP_RECORD_FILE)
            print("✅ 已重置 IP 记录")
        except FileNotFoundError:
            print("ℹ️ 无记录可重置")
        return 0

    # Step 1: 获取当前公网 IP
    current_ip = get_public_ip()
    if not current_ip:
        print("❌ 无法获取公网 IP，请检查网络连接")
        return 1

    if quiet:
        # 静默模式只输出状态
        recorded = load_recorded_ip()
        if recorded == current_ip:
            print("OK")
            return 0
        else:
            print(f"CHANGED:{current_ip}")
            return 2

    print(f"📡 当前公网 IP: {current_ip}")

    # Step 2: 与上次记录对比
    recorded_ip = load_recorded_ip()

    if recorded_ip == current_ip:
        print(f"✅ IP 未变化（上次: {recorded_ip}），可直接发布")
        return 0

    # Step 3: IP 变化了
    if recorded_ip:
        print(f"⚠️  IP 已变化！")
        print(f"   上次记录: {recorded_ip}")
        print(f"   当前 IP:  {current_ip}")
    else:
        print(f"⚠️  首次记录 IP: {current_ip}")

    # Step 4: 复制到剪贴板
    if copy_to_clipboard(current_ip):
        print(f"📋 新 IP 已复制到剪贴板: {current_ip}")
    else:
        print(f"   请手动复制: {current_ip}")

    # Step 5: 打开微信公众号后台
    print(f"\n🌐 正在打开微信公众号后台...")
    print(f"   请按以下步骤操作：")
    print(f"   1. 登录公众号后台")
    print(f"   2. 左侧菜单 → 设置与开发 → 基本配置")
    print(f"   3. 找到「IP白名单」→ 点击修改")
    print(f"   4. 粘贴新 IP: {current_ip}")
    print(f"   5. 保存")
    print(f"   6. 回到这里，重新运行发布命令")

    try:
        webbrowser.open(WECHAT_CONSOLE_URL)
    except Exception:
        pass

    # 保存新 IP 记录（用户更新白名单后记录）
    save_recorded_ip(current_ip)
    print(f"\n📝 已记录新 IP，下次发布将自动对比")
    print(f"   如发布仍失败（白名单未更新），运行: python wechat_ip_check.py --reset")

    return 2  # 返回非零表示 IP 变化，需要用户干预

if __name__ == "__main__":
    sys.exit(main())
