# 腾讯云 SCF 云函数部署指南（v1.2 · 事件函数 + 函数 URL）

## 解决什么问题？

微信公众号 API 要求调用方 IP 在白名单中。你的宽带是动态 IP，每次变化都要手动改白名单。
云函数有固定出口 IP，只需白名单加一次，以后永远不用管。

## v1.2 更新说明

- ❌ API 网关于 2025-06-30 下线，不再支持新建 API 网关触发器
- ❌ SCF 触发器创建功能也已下线，不再支持新建任何触发器
- ✅ v1.2 改用**事件函数 + 函数 URL**，函数 URL 不是触发器，是函数级别的 HTTP 端点配置
- ✅ 事件函数 + 函数 URL 的 event 格式与旧 API 网关基本一致，代码原生兼容

## 架构

```
本地电脑                    腾讯云 SCF                   微信公众号
┌──────────────┐         ┌──────────────┐          ┌──────────────┐
│ wenyan render│         │  事件函数     │          │              │
│ (生成HTML)    │         │              │          │  IP白名单     │
│              │  POST   │  1.get_token │          │  (只加一次)   │
│ 读封面图base64│────────→│  2.传封面图   │  API     │              │
│              │         │  3.传内联图   │─────────→│  草稿箱       │
│              │         │  4.创建草稿   │          │              │
└──────────────┘         └──────────────┘          └──────────────┘
  不需要IP白名单             固定出口IP               只信任SCF的IP
```

## 关键概念：函数 URL ≠ 触发器

| | 触发器（已下线） | 函数 URL（当前方案） |
|---|---|---|
| 位置 | 触发管理 → 创建触发器 | 函数配置 → 函数 URL |
| 状态 | ❌ 不再支持新建 | ✅ 当前推荐 |
| 本质 | 外部服务触发函数 | 函数自带的 HTTP 端点 |
| URL 格式 | `https://xxx.apigw.tencentcs.com/release/` | `https://xxx.tencentscf.com` |
| 适用函数类型 | 事件函数 / Web 函数 | **事件函数** / Web 函数 |

> 参考文档：https://cloud.tencent.com/document/product/583/96099

## 部署步骤（约 10 分钟）

### Step 1: 登录腾讯云 SCF 控制台

1. 打开 https://console.cloud.tencent.com/scf
2. 用微信扫码登录（首次需实名认证，个人户即可）
3. 选择区域：推荐 **广州** 或 **上海**（离微信服务器近）

### Step 2: 创建事件函数

1. 点击 **新建** → 选择 **从头开始**
2. 基础配置：
   - 函数名称：`wechat-publisher`
   - 运行环境：`Python 3.9`
   - 执行超时：`60` 秒
   - 内存：`128 MB`（够用）
3. **函数类型：选择「事件函数」**
   > ⚠️ 不要选「Web 函数」！Web 函数需要 Flask 框架和 scf_bootstrap，
   > 我们用的是标准 main_handler(event, context) 入口，必须选事件函数。
4. 函数代码：
   - 选择 **在线编辑**
   - 打开自动生成的 `index.py`，**全选删除默认代码**
   - 将本地 `index.py` 的完整内容粘贴进去
5. 高级配置 → 环境变量：
   - `FUNCTION_API_KEY` = `yq-ai-quant-2026`
6. 点击 **完成** 创建函数

> 💡 入口函数配置：确保「处理程序」字段为 `index.main_handler`
> （文件名 `index.py` + 函数名 `main_handler`）

### Step 3: 开启函数 URL

1. 进入函数详情页
2. 找到 **函数管理** → **函数配置**（不是「触发管理」！）
3. 找到 **函数 URL** 设置 → 点击 **编辑**
4. 开启函数 URL
5. 鉴权方式：**免鉴权**（云函数自身有 API Key 鉴权）
6. 保存

> ⚠️ 函数 URL 在「函数配置」里，不在「触发管理」里！
> 触发器的创建入口已经下线了，不要去找触发管理。

### Step 4: 获取函数 URL 地址

开启函数 URL 后，会得到一个地址，格式类似：
```
https://<app-id>-<url-id>.gz.tencentscf.com
```

> 注意：函数 URL 域名是 `tencentscf.com`，不是旧的 `apigw.tencentcs.com`。

**记下这个 URL**，后面要用。

### Step 5: 获取云函数出口 IP

在浏览器中访问你的函数 URL + `/ip`：
```
https://xxx-xxxxx.gz.tencentscf.com/ip
```

返回类似：
```json
{"ip": "150.109.95.30", "note": "将此 IP 添加到微信公众号后台 IP 白名单（只需一次）"}
```

**记下这个 IP**。

> ⚠️ 如果返回的 IP 是 `unknown`，等 1 分钟再试（函数冷启动时网络可能未就绪）。
> 如果返回 HTML 而不是 JSON，说明函数可能有报错，去 SCF 控制台查看日志。

### Step 6: 添加 IP 到微信公众号白名单

1. 登录 https://mp.weixin.qq.com/
2. 左侧菜单 → **设置与开发** → **基本配置**
3. 找到 **IP白名单** → 点击 **修改**
4. 添加上一步获取的 IP
5. 保存

### Step 7: 配置本地客户端

```bash
# 方式1: 命令行配置
python wechat_cloud_publish.py --set-url https://xxx-xxxxx.gz.tencentscf.com

# 方式2: 手动编辑配置文件
# 文件位置: wechat-scf/scf_config.json
# 将 "scf_url" 字段改为你的函数 URL 地址
```

### Step 8: 测试发布

```bash
# 检查云函数状态和 IP
python wechat_cloud_publish.py --check-ip

# 试发一篇文章（先 dry-run 看 HTML）
python wechat_cloud_publish.py -f "文章.md" --dry-run

# 正式发布
python wechat_cloud_publish.py -f "文章.md" --title "标题" --cover cover.jpg
```

## 事件函数 + 函数 URL 的 event 格式

事件函数通过函数 URL 接收 HTTP 请求时，event 格式如下：

```json
{
    "httpMethod": "POST",
    "path": "/ip",
    "queryString": {"key": "value"},
    "headers": {
        "Content-Type": "application/json",
        "Authorization": "Bearer xxx"
    },
    "body": "{\"html\": \"...\"}"
}
```

> 与旧 API 网关格式基本一致（`queryString` 而非 `queryStringParameters`，`path` 直接可用）。
> 代码中的 `parse_event()` 函数已做兼容处理。

## 关于 IP 稳定性

### 默认情况（免费）

腾讯云 SCF 默认使用共享 IP 池，IP **偶尔**会变化（通常几个月一次，不像家宽每天变）。
如果变了，重新访问 `/ip` 获取新 IP，更新白名单即可。

### 彻底固定（需 NAT 网关，约 ¥0.2/小时）

如果要做到 IP **永远不变**：

1. 在 SCF 控制台 → 函数详情 → **函数管理** → **网络配置**
2. 开启 **私有网络 (VPC)**
3. 关联一个 NAT 网关
4. NAT 网关绑定一个 **弹性公网 IP (EIP)**
5. 此后函数的所有出站请求都通过 EIP 发出，IP 永久固定

费用：EIP 约 ¥0.02/小时 + NAT 网关约 ¥0.15/小时 ≈ **¥4/天**（仅在函数实际运行时计费）

> 对于每天只发 1-2 篇文章的场景，免费方案已经够用，不需要 NAT 网关。

## 与现有方案的对比

| 方案 | 成本 | IP稳定性 | 操作复杂度 |
|------|------|---------|-----------|
| 手动更新白名单 | 免费 | 每天可能变 | 每次发布前手动改 |
| IP 检测脚本（已有） | 免费 | 每天可能变 | 半自动（IP变时需手动改） |
| **云函数（本方案）** | **免费** | **几个月变一次** | **部署一次，后续全自动** |
| 云函数+NAT网关 | ¥4/天 | 永久固定 | 部署一次，完全免维护 |

## 故障排查

### 1. 找不到创建函数 URL 的入口
- 函数 URL 在 **函数配置** 里，不是 **触发管理** 里
- 确认函数类型是 **事件函数**（Web 函数的函数 URL 行为不同）
- 参考文档：https://cloud.tencent.com/document/product/583/96099

### 2. 云函数返回 401 Unauthorized
- 检查 `scf_config.json` 中的 `scf_api_key` 是否与云函数环境变量 `FUNCTION_API_KEY` 一致

### 3. 云函数返回 400 "缺少封面图"
- 确保指定了 `--cover` 参数，或文章目录下有 `cover.jpg`

### 4. 微信 API 返回 ip not in whitelist
- 访问 `https://你的函数URL/ip` 获取当前出口 IP
- 将该 IP 添加到微信公众号白名单

### 5. 云函数返回 HTTP 433 "timed out after 3 seconds"
- **默认超时只有 3 秒，发布流程需要至少 30 秒**
- SCF 控制台 → 函数管理 → 函数配置 → 编辑 → **执行超时时间** 改为 `60` 秒
- 保存后重新发布
- 如果用了 NAT 网关，检查 EIP 是否正确绑定

### 5. wenyan render 失败
- 确保 wenyan-cli 已安装：`wenyan --version`
- 如果用托管版本，路径应在 `~/.workbuddy/binaries/node/workspace/`

### 6. 云函数超时
- 默认超时 60 秒，如果文章图片较多可能不够
- 在 SCF 控制台修改执行超时为 120 秒

### 7. 访问函数 URL 返回 HTML 错误页面而不是 JSON
- 说明函数代码有运行时错误
- 去 SCF 控制台 → 函数详情 → **日志查询** 查看错误详情
- 常见原因：处理程序配置不是 `index.main_handler`、环境变量未设置

### 8. 函数 URL 响应格式不对（返回原始 dict 而非 HTTP 响应）
- 确认函数类型是 **事件函数**（不是 Web 函数）
- 事件函数需要返回 `{"statusCode": 200, "headers": {...}, "body": "..."}` 格式
- Web 函数的响应是直接透传的，格式不同

## 文件清单

```
wechat-scf/
├── index.py                     ← 云函数代码（粘贴到 SCF 在线编辑器）
├── DEPLOY.md                    ← 本文件
├── wechat_cloud_publish.py      ← 本地客户端（主入口）
└── scf_config.json              ← 配置文件（存函数 URL 等）
```
