# -*- coding: utf-8 -*-
"""
微信公众号云函数代理发布器 v1.2
部署在腾讯云 SCF（事件函数 + 函数 URL），固定出口 IP，彻底解决动态 IP 白名单问题

功能：
  POST /          → 发布文章到公众号草稿箱（完整流程：token→封面→内图→草稿）
  GET  /          → 健康检查
  GET  /ip        → 返回函数出口 IP（用于白名单设置）
  GET  /token     → 获取 access_token（调试用）

请求格式 (POST):
  Headers: Authorization: Bearer <API_KEY>
  Body (JSON):
    {
      "html": "<section>...完整HTML...</section>",
      "title": "文章标题",
      "cover_base64": "base64编码的封面图",      # 二选一
      "cover_url": "https://封面图URL",           # 二选一
      "app_id": "wx...",
      "app_secret": "...",
      "author": "云侠AI量化",
      "digest": "文章摘要（可选）"
    }

响应格式:
  成功: {"success": true, "media_id": "xxx"}
  失败: {"success": false, "error": "错误信息"}

部署方式（v1.2）：
  使用腾讯云 SCF「事件函数」+「函数 URL」。
  - 函数类型：事件函数（不是 Web 函数）
  - 函数 URL：在函数配置中开启（不是触发器，不需要创建触发器）
  - 事件函数 + 函数 URL 的 event 格式：
      httpMethod, path, queryString, headers, body
    与旧 API 网关格式基本一致，代码原生兼容。
"""

import json
import os
import sys
import time
import base64
import re
import urllib.request
import urllib.parse
import urllib.error

# ============ 配置 ============

WX_API_BASE = "https://api.weixin.qq.com/cgi-bin"


def _load_function_api_key():
    """云函数自身鉴权 key（防止被滥用）。

    仅从环境变量 FUNCTION_API_KEY 读取；缺失则回退同目录 scf_config.json 的
    scf_api_key 字段（部署包配套提供）；两者皆无则 fail-secure 拒绝服务，
    绝不在源码硬编码默认值（避免密钥随代码泄露即可用）。"""
    env_val = os.environ.get("FUNCTION_API_KEY")
    if env_val:
        return env_val
    cfg_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scf_config.json")
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                j = json.load(f)
            v = j.get("scf_api_key")
            if v:
                return v
        except Exception:
            pass
    raise RuntimeError(
        "FUNCTION_API_KEY 未配置：请在 SCF 环境变量或 scf_config.json 中设置，禁止硬编码默认值"
    )


FUNCTION_API_KEY = _load_function_api_key()


# ============ 工具函数 ============

def make_response(status_code, body_dict):
    """构造 SCF 事件函数响应（函数 URL 格式）"""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
        "body": json.dumps(body_dict, ensure_ascii=False),
    }


def parse_event(event):
    """解析事件函数 + 函数 URL 的 event

    事件函数 + 函数 URL event 格式（v1.2）:
      {
        "httpMethod": "POST",
        "path": "/",
        "queryString": {"key": "value"},
        "headers": {"Content-Type": "..."},
        "body": "..."
      }

    兼容旧 API 网关格式（queryStringParameters / requestContext.http.path）
    """
    http_method = event.get("httpMethod", "GET")

    # 函数 URL 用 queryString，旧 API 网关用 queryStringParameters
    query = event.get("queryString") or event.get("queryStringParameters") or {}

    # 路径：函数 URL 直接在 path，旧 API 网关也在 path
    path = event.get("path", "/")
    if not path or path == "/":
        # 兜底：某些场景 path 可能在 requestContext 里
        rc = event.get("requestContext", {}) or {}
        http_ctx = rc.get("http", {}) or {}
        path = http_ctx.get("path", "/")

    # headers
    headers = event.get("headers", {}) or {}

    # body
    raw_body = event.get("body", "{}")
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8")

    return http_method, path, query, headers, raw_body


def build_multipart(filename, file_data, content_type="image/jpeg"):
    """构造 multipart/form-data 请求体"""
    boundary = f"----CloudFuncBoundary{int(time.time() * 1000)}"
    
    parts = []
    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'.encode("utf-8")
    )
    parts.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    parts.append(file_data)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    
    return b"".join(parts), boundary


# ============ 微信 API 封装 ============

def get_access_token(app_id, app_secret):
    """获取 access_token"""
    url = (
        f"{WX_API_BASE}/token"
        f"?grant_type=client_credential&appid={app_id}&secret={app_secret}"
    )
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if "access_token" not in data:
        raise WeChatAPIError("get_token", data)
    return data["access_token"]


def upload_cover_material(access_token, image_data, filename="cover.jpg"):
    """上传永久素材（封面图）→ 返回 media_id"""
    url = f"{WX_API_BASE}/material/add_material?access_token={access_token}&type=image"
    body, boundary = build_multipart(filename, image_data)

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if "media_id" not in data:
        raise WeChatAPIError("upload_cover", data)
    return data["media_id"]


def upload_content_image(access_token, image_data, filename="content_img.jpg"):
    """上传文章内联图片到微信图床 → 返回 CDN URL"""
    url = f"{WX_API_BASE}/media/uploadimg?access_token={access_token}"
    body, boundary = build_multipart(filename, image_data)

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if "url" not in data:
        raise WeChatAPIError("upload_content_image", data)
    return data["url"]


def create_draft(access_token, title, content, thumb_media_id,
                 author="云侠AI量化", digest=""):
    """创建草稿 → 返回 draft media_id"""
    url = f"{WX_API_BASE}/draft/add?access_token={access_token}"

    payload = {
        "articles": [{
            "title": title[:64],          # 微信标题限制 64 字
            "author": author[:8] if author else "云侠AI量化",
            "digest": digest[:120] if digest else "",
            "content": content,
            "thumb_media_id": thumb_media_id,
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
        }]
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=UTF-8")

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if "media_id" not in data:
        raise WeChatAPIError("create_draft", data)
    return data["media_id"]


# ============ HTML 内联图片处理 ============

def process_inline_images(access_token, html_content):
    """处理 HTML 中的 <img> 标签

    - data:image/xxx;base64,...  → 解码并上传到微信图床
    - http(s)://...               → 下载并上传到微信图床
    - 本地路径                     → 跳过（客户端应已转 base64）

    返回处理后的 HTML（src 替换为微信 CDN URL）
    """
    upload_count = 0
    skip_count = 0

    def replace_img(match):
        nonlocal upload_count, skip_count
        src = match.group(1)
        original = match.group(0)

        # base64 data URI
        if src.startswith("data:image/"):
            try:
                header, b64data = src.split(",", 1)
                # 推断图片格式
                if "png" in header:
                    ext, ctype = "png", "image/png"
                elif "gif" in header:
                    ext, ctype = "gif", "image/gif"
                else:
                    ext, ctype = "jpg", "image/jpeg"

                img_data = base64.b64decode(b64data)
                wx_url = upload_content_image(access_token, img_data, f"img_{upload_count}.{ext}")
                upload_count += 1
                return f'<img src="{wx_url}"'
            except Exception as e:
                print(f"[warn] base64 图片上传失败: {e}")
                skip_count += 1
                return original

        # HTTP/HTTPS URL
        elif src.startswith("http"):
            try:
                req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    img_data = resp.read()
                wx_url = upload_content_image(access_token, img_data, f"img_{upload_count}.jpg")
                upload_count += 1
                return f'<img src="{wx_url}"'
            except Exception as e:
                print(f"[warn] URL 图片下载/上传失败 ({src}): {e}")
                skip_count += 1
                return original

        # 本地路径 → 跳过
        else:
            print(f"[warn] 跳过本地图片: {src}")
            skip_count += 1
            return original

    processed = re.sub(r'<img\s+src="([^"]+)"', replace_img, html_content)
    print(f"  内联图片处理完成: 上传 {upload_count} 张, 跳过 {skip_count} 张")
    return processed


# ============ 辅助 ============

def get_outbound_ip():
    """获取云函数出口 IP"""
    sources = ["https://ifconfig.me", "https://api.ipify.org"]
    for url in sources:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ip = resp.read().decode("utf-8").strip()
                if ip and len(ip) < 50:
                    return ip
        except:
            continue
    return "unknown"


class WeChatAPIError(Exception):
    """微信 API 错误"""
    def __init__(self, action, response):
        self.action = action
        self.response = response
        super().__init__(f"[{action}] {json.dumps(response, ensure_ascii=False)}")


# ============ SCF 入口 ============

def main_handler(event, context):
    """腾讯云 SCF 入口函数（事件函数 + 函数 URL）"""
    try:
        http_method, path, query, headers, raw_body = parse_event(event)

        # CORS preflight
        if http_method == "OPTIONS":
            return make_response(200, {"ok": True})

        # ---- GET 路由 ----
        if http_method == "GET":
            # /ip → 返回出口 IP
            if "ip" in path or query.get("action") == "ip":
                ip = get_outbound_ip()
                return make_response(200, {
                    "ip": ip,
                    "note": "将此 IP 添加到微信公众号后台 IP 白名单（只需一次）",
                })

            # /token → 获取 access_token（调试用）
            if "token" in path or query.get("action") == "token":
                app_id = query.get("app_id", "")
                app_secret = query.get("app_secret", "")
                if not app_id or not app_secret:
                    return make_response(400, {"error": "需要 app_id 和 app_secret 参数"})
                token = get_access_token(app_id, app_secret)
                return make_response(200, {"access_token": token})

            # 健康检查
            return make_response(200, {
                "service": "wechat-publisher-proxy",
                "version": "1.2",
                "status": "ok",
            })

        # ---- POST 发布文章 ----
        if http_method != "POST":
            return make_response(405, {"error": "Method not allowed"})

        # 鉴权
        auth = headers.get("authorization", "") or headers.get("Authorization", "")
        if auth != f"Bearer {FUNCTION_API_KEY}":
            return make_response(401, {"error": "Unauthorized", "hint": "请在请求头中设置 Authorization: Bearer <API_KEY>"})

        # 解析请求体
        body = json.loads(raw_body)

        app_id = body.get("app_id", "")
        app_secret = body.get("app_secret", "")
        title = body.get("title", "未命名文章")
        html_content = body.get("html", "")
        cover_base64 = body.get("cover_base64", "")
        cover_url = body.get("cover_url", "")
        author = body.get("author", "云侠AI量化")
        digest = body.get("digest", "")

        # 参数校验
        if not app_id or not app_secret:
            return make_response(400, {"error": "缺少 app_id 或 app_secret"})
        if not html_content:
            return make_response(400, {"error": "缺少 html 内容"})
        if not cover_base64 and not cover_url:
            return make_response(400, {"error": "缺少封面图 (cover_base64 或 cover_url)"})

        # Step 1: 获取 access_token
        print("[step1] 获取 access_token...")
        token = get_access_token(app_id, app_secret)
        print(f"  access_token: {token[:20]}...")

        # Step 2: 上传封面图
        print("[step2] 上传封面图...")
        if cover_base64:
            img_data = base64.b64decode(cover_base64)
            print(f"  封面图大小: {len(img_data)} bytes (base64)")
        else:
            print(f"  下载封面图: {cover_url}")
            req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                img_data = resp.read()
            print(f"  封面图大小: {len(img_data)} bytes (downloaded)")

        thumb_media_id = upload_cover_material(token, img_data, "cover.jpg")
        print(f"  thumb_media_id: {thumb_media_id}")

        # Step 3: 处理文章内联图片
        print("[step3] 处理文章内联图片...")
        processed_html = process_inline_images(token, html_content)

        # Step 4: 创建草稿
        print("[step4] 创建草稿...")
        draft_media_id = create_draft(
            token, title, processed_html, thumb_media_id, author, digest
        )
        print(f"  draft_media_id: {draft_media_id}")

        return make_response(200, {
            "success": True,
            "media_id": draft_media_id,
            "message": "草稿已创建，请到公众号后台草稿箱查看",
            "steps": {
                "token": "ok",
                "cover_upload": "ok",
                "inline_images": "processed",
                "draft_created": "ok",
            },
        })

    except WeChatAPIError as e:
        print(f"[WeChat API Error] {e}")
        return make_response(500, {
            "success": False,
            "error": str(e),
            "action": e.action,
            "wechat_response": e.response,
        })
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        print(f"[HTTP Error] {e.code}: {error_body}")
        return make_response(500, {
            "success": False,
            "error": f"HTTP {e.code}: {error_body}",
        })
    except Exception as e:
        import traceback
        print(f"[Error] {str(e)}")
        traceback.print_exc()
        return make_response(500, {
            "success": False,
            "error": str(e),
        })
