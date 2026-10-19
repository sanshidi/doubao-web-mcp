# doubao-web-mcp

豆包网页版能力接入 MCP —— 让 AI 助手直接调用你的豆包网页账号：**对话 / 文生图 / 图生视频 / 多账号轮换**。

参考 [web-ai-mcp](https://github.com/983033995/web-ai-mcp)（DeepSeek 网页版接入）的思路，零服务依赖、token 级配置、开箱即用。

## ✨ 能力

| 工具 | 功能 | 传输方式 |
|---|---|---|
| `doubao_chat` | 文本对话 / 写稿 / 审片 / 起标题（支持深度思考） | 纯 HTTP，免签名 |
| `doubao_image` | 文生图 / 图生图，自动落盘 | 纯 HTTP，免签名 |
| `doubao_video` | 文生视频 / **图生视频**，自动落盘 mp4 | HTTP + X-Bogus 签名 |
| `doubao_accounts` | 账号池管理（list / switch 多账号切换） | — |
| `doubao_ping` | 链路自检（登录态 + IP 风控状态） | — |

## 🏗 架构

```
MCP 工具层
   │
   ├─ 对话 / 图片 ──→ 纯 HTTP 直连 doubao.com（免签名，快）
   │
   └─ 视频 ──→ X-Bogus 签名器（headless 浏览器页面 evaluate
              window.bdms.frontierSign，毫秒级）──→ HTTP 提交任务
                                                        │
                                                   轮询 async/stream
                                                        │
                                                   下载 mp4 落盘
```

**浏览器只当"签名器"**（仅视频需要，常驻一个 headless 页面），不作为请求通道。
对话/图片全程零浏览器。

## 🚀 配置

依赖 Python 3.10+。安装依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install "mcp[cli]<2" aiohttp httpx playwright
.venv/bin/python -m playwright install chromium
```

### 方式一：账号池文件（零配置，推荐）

如果你本地跑过 [doubao2api](https://github.com/SeiShonagon520/doubao2api) 并扫码登录过，
`~/.doubao_browser/accounts.json` 会自动生成，本 MCP **直接兼容读取**，无需任何配置。

### 方式二：环境变量显式配置

```json
{
  "mcpServers": {
    "doubao-web": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/direct_server.py"],
      "env": {
        "DOUBAO_ACCOUNTS": "主号:sessionid=xxx,ttwid=yyy;小号:sessionid=zzz",
        "DOUBAO_OUT_DIR": "/Users/you/Downloads/豆包输出"
      }
    }
  }
}
```

Cookie 获取：浏览器登录 doubao.com → F12 → Application → Cookies → 复制 `sessionid`（必需）
和 `ttwid` / `passport_csrf_token`（可选，带上更稳）。

多账号用 `;` 分隔，每个账号至少含 `sessionid`。额度耗尽或风控时，
用 `doubao_accounts(action="switch", name="小号")` 热切换。

## 📦 文件

| 文件 | 说明 |
|---|---|
| `direct_server.py` | MCP 服务（5 个工具 + 账号池） |
| `doubao_client.py` | 豆包网页版协议客户端（vendored 自 doubao2api，修复了 `upload_image` 的 `file_info` 未定义 bug，内联了全部相对导入依赖，`generate_video` 增加 X-Bogus 签名） |

## ⚠️ 已知限制

- **IP 风控**：视频端点（skill 17）风控阈值最高。触发 `710022002/710022004` 时：
  等待冷却（约 30 分钟，期间别开豆包网页版）/ 换网络（手机热点最快）/
  在浏览器手动过一次滑块验证。对话端点基本不受影响。
- 生成的图片带豆包官方水印（服务端 CDN 渲染，无法去除）。
- 视频生成受豆包账号每日免费额度限制。
- 签名器依赖 Playwright + Chromium；仅视频功能需要，对话/图片不受影响。
- 抖音的 aBogus 纯 Python 算法与豆包 frontierSign **不同源**（输出格式不同），无法移植；
  签名必须走浏览器页面 evaluate（已验证 `X-Bogus` 参数名 + 16 位短签名）。

## 📄 协议要点（逆向笔记）

- 对话：`POST /chat/completion`（新版端点，免签名），payload 为 `client_meta/messages[].content_block[{block_type:10000,text_block}]/option/ext` 结构，**必须带 `Agw-Js-Conv: str` 头**。
- 上传图片：`POST /samantha/pages/upload_image` → 拿 uri → `POST /alice/message/get_file_url` 换 CDN 链接。
- 文生图：`/samantha/chat/completion` + `content_type:2009` + `skill_type:3`（免签名）。
- 图生视频：`/samantha/chat/completion` + `content_type:2020` + `skill_type:17` + `X-Bogus` 签名，
  响应 SSE 提取 `fin_reason.async_task.id` → 轮询 `/samantha/chat/async/stream`。
- 凭证：`sessionid` Cookie（登录态）；`msToken` 假值会触发 710022002，留空最安全。
- 响应头 `x-ms-token` 可用于 msToken 轮换（当前未启用）。

## 免责声明

仅供个人学习研究使用。豆包网页版协议随时可能变更，接口失效时以官方为准。
请勿用于商业营利或高并发滥用。
