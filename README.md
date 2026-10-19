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
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

### 步骤一：扫码登录拿凭证（本项目自带，不依赖任何外部服务）

```bash
.venv/bin/python login.py            # 无 GUI 环境：截二维码到 /tmp，手机扫
.venv/bin/python login.py --headed   # 在自己终端跑：可见浏览器窗口扫码
.venv/bin/python login.py --name 主号
```

登录成功后凭证写入 `~/.doubao-mcp/accounts.json`（权限 600）。
多账号就多跑几次，`--name` 区分，凭证会累积进同一个账号池。

`login.py` 会自动完成：点开登录弹窗 → 勾选用户协议 → 清掉「下载电脑版」推广弹窗
→ 截取二维码（2 倍分辨率）→ 轮询 `sessionid` 落地。

### 步骤二：注册 MCP

```json
{
  "mcpServers": {
    "doubao-web": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/direct_server.py"],
      "env": {
        "DOUBAO_OUT_DIR": "/Users/you/Downloads/豆包输出"
      }
    }
  }
}
```

凭证默认从 `~/.doubao-mcp/accounts.json` 读，无需其他配置。

<details>
<summary>不想用 login.py？手动填 Cookie</summary>

浏览器登录 doubao.com → F12 → Application → Cookies → 复制 `sessionid`（必需）
和 `ttwid` / `passport_csrf_token`（可选，带上更稳），然后：

```json
"env": {
  "DOUBAO_ACCOUNTS": "主号:sessionid=xxx,ttwid=yyy;小号:sessionid=zzz"
}
```

多账号用 `;` 分隔。环境变量优先级高于账号池文件。
额度耗尽或风控时用 `doubao_accounts(action="switch", name="小号")` 热切换。
</details>

## 📦 文件

| 文件 | 说明 |
|---|---|
| `direct_server.py` | MCP 服务（5 个工具 + 账号池） |
| `doubao_client.py` | 豆包网页版协议客户端（vendored 自 doubao2api，修复了 `upload_image` 的 `file_info` 未定义 bug，内联了全部相对导入依赖，`generate_video` 增加 X-Bogus 签名） |
| `login.py` | 扫码登录 → 抓 `sessionid` 写账号池（本项目自有能力） |

## 🔓 独立性

本项目**运行时零外部依赖**：不启动任何本地服务、不依赖 doubao2api 或 Playwright 常驻的
请求通道（浏览器仅在视频签名时临时拉起）、不需要 API Key。

`doubao_client.py` 中的 `doubao2api` 仅出现在**代码来源署名**（见 `NOTICE`），
运行期不再读取它的任何文件或端口。从 doubao2api 迁来的旧账号池文件默认也不读，
确需读取时显式设 `DOUBAO_LEGACY_ACCOUNTS=1`。

## ⚠️ 已知限制

- **IP 风控**：视频端点（skill 17）风控阈值最高。触发 `710022002/710022004` 时：
  等待冷却（约 30 分钟，期间别开豆包网页版）/ 换网络（手机热点最快）/
  在浏览器手动过一次滑块验证。对话端点基本不受影响。
- **水印**：生图默认输出**无「豆包 AI 生成」标识**的版本（详见下方「水印机制」）。
  右下角仍会保留一个很小的「豆包」品牌标 —— 那是普通账号无法去除的部分。
- **视频生成受豆包账号每日免费额度限制**。
- 签名器依赖 Playwright + Chromium；仅视频功能需要，对话/图片不受影响。
- 抖音的 aBogus 纯 Python 算法与豆包 frontierSign **不同源**（输出格式不同），无法移植；
  签名必须走浏览器页面 evaluate（已验证 `X-Bogus` 参数名 + 16 位短签名）。

## 🔏 水印机制（2026-10-19 逆向 + A/B 实测）

豆包在 **CDN（ImageX）渲染层**烧水印，同一个资源对象会下发多个 tplv 模板 URL，
**模板后缀决定是否带水印**：

| 模板 | 水印 |
|---|---|
| `~tplv-*-cgen_lwm3` / `cthumb_lwm3` | 带「豆包 AI 生成」 |
| `~tplv-*-cgen_dwm3` / `cthumb_dwm3` | 去水印版 |
| `~tplv-*-image_raw` | 原始图，无水印 |

关键机制：

1. **服务端只在 `memory_type=0`（保存到云端历史）时下发 `dwm3` / `image_raw`**；
   `memory_type=2`（阅后即焚）只给 `lwm3`。这与豆包「AI 生成水印设置」弹窗里
   「当前账号生成**并保存的** AI 图片、视频将不再显示水印」的措辞完全一致。
2. **URL 签名覆盖整个 path（含模板名）**，所以手工把 `lwm3` 改成 `dwm3` 会直接
   `400 ImageX code:2004` —— 只能让服务端自己发无水印模板。
3. `memory_type=0` 时服务端会走 agent 通道，**忽略 `ratio` 参数**，出图比例随机。
   本项目用「模糊背景填充」在本地把图修正到精确目标比例（主体完整不裁切）。
4. 账号开关本身（`POST /creativity/user_config/get` → `config_map["1"].watermark_option.is_on`）
   是账号级的，但**去水印与否最终由服务端按资源策略决定**，开关改不了已生成资源。

## 📄 协议要点（逆向笔记）
- 对话：`POST /chat/completion`（新版端点，免签名），payload 为 `client_meta/messages[].content_block[{block_type:10000,text_block}]/option/ext` 结构，**必须带 `Agw-Js-Conv: str` 头**。
- 上传图片：`POST /samantha/pages/upload_image` → 拿 uri → `POST /alice/message/get_file_url` 换 CDN 链接。
- 文生图：`/samantha/chat/completion` + `content_type:2009` + `skill_type:3`（免签名）。
- 图生视频：`/samantha/chat/completion` + `content_type:2020` + `skill_type:17` + `X-Bogus` 签名，
  响应 SSE 提取 `fin_reason.async_task.id` → 轮询 `/samantha/chat/async/stream`。
- 水印相关：`POST /creativity/user_config/{get,set}`（开关）、
  `POST /alice/resource/watermark_download`（换下载模板）、
  `POST /creativity/resource/get_without_watermark`（无水印资源判定）。
- 凭证：`sessionid` Cookie（登录态）；`msToken` 假值会触发 710022002，留空最安全。
- 响应头 `x-ms-token` 可用于 msToken 轮换（当前未启用）。

## 📜 开源许可与致谢

本项目以 **Apache License 2.0** 发布（见 `LICENSE`）。

`doubao_client.py` 是从 **[doubao2api](https://github.com/SeiShonagon520/doubao2api)**
（Apache 2.0）的 `client.py` vendored 并改造而来 —— 修复了 `upload_image` 的 `NameError`、
内联了相对导入、新增 X-Bogus 签名器。逐条改动见 `NOTICE`。

思路参考 [web-ai-mcp](https://github.com/983033995/web-ai-mcp)（DeepSeek 网页版接入 MCP）。
豆包网页版协议为逆向所得，仅供个人学习研究。

## 免责声明

仅供个人学习研究使用。豆包网页版协议随时可能变更，接口失效时以官方为准。
请勿用于商业营利或高并发滥用。
