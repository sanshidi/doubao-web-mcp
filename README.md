# doubao-web-mcp

豆包网页版能力接入 MCP —— 让 AI 助手直接调用你的豆包网页账号：**对话 / 文生图 / 图生视频 / 模型选择 / 多账号轮换**。

参考 [web-ai-mcp](https://github.com/983033995/web-ai-mcp)（DeepSeek 网页版接入）的思路，零服务依赖、token 级配置、开箱即用。

## ✨ 能力

| 工具 | 功能 | 传输方式 |
|---|---|---|
| `doubao_chat` | 文本对话 / 写稿 / 审片 / 起标题（支持深度思考） | 纯 HTTP，免签名 |
| `doubao_image` | 文生图 / 图生图，**四档图片模型 + 32 种风格**可选，自动落盘 | 纯 HTTP，免签名 |
| `doubao_video` | 文生视频 / 图生视频，**四档视频模型 + 时长/比例**可选，轮询会话取回成品并落盘 mp4 | 纯 HTTP，免签名 |
| `doubao_models` | 查询官方模型/风格清单（含付费墙与消耗倍率标注） | — |
| `doubao_accounts` | 账号池管理（list / switch 多账号切换） | — |
| `doubao_ping` | 链路自检（登录态 + IP 风控状态） | — |

## 🏗 架构

```
MCP 工具层
   │
   ├─ 文生图 / 文生视频 ──→ 新统一端点 /chat/completion（chat_ability 协议，
   │                        免签名）模型/时长/比例/风格精确选择
   │                        │
   │                        └─（视频异步）轮询会话消息 latest_messagelist
   │                              │
   │                          提取卡片 → 下载落盘
   │
   └─ 图生图 / 兜底 ────→ 旧 /samantha/chat/completion（agent 通道）
```

**全程零浏览器**：对话/图片/视频都不需要 Playwright（签名器已废弃，见「已知限制」）。

## 🚀 配置

依赖 Python 3.10+。安装依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

### 步骤一：扫码登录拿凭证（本项目自带，不依赖任何外部服务）

```bash
.venv/bin/python login_qr.py                    # 无 GUI 环境：截二维码到 /tmp，手机扫
.venv/bin/python login_qr.py --name 主号        # 指定账号名
.venv/bin/python login_qr.py --qr-out /tmp/qr.png --timeout 420   # 自定义二维码路径 / 等待秒数
```

登录成功后凭证写入 `~/.doubao-mcp/accounts.json`（权限 600）。
多账号就多跑几次，`--name` 区分，凭证会累积进同一个账号池。

`login_qr.py` 会自动完成：点开登录弹窗 → 关掉「下载电脑版」推广窗（必要时重开登录）
→ 勾选用户协议 → 切到「扫码登录」tab → 截取二维码（2 倍分辨率）→ 轮询 `sessionid` 落地。

> ⚠️ `login.py` 是早期版本，**在豆包新版登录页上有已知失效点**（推广窗与协议勾选框都移进了
> shadow DOM，旧脚本的定位方式点不到它们，扫码后登录态不会落地）。新增账号请一律用 `login_qr.py`。

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
| `direct_server.py` | MCP 服务（6 个工具 + 账号池） |
| `doubao_client.py` | 豆包网页版协议客户端（vendored 自 doubao2api，修复了 `upload_image` 的 `file_info` 未定义 bug，内联了全部相对导入依赖；双协议：新统一端点 chat_ability〔模型选择〕+ 旧 samantha agent 通道〔兜底/图生图〕，含会话轮询取回与卡片解析） |
| `login_qr.py` | 扫码登录 → 抓 `sessionid` 写账号池（**当前推荐**，修了 shadow DOM 遮挡 / 协议勾选 / 扫码 tab） |
| `login.py` | 早期版扫码登录（保留，新版登录页上有已知失效点，勿用于新增账号） |

## 🔓 独立性

本项目**运行时零外部依赖**：不启动任何本地服务、不依赖 doubao2api、不依赖 Playwright、
不需要 API Key。

`doubao_client.py` 中的 `doubao2api` 仅出现在**代码来源署名**（见 `NOTICE`），
运行期不再读取它的任何文件或端口。从 doubao2api 迁来的旧账号池文件默认也不读，
确需读取时显式设 `DOUBAO_LEGACY_ACCOUNTS=1`。

## ⚠️ 已知限制

- **🔒 模型额度按账号套餐门控（2026-10-19 定论）**：这是**账号套餐限制，不是协议或风控问题**。
  新端点会按所选模型查额度——免费账号可跑免费档（Seedream 5.0 Flash / 4.5 / 4.0，
  Seedance 2.0 Mini / 2.0 Fast）；请求需订阅档（Seedream 5.0 Pro、Seedance 2.0 / 2.5）时
  服务端**明确返回不出图**（「这是豆包订阅标准套餐专属能力」+ `unavailable_reason`），
  已 A/B 实证：同一账号同一 prompt，免费档正常出图、Pro 档零图片。
  旧 agent 通道（samantha）则在扣费环节拦（「不可用于创作使用」）。
  开通入口：
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
- 签名器（Playwright + Chromium）**默认已不启用**（见上），对话/图片/视频都不需要。
- 抖音的 aBogus 纯 Python 算法与豆包 frontierSign **不同源**（输出格式不同），无法移植；
  且豆包侧签名已失效，本项目不再走浏览器签名路线。

## 🎬 视频生成协议（2026-10-19 逆向 · 全链路实测打通）

提交（免签名）：

```
POST /samantha/chat/completion?{security_params}
  messages[0].content_type = 2020            # SamanthaVideoGenerationInput
  messages[0].skill.skill_type = 17          # SkillVideoGeneration
  messages[0].content = {"text": "...", "video-model": ..., "video-duration": ..., "video-ratio": ...}
  completion_option.launch_stage = 2         # 关键：唯一能真正触发生成的档
  completion_option.memory_type = 0          # 关键：必须保存云端，否则任务孤儿化
```

取回（成品由云端渲染完成后推送进会话）：

```
POST /alice/conversation/latest_messagelist?{security_params}
  body: {"request_list": [{"conversation_id": "<字符串>", "message_index": 0, "count": 20}]}
  → data.message_map → messages[].content（block 列表）
  → block_type == 2074 → json.loads(block.content).creations[0].video
  → video_model.video_list.video_1.main_url（base64 解码 = 720p 完整 mp4）
```

官方参数来自 `POST /alice/slot/action_bar_v3/get_item_conf`（视频项 `item_id=45337547893174274`）：

| key | 可选值 |
|---|---|
| `video-model` | `seedance_v2.0_mini`（1x，默认）/ `seedance_v2.0`（Fast）/ `seedance_v2.0_std`（2x） |
| `video-duration` | `5` / `10`（默认 10） |
| `video-ratio` | `1:1` / `3:4` / `4:3` / `9:16` / `16:9` / `21:9` |

五个反直觉的实测结论：

1. **`launch_stage` 决定一切**：填 `1` 或 `0` 时 agent 会反复追问参数、任务永不落地
   （`query_video_gen_info` 始终 `has_generating_task:false`）；只有 `launch_stage=2`
   才会真正进入生成流程。
2. **参数必须同时写进自然语言**：只传结构化字段时 agent 说「视频时长未指定」并反问；
   在 `text` 里写明「Seedance 2.0 Mini，时长 10 秒、比例 16:9，请直接开始生成，
   不要再向我确认参数」后，它才真正开始生成。
3. **`memory_type` 必须为 `0`**：阅后即焚（`2`）的会话不落库 → 成品推送无处落地 →
   任务孤儿化（线程列表不出现、`query_video_gen_info` 无记录、永远拿不到 `vid`）。
   图片链路恰好因为去水印需求强制了 `0`，所以图片能取回而视频不能。
4. **`conversation_id` 必须传字符串**：`latest_messagelist` 的 `request_list` 里传数字
   会返回空；SSE 回显里的 `conversation_id` 全程是 `"0"`，真实 ID 要靠线程列表
   （`/samantha/im/conversation/list`）对比快照发现。
5. **`download_url` 只能拿到截断文件**：视频卡片里的 `download_url` 下载结果是残缺 mp4；
   必须用 `video_model.video_list.video_1.main_url`（base64）才是完整的 720p h264 文件。

其他已探明的端点（留档避免重复踩坑）：

| 端点 | 结论 |
|---|---|
| `/alice/message/list/v2` | `710010202`，7 种参数组合（含 int/str 类型变化）全挂；补 `X-Csrf-Token` 也无效 |
| `/alice/message/stream` | 同上 |
| `/samantha/thread/get` | `404Not Found`（POST） |
| `/samantha/im/conversation/basic_info` | `HTTP400` 空 body |
| `/samantha/video/get_play_info` | 参数被识别（返回业务错误码而非参数错误），是**正确的取回钥匙**，但需要有效 `vid` |
| `/samantha/media/mget_play_status` | 同上，按 `vid` 查询播放状态 |
| `/alice/message/content`、`/samantha/aispace/message_node_info` | 按 msg_id 取内容/节点，均未返回有效数据 |
| `/chat/async/chunk_stream` | 要 `taskId` 驼峰字段（传 message_id 报 "req taskId is empty"） |
| `/samantha/im/conversation/list` | ✅ 可用（POST，body `{}`），列出全部会话（`conversation_type=5`、`message_index`、`badge_count`） |
| `/samantha/video/query_video_gen_info` | ✅ 可用（POST，body `{}`），`remain_count` / `has_generating_task` / `is_beta_user` |
| `/alice/conversation/latest_messagelist` | ✅ 可用，取回成品的钥匙（见上） |

**会话续接（`client_meta`）走不通**：7 种结构变体（官方全字段 / 去掉 `local_permissions` /
去掉 `bot_id` / 只传 conv+sec / 旧格式 `section_id` / `last_message_index+1` / 空对象）
**全部返回 `710020702`**，且第2 轮请求不会在服务端创建可复用会话，对话实际无法续接。

## 🧩 新统一端点协议 chat_ability（2026-10-19 抓包地面真值 · 模型选择的关键）

真实前端（PC 网页版 3.41.4）走 **`POST /chat/completion`**（`aid=497858`，**免签名**），
与 samantha 的 skill content 字典是**两套协议**。模型/比例/时长/风格的选择挂在
`chat_ability.ability_param` 里：

```
POST /chat/completion?aid=497858&doubao_pc_version=3.41.4&...   # 免签名！
{
  "client_meta": {...},                   # local_conversation_id / bot_id / local_permissions
  "messages": [{                          # content_block[0].block_type=10000 纯文本
    "content_block": [{"block_type": 10000,
      "content": {"text_block": {"text": "生成图片：<prompt>"}}}]
  }],
  "option": {...},                        # conversation_mode=1 / message_storage_type=0 ...
  "chat_ability": {
    "ability_type": 3,                    # 3=图片；17=视频
    "ability_param": "<JSON 字符串>"
  },
  "ext": {...}
}
```

`ability_param` 内部结构（图片/视频**不同**，均为抓包真值）：

| 能力 | ability_type | ability_param 结构 |
|---|---|---|
| 图片 | 3 | **嵌套包装**：`{"ability_param": {"model", "ratio", "input_box_content"}, "ability_type": 1}` |
| 视频 | 17 | **扁平**：`{"ratio", "model", "duration", "input_box_content"}` |

- `model`：图片传显示文本（`Seedream 5.0 Flash` 等）；视频传 option_key（`seedance_v2.0_mini` 等）
- `ratio`：`auto` 或具体比例；视频另有 `duration`（5/10）
- `input_box_content.user_input_content`：用户原始输入；`reply_message_format`：官方模板
  （图片 `生成图片：%s`、视频 `生成视频：%s`）
- 消息文本 = 模板套用后的文本（视频还会拼上 `，10s` 后缀）

**响应**是命名事件 SSE（`SSE_HEARTBEAT` / `SSE_ACK` / `STREAM_CHUNK` / `CHUNK_DELTA` /
`FULL_MSG_NOTIFY` / `SSE_REPLY_END` / `STREAM_ERROR`），与 samantha 的 `event_type:2001`
JSON 事件不同：

- `SSE_ACK.ack_client_meta.conversation_id` → 真实会话 ID（异步取回用）
- 成品在 `STREAM_CHUNK.patch_op[].patch_value.content_block[]` → `block_type=2074`
  → `creation_block.creations[].image`
- 付费墙：文本块「这是豆包订阅标准套餐专属能力」+ `unavailable_reason{type:4}`，**不出图**

**视频（ability_type=17）是两步流程**（2026-10-19 抓包 + 实测真值）：

1. 第一条提交 → 服务端回「**视频生成参数确认**」卡（模型/时长/比例/创作方向），
   **前端界面里也没有按钮**，靠用户文字回复推进；
2. 第二条带 `client_meta.conversation_id`（+ `local_conversation_id` 续接上下文、
   `need_create_conversation=false`）发纯文本「**确认**」→ 任务才真正提交，
   响应出现 `block_type=10040` `thinking_block`（`视频生成已提交` / `视频生成中`）；
3. 之后成品异步推送进会话，走 `latest_messagelist` 轮询取回（与旧通道相同）。

> 注：旧 samantha 协议「会话续接全返回 710020702」的结论**不适用于新端点**——
> 新端点确认回复实测正常续接（ACK 返回同一 conversation_id，agent 延续上下文）。

**为什么必须用这套**：samantha 格式会把 content 剥成纯文本走 agent 通道
（读回会话消息可见用户消息只剩 `{"text":"..."}`），结构化模型字段被丢弃——
所以旧格式下 `image-model` / `video-model` 键**不生效**（实证：同账号请求
Pro 档在旧格式正常出图=被忽略；新格式则正确拦截）。

🔴 **浏览器的请求反而被限流**：真实前端自带 `a_bogus` 签名，而签名已失效
（见「已知限制」），所以浏览器提交 710022004；**我们免签名直打反而 200**。

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
- 文生图（新协议，推荐）：`POST /chat/completion` + `chat_ability{ability_type:3}`，模型/比例/风格精确选择（详见「新统一端点协议」章节）。
- 文生视频（新协议，推荐）：`POST /chat/completion` + `chat_ability{ability_type:17}`，模型/时长/比例精确选择，SSE_ACK 拿会话 → 轮询 `latest_messagelist` 取卡片。
- 文生图/图生视频（旧 agent 通道，兜底/图生图用）：`/samantha/chat/completion` + `content_type:2009`+`skill_type:3` / `content_type=2020`+`skill_type:17`（免签名），
  视频 `launch_stage=2` + `memory_type=0` 提交 → 轮询 `/alice/conversation/latest_messagelist`
  取卡片。**模型选择在该通道不生效**（content 被剥成纯文本）。
- 官方模型/风格清单：`POST /alice/slot/action_bar_v3/get_item_conf`（body `item_ids`），
  图片项 `45033999706177282`、视频项 `45337547893174274`；选择器是双层嵌套 JSON
  （`instruction_item_list[].template` → `selector_list[].template` → `option_list`）。
- 水印相关：`POST /creativity/user_config/{get,set}`（开关）、
  `POST /alice/resource/watermark_download`（换下载模板）、
  `POST /creativity/resource/get_without_watermark`（无水印资源判定）。
- 凭证：`sessionid` Cookie（登录态）；`msToken` 假值会触发 710022002，留空最安全。
- 响应头 `x-ms-token` 可用于 msToken 轮换（当前未启用）。

## 📜 开源许可与致谢

本项目以 **Apache License 2.0** 发布（见 `LICENSE`）。

`doubao_client.py` 是从 **[doubao2api](https://github.com/SeiShonagon520/doubao2api)**
（Apache 2.0）的 `client.py` vendored 并改造而来 —— 修复了 `upload_image` 的 `NameError`、
内联了相对导入、重写了视频链路（agent 通道提交 + 会话轮询取回）。逐条改动见 `NOTICE`。

思路参考 [web-ai-mcp](https://github.com/983033995/web-ai-mcp)（DeepSeek 网页版接入 MCP）。
豆包网页版协议为逆向所得，仅供个人学习研究。

## 免责声明

仅供个人学习研究使用。豆包网页版协议随时可能变更，接口失效时以官方为准。
请勿用于商业营利或高并发滥用。
