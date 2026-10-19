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

- **🔒 视频生成需要正式订阅（2026-10-19 定论）**：这是**账号套餐限制，不是协议或风控问题**。
  链路已完全打通 —— `content_type=2020` + `skill_type=17` + `launch_stage=2` 会被服务端正确
  接收，agent 明确回复「正在为您生成视频…本次使用 Seedance 2.0 Mini 生成」，然后在最后的
  **额度扣减环节**返回「当前免费体验套餐不可用于创作使用，可升级正式订阅」。
  实测 `seedance_v2.0_mini` / `seedance_v2.0` / `seedance_v2.0_std` 三档模型与 5s/10s 两档
  时长**全部命中**同一提示。开通入口：
  `https://www.doubao.com/member/subscription?default_sku_key=doubao_personal_std&enter_method=answer_quota_limiting`
- **不要给请求加 X-Bogus 签名**（2026-10-19 推翻旧结论）：签名器产出的签名已不被豆包认可，
  网关直接判为机器人 → `710022004`（`shark_admin` 人机验证）。**免签名反而一路通畅**。
  对照实验：普通对话 + 签名 → `710022004`；普通对话 + 免签名 → 正常返回。
  旧文档曾说「视频端点风控阈值最高」，实测并非如此，而是签名本身失效。
  代码里保留 `force_x_bogus=True` 开关仅供回滚/对照，默认 `False`。
- **IP 风控**：触发 `710022002/710022004` 时：等待冷却（约 30 分钟，期间别开豆包网页版）/
  换网络（手机热点最快）/ 在浏览器手动过一次滑块验证。对话端点基本不受影响。
- **水印**：生图默认输出**无「豆包 AI 生成」标识**的版本（详见下方「水印机制」）。
  右下角仍会保留一个很小的「豆包」品牌标 —— 那是普通账号无法去除的部分。
- 签名器依赖 Playwright + Chromium；**默认已不启用**（见上），对话/图片/视频都不需要。
- 抖音的 aBogus 纯 Python 算法与豆包 frontierSign **不同源**（输出格式不同），无法移植；
  签名必须走浏览器页面 evaluate。

## 🎬 视频生成协议（2026-10-19 逆向）

端点与参数均已确认可用，**唯一缺口是付费墙**：

```
POST /samantha/chat/completion?{security_params}     # 免签名！
  messages[0].content_type = 2020# SamanthaVideoGenerationInput
  messages[0].skill.skill_type = 17                   # SkillVideoGeneration
  messages[0].content = {"text": "...", "video-model": ..., "video-duration": ..., "video-ratio": ...}
  completion_option.launch_stage = 2                  # 关键
  completion_option.memory_type = 0
```

官方参数来自 `POST /alice/slot/action_bar_v3/get_item_conf`（视频项 `item_id=45337547893174274`）：

| key | 可选值 |
|---|---|
| `video-model` | `seedance_v2.0_mini`（1x，默认）/ `seedance_v2.0`（Fast）/ `seedance_v2.0_std`（2x） |
| `video-duration` | `5` / `10`（默认 10） |
| `video-ratio` | `1:1` / `3:4` / `4:3` / `9:16` / `16:9` / `21:9` |

两个反直觉的实测结论：

1. **`launch_stage` 决定一切**：填 `1` 或 `0` 时 agent 会反复追问参数、任务永不落地
   （`query_video_gen_info` 始终 `has_generating_task:false`）；只有 `launch_stage=2`
   才会真正进入生成流程。
2. **参数必须同时写进自然语言**：只传结构化字段时 agent 说「视频时长未指定」并反问；
   在 `text` 里写明「Seedance 2.0 Mini，时长 10 秒、比例 16:9，请直接开始生成，
   不要再向我确认参数」后，它才回复「正在为您生成视频…」并走到扣费环节。

其他已探明但**不可用于取回成品**的端点（留档避免重复踩坑）：

| 端点 | 结论 |
|---|---|
| `/alice/message/list/v2` | `710010202`，7 种参数组合（含 int/str 类型变化）全挂；补 `X-Csrf-Token` 也无效 |
| `/alice/message/stream` | 同上 |
| `/samantha/thread/get` | `404Not Found`（POST） |
| `/samantha/im/conversation/basic_info` | `HTTP400` 空 body |
| `/samantha/video/get_play_info` | 参数被识别（返回业务错误码而非参数错误），是**正确的取回钥匙**，但需要有效 `vid` —— 而付费墙导致任务从未创建，拿不到 vid |
| `/samantha/im/conversation/list` | ✅ 可用，能列出全部会话（`conversation_type=5`、`message_index`、`badge_count`） |
| `/samantha/video/query_video_gen_info` | ✅ 可用，`remain_count` / `has_generating_task` / `is_beta_user` |

**会话续接（`client_meta`）走不通**：7 种结构变体（官方全字段 / 去掉 `local_permissions` /
去掉 `bot_id` / 只传 conv+sec / 旧格式 `section_id` / `last_message_index+1` / 空对象）
**全部返回 `710020702`**，且第2 轮请求不会在服务端创建可复用会话，对话实际无法续接。

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
