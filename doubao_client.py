"""
Doubao Chat API Client - reverse-engineered desktop client protocol.

Supports three endpoint families:
  - stream_call_bot (Alice legacy)
  - /samantha/chat/completion
  - /chat/completion (unified main entry, desktop client)
"""
import asyncio
import base64
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import re
import time
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Dict, Iterator, List, Optional, Tuple, Union
from urllib.parse import parse_qs, quote, urlencode, urlparse

import aiohttp

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .captcha_handler import CaptchaHandler

RISK_ERROR_CODES = {710022002, 710022004}

# ---- vendored constants (was: from .qr_login import ...) ----
CHROME_VERSION = "148.0.0.0"
CHROMIUM_BUILD = "148.0.7816.0"


# ---- 官方创作模型清单（2026-10-19 抓包自 /alice/slot/action_bar_v3/get_item_conf） ----
# 图片项 item_id=45033999706177282，「模型」选择器 selector id=30；
# 视频项 item_id=45337547893174274，「模型」选择器 selector id=6。
# option_key 即前端写入消息 content 的值（图片走 "image-model"，视频走 "video-model"）。

IMAGE_MODELS: Dict[str, Dict[str, Any]] = {
    "Seedream 5.0 Flash": {"id": 6, "sub": "专业快速", "need_upgrade": False},
    "Seedream 5.0 Pro": {"id": 5, "sub": "专业出图・4 倍消耗", "need_upgrade": True},
    "Seedream 4.5": {"id": 3, "sub": "日常生成", "need_upgrade": False},
    "Seedream 4.0": {"id": 1, "sub": "基础生图", "need_upgrade": False},
}

VIDEO_MODELS: Dict[str, Dict[str, Any]] = {
    "seedance_v2.5": {"id": 7, "display": "Seedance 2.5",
                      "sub": "旗舰视频标杆・5 倍消耗", "need_upgrade": True},
    "seedance_v2.0_std": {"id": 5, "display": "Seedance 2.0",
                          "sub": "进阶画面表现・2 倍消耗", "need_upgrade": True},
    "seedance_v2.0": {"id": 4, "display": "Seedance 2.0 Fast",
                      "sub": "快速出片选择", "need_upgrade": False},
    "seedance_v2.0_mini": {"id": 6, "display": "Seedance 2.0 Mini",
                           "sub": "日常生成使用", "need_upgrade": False},
}

# 官方「风格」选择器 selector id=4 的 option_key → 显示名（message_text = "图片风格为{显示名}"）
IMAGE_STYLES: Dict[str, str] = {
    "skill_image_styles_portrait": "人像摄影",
    "skill_image_styles_film": "电影写真",
    "skill_image_styles_chinese": "中国风",
    "skill_image_styles_japanese_anime": "动漫",
    "skill_image_styles_3d": "3D渲染",
    "image_gen_style_cyberpunk": "赛博朋克",
    "skill_image_styles_cg": "CG 动画",
    "skill_image_styles_ink_wash_painting": "水墨画",
    "skill_image_styles_oil_painting": "油画",
    "skill_image_styles_classic": "古典",
    "skill_image_styles_watercolor": "水彩画",
    "skill_image_styles_cartoon": "卡通",
    "skill_image_styles_flat_illustration": "平面插画",
    "skill_image_styles_landscape": "风景",
    "skill_image_styles_hongkong_anime": "港风动漫",
    "skill_image_styles_pixel_style": "像素风格",
    "skill_image_styles_fluorescence": "荧光绘画",
    "skill_image_styles_colored_pencil": "彩铅画",
    "skill_image_styles_figure": "手办",
    "skill_image_styles_children_illustration": "儿童绘画",
    "skill_image_styles_abstract": "抽象",
    "skill_image_styles_sharp_illustration": "锐笔插画",
    "skill_image_styles_acg": "二次元",
    "skill_image_styles_ink_print": "油墨印刷",
    "skill_image_styles_bnw_printing": "版画",
    "skill_image_styles_monet": "莫奈",
    "skill_image_styles_picasso": "毕加索",
    "skill_image_styles_rembrandt": "伦勃朗",
    "skill_image_styles_matisse": "马蒂斯",
    "skill_image_styles_baroque": "巴洛克",
    "skill_image_styles_oldschool": "复古动漫",
    "skill_image_styles_picturebook": "绘本",
}

# 兼容 generate_image 里的注入写法
IMAGE_STYLE_LABELS = IMAGE_STYLES


# ---- vendored (was: from .sse import iter_sse_events) ----
async def iter_sse_events(content: Any) -> AsyncIterator[Dict[str, Any]]:
    """Parse SSE stream from aiohttp response.content."""
    current_event = "message"
    current_data_lines: List[str] = []

    async for raw_line in content:
        line = raw_line.decode("utf-8", errors="ignore").rstrip("\r\n")
        if line == "":
            if current_data_lines:
                data_text = "\n".join(current_data_lines)
                yield {"event": current_event, "data": data_text,
                       "json": _safe_json_loads(data_text)}
            current_event = "message"
            current_data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            current_event = line[6:].strip() or "message"
            continue
        if line.startswith("data:"):
            current_data_lines.append(line[5:].lstrip())
            continue

    if current_data_lines:
        data_text = "\n".join(current_data_lines)
        yield {"event": current_event, "data": data_text,
               "json": _safe_json_loads(data_text)}


def _safe_json_loads(text: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    return None


# ---- vendored (was: from .browser_client import resolve_memory_type) ----
def resolve_memory_type(
    memory_type: Optional[int] = None,
    save_history: Optional[bool] = None,
) -> int:
    """0 = saved to web history, 2 = ephemeral (burn-after-reading)."""
    if memory_type is not None:
        return int(memory_type)
    if save_history is not None:
        return 0 if save_history else 2
    save_env = os.environ.get("SAVE_CHAT_HISTORY", "").strip().lower()
    if save_env in ("true", "1", "yes"):
        return 0
    auto_del_env = os.environ.get("DOUBAO_AUTO_DELETE_CONV", "true").strip().lower()
    if auto_del_env in ("false", "0", "no"):
        return 0
    return 2


# ---- X-Bogus 签名器（常驻 headless 浏览器页面，仅签名用）----
class _XSigner:
    """调用豆包页面 window.bdms.frontierSign() 生成 X-Bogus 签名。

    首次 sign() 时启动 headless 浏览器并加载豆包页面（约 5 秒），
    之后同一页面反复 evaluate（毫秒级）。失败自动重置，下次重试。
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._page = None

    async def sign(self, query_string: str, cookies: Dict[str, str]) -> str:
        try:
            if self._page is None:
                await self._start(cookies)
            if self._page is None:
                return ""
            sig = await self._page.evaluate(
                f"window.bdms.frontierSign({json.dumps(query_string)})")
            if isinstance(sig, dict):
                return sig.get("X-Bogus", "") or ""
            return ""
        except Exception as e:
            log.warning("X-Bogus 签名失败，重置签名器: %s", e)
            await self.close()
            return ""

    async def _start(self, cookies: Dict[str, str]):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            log.warning("playwright 未安装，视频签名不可用（pip install playwright）")
            return
        self._playwright = async_playwright()
        p = await self._playwright.__aenter__()
        self._browser = await p.chromium.launch(headless=True)
        ctx = await self._browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"))
        pw_cookies = [{"name": k, "value": v, "domain": ".doubao.com", "path": "/"}
                      for k, v in cookies.items() if v]
        await ctx.add_cookies(pw_cookies)
        self._page = await ctx.new_page()
        await self._page.goto("https://www.doubao.com/chat/",
                              wait_until="domcontentloaded", timeout=30000)
        await self._page.wait_for_timeout(4000)

    async def close(self):
        for attr in ("_page", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._playwright is not None:
            try:
                await self._playwright.__aexit__(None, None, None)
            except Exception:
                pass
            self._playwright = None


_signer = _XSigner()


def _get_signer() -> _XSigner:
    return _signer


class DoubaoChatError(RuntimeError):
    pass


class DoubaoRateLimitError(DoubaoChatError):
    """Raised when the server returns a risk-control / rate-limit error.

    Attributes:
        code:        The numeric error code (e.g. 710022004).
        extra:       Full ``extra`` dict from the STREAM_ERROR payload.
        verify_data: The raw ``decision`` JSON string if the challenge is of
                     type ``"verify"``; empty string otherwise.
    """

    def __init__(self, code: int, msg: str, extra: Optional[Dict[str, Any]] = None):
        self.code = code
        self.extra: Dict[str, Any] = extra or {}
        decision_str: str = self.extra.get("decision", "")
        try:
            decision: Dict[str, Any] = json.loads(decision_str) if decision_str else {}
        except Exception:
            decision = {}
        self.verify_data: str = decision_str if decision.get("type") == "verify" else ""
        super().__init__(f"Rate limited ({code}): {msg}")


@dataclass
class ChatMessage:
    """A single message in a stream_call_bot response."""
    message_id: str = ""
    reply_id: str = ""
    content_type: int = 0
    text: str = ""
    status: int = 0
    chunk_seq: int = -1
    suggestions: List[str] = field(default_factory=list)
    think: str = ""
    event_type: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_user_echo(self) -> bool:
        return self.reply_id == "0"

    @property
    def is_text_chunk(self) -> bool:
        return self.content_type == 1 and not self.is_user_echo

    @property
    def is_answer_chunk(self) -> bool:
        # In Samantha deep-think mode, final answer text can arrive as
        # content_type=2008 with content.text.
        return self.content_type in (1, 2001, 10000, 2008) and not self.is_user_echo

    @property
    def is_suggestion(self) -> bool:
        return self.content_type in (2, 2002)

    @property
    def is_think_chunk(self) -> bool:
        return self.content_type in (2008, 10040) and bool(self.think)


@dataclass
class ChatResult:
    """Aggregated result of a chat completion."""
    text: str = ""
    user_message_id: str = ""
    bot_message_id: str = ""
    suggestions: List[str] = field(default_factory=list)
    thinking_text: str = ""
    events: List[ChatMessage] = field(default_factory=list)


@dataclass
class CompletionChunk:
    """A single incremental chunk from /chat/completion SSE stream.

    The thinking state machine works as follows:
      1st BLOCK_THINKING(10040) -> enter thinking phase
      2nd BLOCK_THINKING(10040) -> exit thinking phase
    Text arriving between the two markers is thinking content;
    text arriving after the second marker is the answer.
    """
    text: str = ""
    thinking: str = ""
    block_type: int = 0
    tool_info: str = ""
    search_info: Optional[Dict[str, Any]] = None
    error_code: int = 0
    error_msg: str = ""
    is_done: bool = False
    conversation_id: str = ""


@dataclass
class CompletionResult:
    """Aggregated result of a /chat/completion call."""
    text: str = ""
    thinking_text: str = ""
    tool_events: List[str] = field(default_factory=list)
    search_results: List[Dict[str, Any]] = field(default_factory=list)
    conversation_id: str = ""


@dataclass
class GeneratedImage:
    """A single generated image from the image generation API.

    Watermark note (updated 2026-10-19, verified by A/B test):
    The server renders watermarks at the CDN (ImageX) level via tplv templates.
    The template suffix decides whether a watermark is burned in:

        ~tplv-*-cgen_lwm3 / cthumb_lwm3   → HAS "豆包 AI 生成" watermark
        ~tplv-*-cgen_dwm3 / cthumb_dwm3   → watermark removed ("d" = 去水印)
        ~tplv-*-image_raw                 → original asset, NO watermark

    Which family the server sends depends on `memory_type`:
        memory_type=2 (ephemeral)   → lwm3 (watermarked)
        memory_type=0 (saved)       → dwm3 / image_raw (watermark-free)

    Signatures cover the whole path including the template, so you cannot
    hand-edit lwm3 → dwm3; the server must hand out the free URL itself.

    Fields:
        ori_url: "download" quality, ~tplv-*-cgen_dwm3 (or lwm3 if watermarked)
        raw_url: "preview" quality, ~tplv-*-cthumb_dwm3
        thumb_url: thumbnail
        ori_raw_url: ~tplv-*-image_raw — watermark-free original, best pick
    """
    key: str = ""
    thumb_url: str = ""
    ori_url: str = ""
    raw_url: str = ""
    ori_raw_url: str = ""
    width: int = 0
    height: int = 0
    format: str = ""


@dataclass
class ImageGenerationResult:
    """Result of an image generation call."""
    images: List[GeneratedImage] = field(default_factory=list)
    prompt: str = ""
    notice: str = ""  # 付费墙/风控等服务端提示（如「订阅标准套餐专属能力」）
    model: str = ""   # 实际生效的模型（新协议从响应回显确认）


def _extract_watermark_free_urls(raw: str) -> Dict[str, str]:
    """从生图 SSE 原文里捞出「无水印」模板的 CDN 直链。

    ImageX 的 tplv 模板后缀决定是否把「豆包 AI 生成」烧进画面（已 A/B 实测）：

        ~tplv-*-cgen_lwm3 / cthumb_lwm3  → 带水印
        ~tplv-*-cgen_dwm3 / cthumb_dwm3  → 去水印
        ~tplv-*-image_raw                → 原始图，无水印

    服务端只在 memory_type=0（保存到历史）时才下发 dwm3 / image_raw，
    阅后即焚(memory_type=2)只给 lwm3。签名覆盖整个 path 含模板，
    所以不能手工把 lwm3 改成 dwm3（会 400），只能让服务端自己给。

    Returns:
        {"image_raw": url, "dwm3": url}（只含存在的键）
    """
    if not raw:
        return {}
    s = raw.replace("\\\\u0026", "&").replace("\\u0026", "&")
    s = s.replace("\\\\/", "/").replace("\\/", "/")
    s = s.replace("\\\\", "").replace("\\", "")
    out: Dict[str, str] = {}
    for m in re.finditer(r'https://[^\s"<>]+rc_gen_image[^\s"<>]+', s):
        u = m.group(0)
        if "~tplv-" not in u:
            continue
        if "-image_raw." in u:
            out.setdefault("image_raw", u)
        elif "-dwm3." in u:
            out.setdefault("dwm3", u)
    return out


@dataclass
class GeneratedVideo:
    """A single generated video from the video generation API."""
    video_url: str = ""
    cover_url: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0
    vid: str = ""


@dataclass
class VideoGenerationResult:
    """Result of a video generation call.

    🔴 2026-10-19 实测结论（ipone 账号端到端跑通）：
    豆包视频是**异步 agent 通道**——``launch_stage=2`` 提交后 SSE 里只有
    agent 文本（「正在为您生成视频…」），没有 task_id / vid / mp4。
    成品由云端渲染完成后作为新消息（``block_type=2074`` 的 creations 卡片）
    推送进会话。取回方式：轮询 ``/alice/conversation/latest_messagelist``。

    🔴 另一个关键点：提交必须 ``memory_type=0``（保存云端）。
    阅后即焚(2)的会话不落库，成品推送无处落地，任务直接孤儿化
    （线程列表不出现、``query_video_gen_info`` 也无记录）。
    """
    videos: List[GeneratedVideo] = field(default_factory=list)
    prompt: str = ""
    conversation_id: str = ""
    agent_reply: str = ""
    task_id: str = ""
    model: str = ""  # 新协议实际生效的模型（旧 agent 通道留空）


@dataclass
class GeneratedMusic:
    """A single generated music track from the music generation API.

    The audio URL is extracted from the video_model field (base64-encoded in response).
    """
    audio_url: str = ""
    title: str = ""
    duration: float = 0.0
    lyrics: str = ""
    cover_url: str = ""
    vid: str = ""


@dataclass
class MusicGenerationResult:
    """Result of a music generation call."""
    tracks: List[GeneratedMusic] = field(default_factory=list)
    prompt: str = ""


@dataclass
class UploadedFile:
    """Metadata for a file uploaded via upload_file().

    Use this object with chat_stream_completion / completion to attach
    files to a chat message.
    """
    uri: str = ""
    name: str = ""
    size: int = 0
    file_type: str = ""


DEFAULT_BOT_ID = "7234781073513644036"
EXTENSION_BOT_ID = "7338286299411103781"

# Supported file extensions for upload (from frontend source)
UPLOAD_SUPPORTED_EXTENSIONS = {
    "pdf", "txt", "csv", "docx", "doc", "xlsx", "xls", "pptx", "ppt",
    "md", "mobi", "epub", "py", "java", "js", "ts", "c", "cpp", "h",
    "hpp", "html", "css", "php", "rb", "pl", "sh", "bash", "swift",
    "kt", "go", "dart", "scala", "cs", "xaml", "vue", "json", "yaml",
    "yml", "xml", "env", "ini", "toml", "plist", "feature", "bat",
    "cmd", "ps1", "vbs", "lua", "mod", "sum", "png", "jpeg", "jpg", "webp",
    "proto", "dockerfile", "rs", "tsx", "jsx",
}


def _aws_sign_v4(
    method: str,
    url: str,
    body: Union[bytes, str],
    access_key: str,
    secret_key: str,
    session_token: str,
    region: str = "cn-north-1",
    service: str = "imagex",
) -> Dict[str, str]:
    """Generate AWS Signature V4 headers for ByteDance ImageX proxy."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or "/"
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    # Canonical query string (sorted)
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    sorted_params = sorted((k, v[0] if v else "") for k, v in query_params.items())
    canonical_qs = "&".join(
        f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted_params
    )

    # Headers to sign
    headers_to_sign: Dict[str, str] = {"host": host, "x-amz-date": amz_date}
    if session_token:
        headers_to_sign["x-amz-security-token"] = session_token
    signed_headers = ";".join(sorted(headers_to_sign.keys()))
    canonical_headers = "".join(
        f"{k}:{v}\n" for k, v in sorted(headers_to_sign.items())
    )

    # Payload hash
    body_bytes = body if isinstance(body, bytes) else body.encode()
    payload_hash = hashlib.sha256(body_bytes).hexdigest()

    # Canonical request
    canonical_request = (
        f"{method}\n{path}\n{canonical_qs}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    cr_hash = hashlib.sha256(canonical_request.encode()).hexdigest()
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{cr_hash}"

    # Derive signing key
    def _sign(key: bytes, msg: str) -> bytes:
        return hmac_mod.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(f"AWS4{secret_key}".encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")

    signature = hmac_mod.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    result = {
        "Authorization": auth,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    if session_token:
        result["x-amz-security-token"] = session_token
    return result


class DoubaoChatClient:
    """
    Async client for Doubao's internal chat API (stream_call_bot).

    Usage:
        async with DoubaoChatClient(cookies, ms_token=token) as client:
            result = await client.chat("你好")
            print(result.text)

            # Or stream:
            async for chunk in client.chat_stream("讲个故事"):
                print(chunk.text, end="", flush=True)
    """

    BASE_URL = "https://www.doubao.com"

    def __init__(
        self,
        cookies: Dict[str, str],
        ms_token: str,
        device_id: str = "714003710229497",
        web_id: str = "7604137868021548590",
        fp: str = "verify_mlcfw5f7_TPq0YmFD_NrsC_4RuQ_BJPg_M5W7i58I7wV0",
        bot_id: str = DEFAULT_BOT_ID,
        timeout_seconds: int = 120,
        captcha_handler: Union[None, str, "CaptchaHandler"] = "auto",
        max_captcha_retries: int = 3,
        force_x_bogus: bool = False,
    ) -> None:
        """
        Args:
            captcha_handler:
                ``"auto"`` (default) — lazily create an ``AutoCaptchaHandler``
                that opens a browser tab when a verify challenge is received.
                ``None`` — disable automatic handling; ``DoubaoRateLimitError``
                is raised immediately (preserving the old behaviour).
                A custom ``CaptchaHandler`` instance — used as-is.
            max_captcha_retries: Maximum number of automatic captcha retries
                before giving up and raising ``DoubaoRateLimitError``.
            force_x_bogus:
                🔴 2026-10-19 起默认 **False**。实测加 X-Bogus 签名会稳定触发
                ``710022004``（shark_admin 人机验证），免签名反而一路通畅。
                保留此开关仅为对照实验/未来签名器修复后回滚用。
        """
        if not cookies:
            raise ValueError("cookies are required")

        self.cookies = cookies
        self.ms_token = ms_token
        self.device_id = device_id
        self.web_id = web_id
        self.fp = fp
        self.bot_id = bot_id
        self.max_captcha_retries = max_captcha_retries
        self.force_x_bogus = force_x_bogus
        # Use sock_read for SSE streams (reset on each chunk/heartbeat).
        # total=None avoids cutting off long expert-mode responses that
        # can exceed 3-4 minutes with multiple search rounds.
        self.timeout = aiohttp.ClientTimeout(
            total=None, sock_read=timeout_seconds
        )
        self._session: Optional[aiohttp.ClientSession] = None

        # Resolve captcha handler
        if captcha_handler == "auto":
            # vendored 版不含 AutoCaptchaHandler（它依赖 doubao2api 的浏览器上下文），
            # 静默降级为无处理器——每次构造 client 都告警一次纯属噪声。
            # 出现 710022004 人机验证时，本项目建议换 IP / 手动过滑块，而非在这里自动重试。
            self._captcha_handler: Optional["CaptchaHandler"] = None
        elif captcha_handler is None:
            self._captcha_handler = None
        else:
            self._captcha_handler = captcha_handler  # type: ignore[assignment]

    async def __aenter__(self) -> "DoubaoChatClient":
        self._session = aiohttp.ClientSession(
            cookies=self.cookies,
            timeout=self.timeout,
            read_bufsize=2**20,  # 1MB; default 64KB causes "Chunk too big" for long SSE lines
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{CHROME_VERSION} Safari/537.36"
                ),
                "Content-Type": "application/json",
                "Origin": "https://www.doubao.com",
                "Referer": "https://www.doubao.com/chat",
            },
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session:
            await self._session.close()
        self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(
                "Client not initialized. Use: async with DoubaoChatClient(...) as client:"
            )
        return self._session

    @staticmethod
    def _check_gateway_error(raw: str) -> None:
        """Check raw SSE text for gateway-error event and raise if found."""
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            event_name = ""
            data_str = ""
            for line in block.strip().split("\n"):
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_str = line[5:].strip()
            if event_name == "gateway-error" and data_str:
                try:
                    err_obj = json.loads(data_str)
                    code = err_obj.get("code", "")
                    msg = err_obj.get("message", data_str)
                except json.JSONDecodeError:
                    code, msg = "", data_str
                raise DoubaoChatError(
                    f"gateway-error: code={code} message={msg}"
                )

    def _security_params(self) -> Dict[str, str]:
        params = {
            "aid": "582478",
            "real_aid": "582478",
            "device_id": self.device_id,
            "tea_uuid": self.device_id,
            "web_id": self.web_id,
            "device_platform": "web",
            "language": "zh",
            "region": "CN",
            "sys_region": "CN",
            "pkg_type": "release_version",
            "version_code": "20800",
            "pc_version": "2.1.7",
            "chromium_version": CHROMIUM_BUILD,
            "client_platform": "pc_client",
            "runtime": "web",
            "runtime_version": "3.5.4",
            "samantha_web": "1",
            "use-olympus-account": "1",
            "fp": self.fp,
            "web_tab_id": str(uuid.uuid4()),
        }
        # Only include msToken if we have a real one;
        # empty/fake values trigger rate limiting (710022002)
        if self.ms_token:
            params["msToken"] = self.ms_token
        return params

    async def _signed_url(self, path: str) -> str:
        """构造请求 URL。

        🔴 2026-10-19 重大更正：X-Bogus 签名已失效，加签名反而触发风控。

        对照实验（同一账号、同一端点 /samantha/chat/completion）：

        | 请求               | 结果                                   |
        |--------------------|----------------------------------------|
        | 普通对话 + 签名    | 710022004 rate limited（shark_admin 人机验证）|
        | 普通对话 + 免签名  | ✅ 正常返回「你好😊」                    |
        | 生图 (skill 5) + 签名 | 710022004 rate limited              |
        | 视频 (skill 17) + 签名 | 710022004 rate limited              |

        结论：签名器产出的 X-Bogus 已不被豆包认可，网关把它判为机器人请求。
        本方法保留为**可选强制签名**入口，默认走免签名。
        """
        params = self._security_params()
        qs = urlencode(dict(sorted(params.items())))
        if not self.force_x_bogus:
            return f"{self.BASE_URL}{path}?{qs}"
        sig = await _get_signer().sign(qs, self.cookies)
        return f"{self.BASE_URL}{path}?{qs}" + (f"&X-Bogus={sig}" if sig else "")

    def _plain_url(self, path: str) -> str:
        """构造免签名 URL（当前唯一稳定可用的方式）。"""
        return f"{self.BASE_URL}{path}?{urlencode(self._security_params())}"

    @staticmethod
    def _encode_payload(obj: Dict[str, Any]) -> str:
        return base64.b64encode(
            json.dumps(obj, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")

    def _build_inner_payload(
        self,
        text: str,
        bot_id: Optional[str] = None,
        conversation_id: str = "0",
        section_id: str = "0",
    ) -> Dict[str, Any]:
        return {
            "event_type": 1,
            "message": {
                "conversation_id": conversation_id,
                "section_id": section_id,
                "local_message_id": str(uuid.uuid4()),
                "content_type": 1,
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "reply_id": "",
                "ext": {
                    "origin": "https://www.doubao.com",
                    "stream": "1",
                    "answer_with_suggest": "1",
                    "browser_language": "zh-CN",
                },
                "local_conversation_id": "0",
                "bot_id": bot_id or self.bot_id,
                "meta_infos": [],
            },
        }

    @staticmethod
    def _parse_alice_sse_message(data: Dict[str, Any]) -> ChatMessage:
        msg = data.get("message", {})
        content_type = msg.get("content_type", 0)
        text = ""
        suggestions: List[str] = []

        try:
            content = json.loads(msg.get("content", "{}"))
            if content_type == 1:
                text = content.get("text", "")
            elif content_type == 2:
                suggestions = content.get("suggest", [])
        except (json.JSONDecodeError, TypeError):
            pass

        return ChatMessage(
            message_id=msg.get("message_id", ""),
            reply_id=msg.get("reply_id", "0"),
            content_type=content_type,
            text=text,
            status=msg.get("status", 0),
            chunk_seq=msg.get("chunk_seq", -1),
            suggestions=suggestions,
            event_type=data.get("event_type", 0),
            raw=data,
        )

    @staticmethod
    def _parse_samantha_sse_message(data: Dict[str, Any]) -> Optional[ChatMessage]:
        event_type = data.get("event_type", 0)
        event_data = data.get("event_data", {})
        if isinstance(event_data, str):
            try:
                event_data = json.loads(event_data)
            except json.JSONDecodeError:
                event_data = {}

        if not isinstance(event_data, dict):
            return None

        # CMPL (2001) carries message chunks, FIN (2003) marks end.
        msg = event_data.get("message", {})
        if isinstance(msg, str):
            try:
                msg = json.loads(msg)
            except json.JSONDecodeError:
                msg = {}

        if not isinstance(msg, dict):
            return None

        content_type = msg.get("content_type", 0)
        content_raw = msg.get("content", {})
        if isinstance(content_raw, str):
            try:
                content = json.loads(content_raw)
            except json.JSONDecodeError:
                content = {}
        elif isinstance(content_raw, dict):
            content = content_raw
        else:
            content = {}

        text = ""
        think = ""
        suggestions: List[str] = []

        if content_type in (2001, 2003, 10000):
            text = content.get("text", "")
        elif content_type == 2008:
            # Deep-think chunks can carry either think text (content.think)
            # or final answer text (content.text).
            think = content.get("think", "")
            text = content.get("text", "")
        elif content_type == 10040:
            think = content.get("think", "")
            if not think:
                think = (
                    content.get("finish_title", "")
                    or content.get("title", "")
                    or content.get("text", "")
                )
        elif content_type == 2002:
            suggest_value = content.get("suggest", [])
            if isinstance(suggest_value, list):
                suggestions = suggest_value
            elif isinstance(suggest_value, str):
                suggestions = [suggest_value]

        return ChatMessage(
            message_id=msg.get("message_id", ""),
            reply_id=msg.get("reply_id", ""),
            content_type=content_type,
            text=text,
            think=think,
            status=event_data.get("status", 0),
            chunk_seq=msg.get("chunk_seq", -1),
            suggestions=suggestions,
            event_type=event_type,
            raw=data,
        )

    async def chat_stream(
        self,
        text: str,
        bot_id: Optional[str] = None,
        conversation_id: str = "0",
        section_id: str = "0",
    ) -> AsyncIterator[ChatMessage]:
        """
        Send a message and yield ChatMessage objects as they arrive.

        Yields user echo first, then bot reply chunks, then suggestions.
        """
        inner = self._build_inner_payload(text, bot_id, conversation_id, section_id)
        encoded = self._encode_payload(inner)
        params = self._security_params()
        url = f"{self.BASE_URL}/alice/message/stream_call_bot?{urlencode(params)}"

        async with self.session.post(
            url,
            json={"payload": encoded},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise DoubaoChatError(
                    f"stream_call_bot failed ({resp.status}): {error_text[:500]}"
                )

            async for event in iter_sse_events(resp.content):
                if event["event"] == "done":
                    return
                if event["event"] == "json" and event.get("json"):
                    yield self._parse_alice_sse_message(event["json"])

    async def chat_stream_samantha(
        self,
        text: str,
        bot_id: Optional[str] = None,
        use_deep_think: bool = False,
        use_auto_cot: bool = False,
    ) -> AsyncIterator[ChatMessage]:
        """
        Stream from /samantha/chat/completion.

        Think traces are in CMPL payloads with content_type=2008 and content.think.
        """
        url = (
            f"{self.BASE_URL}/samantha/chat/completion?"
            f"{urlencode(self._security_params())}"
        )

        sent_event = {
            "messages": [
                {
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                    "content_type": 2001,
                    "attachments": [],
                    "references": [],
                }
            ],
            "completion_option": {
                "is_regen": False,
                "with_suggest": True,
                "need_create_conversation": True,
                "launch_stage": 1,
                "is_replace": False,
                "is_delete": False,
                "is_ai_playground": False,
                "memory_type": 2,
                "message_from": 0,
                "use_deep_think": use_deep_think,
                "use_auto_cot": use_auto_cot,
                "resend_for_regen": False,
                "enable_commerce_credit": False,
            },
            "evaluate_option": {"web_ab_params": ""},
            "local_conversation_id": str(uuid.uuid4()),
            "local_message_id": str(uuid.uuid4()),
        }
        if bot_id:
            sent_event["bot_id"] = bot_id

        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Agw-Js-Conv": "str",
        }

        async with self.session.post(
            url,
            data=json.dumps(sent_event, ensure_ascii=False),
            headers=headers,
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise DoubaoChatError(
                    f"chat/completion failed ({resp.status}): {error_text[:500]}"
                )

            raw_text = (await resp.read()).decode("utf-8", errors="replace")

            # Check if response is a JSON error (not SSE) — e.g. session expired
            if raw_text.lstrip().startswith("{"):
                try:
                    err_obj = json.loads(raw_text)
                    if isinstance(err_obj, dict) and "code" in err_obj:
                        code = err_obj.get("code", 0)
                        msg = err_obj.get("msg") or err_obj.get("message", raw_text[:200])
                        raise DoubaoChatError(
                            f"samantha/chat/completion auth error: code={code} msg={msg}"
                        )
                except json.JSONDecodeError:
                    pass

            for block in raw_text.split("\n\n"):
                if not block.strip():
                    continue

                event_name = "message"
                data_str = ""
                for line in block.strip().split("\n"):
                    if line.startswith("event:"):
                        event_name = line[6:].strip() or "message"
                    elif line.startswith("data:"):
                        data_str = line[5:].strip()

                if not data_str:
                    continue

                # Gateway-level auth error (session expired / user invalid)
                if event_name == "gateway-error":
                    try:
                        err_obj = json.loads(data_str)
                        code = err_obj.get("code", "")
                        msg = err_obj.get("message", data_str)
                    except json.JSONDecodeError:
                        code, msg = "", data_str
                    raise DoubaoChatError(
                        f"gateway-error: code={code} message={msg}"
                    )

                if event_name not in ("message", "json"):
                    continue

                try:
                    data_json = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                parsed = self._parse_samantha_sse_message(data_json)
                if parsed is None:
                    event_json = data_json
                    if event_json.get("event_type") == 2005:
                        detail = event_json.get("event_data", "")
                        raise DoubaoChatError(
                            f"chat/completion returned ERR event: {str(detail)[:500]}"
                        )
                    continue
                if parsed.event_type == 2003:
                    return
                yield parsed

    async def chat(
        self,
        text: str,
        bot_id: Optional[str] = None,
        conversation_id: str = "0",
        section_id: str = "0",
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> ChatResult:
        """
        Send a message and collect the full response.

        Args:
            text: The message text to send.
            bot_id: Override the default bot_id.
            conversation_id: Conversation ID ("0" for stateless).
            section_id: Section ID ("0" for default).
            on_chunk: Optional callback called with each text chunk as it arrives.

        Returns:
            ChatResult with aggregated text, message IDs, and suggestions.
        """
        result = ChatResult()
        text_parts: List[str] = []

        async for msg in self.chat_stream(text, bot_id, conversation_id, section_id):
            result.events.append(msg)

            if msg.is_user_echo and not result.user_message_id:
                result.user_message_id = msg.message_id
                continue

            if msg.is_text_chunk:
                if not result.bot_message_id:
                    result.bot_message_id = msg.message_id
                text_parts.append(msg.text)
                if on_chunk and msg.text:
                    on_chunk(msg.text)

            if msg.is_suggestion and msg.suggestions:
                result.suggestions.extend(msg.suggestions)

        result.text = "".join(text_parts)
        return result

    async def chat_samantha(
        self,
        text: str,
        bot_id: Optional[str] = None,
        use_deep_think: bool = False,
        use_auto_cot: bool = False,
        on_chunk: Optional[Callable[[str], None]] = None,
        on_think_chunk: Optional[Callable[[str], None]] = None,
    ) -> ChatResult:
        """
        Send a message via /samantha/chat/completion and collect full response.
        """
        result = ChatResult()
        text_parts: List[str] = []
        think_parts: List[str] = []

        async for msg in self.chat_stream_samantha(
            text=text,
            bot_id=bot_id,
            use_deep_think=use_deep_think,
            use_auto_cot=use_auto_cot,
        ):
            result.events.append(msg)

            if msg.is_answer_chunk and msg.text:
                if not result.bot_message_id:
                    result.bot_message_id = msg.message_id
                text_parts.append(msg.text)
                if on_chunk:
                    on_chunk(msg.text)

            if msg.is_think_chunk and msg.think:
                think_parts.append(msg.think)
                if on_think_chunk:
                    on_think_chunk(msg.think)

            if msg.is_suggestion and msg.suggestions:
                result.suggestions.extend(msg.suggestions)

        result.text = "".join(text_parts)
        result.thinking_text = "".join(think_parts)
        return result

    # ── /chat/completion (unified main entry) ──────────────────

    @staticmethod
    def _build_completion_content_blocks(
        text: str,
        image_attachments: Optional[List[Dict[str, str]]] = None,
        file_attachments: Optional[List["UploadedFile"]] = None,
    ) -> List[Dict[str, Any]]:
        """Build content_block list for /chat/completion payload.

        Args:
            text: User message text.
            image_attachments: Optional list of dicts with keys
                ``uri``, ``cdn_url``, ``width``, ``height``, ``format``, ``name``.
            file_attachments: Optional list of UploadedFile objects from upload_file().
        """
        blocks: List[Dict[str, Any]] = []

        # File attachments (type=3) — must come before text block
        if file_attachments:
            attachments_list = []
            for f in file_attachments:
                attachments_list.append({
                    "type": 3,
                    "identifier": str(uuid.uuid4()),
                    "file": {
                        "uri": f.uri,
                        "url": "",
                        "file_type": 0,
                        "name": f.name,
                        "size": f.size,
                    },
                    "parse_state": 1,
                    "review_state": 1,
                    "upload_status": 1,
                    "progress": 100,
                    "src": "",
                })
            blocks.append({
                "block_type": 10052,
                "content": {
                    "attachment_block": {"attachments": attachments_list},
                    "pc_event_block": "",
                },
                "block_id": str(uuid.uuid4()),
                "parent_id": "",
                "meta_info": [],
                "append_fields": [],
            })

        # Image attachments (type=1)
        for img in image_attachments or []:
            blocks.append({
                "block_type": 10052,
                "content": {
                    "attachment_block": {
                        "attachments": [{
                            "type": 1,
                            "identifier": str(uuid.uuid4()),
                            "image": {
                                "name": img.get("name", "image.png"),
                                "uri": img["uri"],
                                "image_ori": {
                                    "url": img["cdn_url"],
                                    "width": int(img.get("width", 1024)),
                                    "height": int(img.get("height", 1024)),
                                    "format": img.get("format", "png"),
                                    "url_formats": {},
                                },
                            },
                            "parse_state": 0,
                            "review_state": 0,
                        }],
                    },
                    "pc_event_block": "",
                },
                "block_id": str(uuid.uuid4()),
                "parent_id": "",
                "meta_info": [],
                "append_fields": [],
                "is_finish": True,
                "patch_type": 2,
            })
        blocks.append({
            "block_type": 10000,
            "content": {
                "text_block": {
                    "text": text,
                    "icon_url": "",
                    "icon_url_dark": "",
                    "summary": "",
                },
                "pc_event_block": "",
            },
            "block_id": str(uuid.uuid4()),
            "parent_id": "",
            "meta_info": [],
            "append_fields": [],
            "is_finish": True,
            "patch_type": 2,
        })
        return blocks

    def _build_completion_payload(
        self,
        text: str,
        need_deep_think: int = 0,
        bot_id: Optional[str] = None,
        image_attachments: Optional[List[Dict[str, str]]] = None,
        file_attachments: Optional[List["UploadedFile"]] = None,
    ) -> Dict[str, Any]:
        content_blocks = self._build_completion_content_blocks(
            text, image_attachments, file_attachments,
        )
        return {
            "client_meta": {
                "local_conversation_id": f"local_{uuid.uuid4().hex[:16]}",
                "conversation_id": "",
                "bot_id": bot_id or EXTENSION_BOT_ID,
                "last_section_id": "",
                "last_message_index": 0,
            },
            "messages": [{
                "local_message_id": str(uuid.uuid4()),
                "content_block": content_blocks,
                "message_status": 0,
            }],
            "option": {
                "send_message_scene": "",
                "create_time_ms": 0,
                "collect_id": "",
                "is_audio": False,
                "answer_with_suggest": True,
                "tts_switch": False,
                "need_deep_think": need_deep_think,
                "click_clear_context": False,
                "from_suggest": False,
                "is_regen": False,
                "is_replace": False,
                "disable_sse_cache": False,
                "select_text_action": "",
                "resend_for_regen": False,
                "scene_type": 0,
                "unique_key": str(uuid.uuid4()),
                "start_seq": 0,
                "need_create_conversation": True,
                "conversation_init_option": {"need_ack_conversation": True},
                "regen_query_id": [],
                "edit_query_id": [],
                "regen_instruction": "",
                "no_replace_for_regen": False,
                "message_from": 0,
                "shared_app_name": "",
                "action_bar_skill_id": 0,
                "sse_recv_event_options": {"support_chunk_delta": True},
                "is_ai_playground": False,
            },
            "chat_ability": {},
            "ext": {
                "use_deep_think": str(need_deep_think),
                "fp": self.fp,
                "use_submit_pipeline": "1",
                "commerce_credit_config_enable": "0",
                "sub_conv_firstmet_type": "1",
            },
        }

    async def _chat_stream_completion_once(
        self,
        text: str,
        need_deep_think: int = 0,
        bot_id: Optional[str] = None,
        image_attachments: Optional[List[Dict[str, str]]] = None,
        file_attachments: Optional[List["UploadedFile"]] = None,
    ) -> AsyncIterator[CompletionChunk]:
        """Single-attempt stream from /chat/completion. May raise DoubaoRateLimitError."""
        payload = self._build_completion_payload(
            text, need_deep_think, bot_id, image_attachments, file_attachments,
        )
        params = self._security_params()
        url = f"{self.BASE_URL}/chat/completion?{urlencode(params)}"

        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "x-tt-passport-csrf-token": (
                self.cookies.get("passport_csrf_token")
                or self.cookies.get("passport_csrf_token_default")
                or ""
            ),
        }

        thinking_count = 0
        in_thinking = False

        async with self.session.post(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise DoubaoChatError(
                    f"/chat/completion failed ({resp.status}): {error_text[:500]}"
                )

            # Check if response is a JSON error instead of SSE stream
            # (e.g. session expired: {"code":710012001,"msg":"登录已过期"})
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                body_text = await resp.text()
                try:
                    err_obj = json.loads(body_text)
                    if isinstance(err_obj, dict) and "code" in err_obj:
                        code = err_obj.get("code", 0)
                        msg = err_obj.get("msg") or err_obj.get("message", body_text[:200])
                        raise DoubaoChatError(
                            f"/chat/completion auth error: code={code} msg={msg}"
                        )
                except json.JSONDecodeError:
                    pass
                # Check for gateway-error in case body is SSE-like
                self._check_gateway_error(body_text)
                # Unknown non-SSE response — raise generic error
                raise DoubaoChatError(
                    f"/chat/completion unexpected response (content-type={content_type}): "
                    f"{body_text[:300]}"
                )

            async for event in iter_sse_events(resp.content):
                obj = event.get("json")
                if obj is None:
                    continue

                sse_event_name = event.get("event", "")

                # Gateway-level auth error (session invalid / user invalid)
                if sse_event_name == "gateway-error":
                    code = obj.get("code", "")
                    msg = obj.get("message", str(obj))
                    raise DoubaoChatError(
                        f"gateway-error: code={code} message={msg}"
                    )

                # Capture conversation_id from SSE_ACK event
                if sse_event_name == "SSE_ACK":
                    ack = obj.get("ack_client_meta", {})
                    if isinstance(ack, dict):
                        cid = str(ack.get("conversation_id", "")).strip()
                        if cid and cid != "0":
                            yield CompletionChunk(conversation_id=cid)
                    continue

                if sse_event_name == "STREAM_ERROR" or "error_code" in obj:
                    code = int(obj.get("error_code", 0))
                    msg = str(obj.get("error_msg", ""))
                    if code in RISK_ERROR_CODES:
                        raise DoubaoRateLimitError(code, msg, extra=obj.get("extra"))
                    if code:
                        yield CompletionChunk(error_code=code, error_msg=msg)
                        continue

                # --- chunk_delta: compact {"text": "..."} (exclude error payloads) ---
                if (
                    "text" in obj
                    and isinstance(obj.get("text"), str)
                    and "error_code" not in obj
                ):
                    t = obj["text"]
                    if in_thinking:
                        yield CompletionChunk(thinking=t, block_type=10040)
                    else:
                        yield CompletionChunk(text=t, block_type=10000)
                    continue

                # --- process content_block arrays ---
                def _iter_blocks(data: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
                    for patch in data.get("patch_op", []):
                        pv = patch.get("patch_value", {})
                        yield from pv.get("content_block", [])
                    dc = data.get("content", {})
                    if isinstance(dc, dict):
                        yield from dc.get("content_block", [])

                for cb in _iter_blocks(obj):
                    bt = cb.get("block_type", 0)
                    content = cb.get("content", {})

                    if bt == 10040:
                        thinking_count += 1
                        in_thinking = thinking_count == 1
                        yield CompletionChunk(block_type=bt)

                    elif bt == 10000:
                        tb = content.get("text_block", {})
                        if isinstance(tb, dict) and tb.get("text"):
                            t = tb["text"]
                            if in_thinking:
                                yield CompletionChunk(
                                    thinking=t, block_type=bt,
                                )
                            else:
                                yield CompletionChunk(text=t, block_type=bt)

                    elif bt == 10101:
                        lb = content.get("loading_block", {})
                        tl = lb.get("text_loading", {})
                        if tl.get("text"):
                            yield CompletionChunk(
                                tool_info=tl["text"], block_type=bt,
                            )

                    elif bt == 10024:
                        gtb = content.get("generic_tool_block", {})
                        title = gtb.get("title", "")
                        if title:
                            yield CompletionChunk(
                                tool_info=f"[tool] {title}",
                                block_type=bt,
                            )

                    elif bt == 10025:
                        sqrb = content.get("search_query_result_block", {})
                        if sqrb:
                            queries = sqrb.get("queries", [])
                            summary = sqrb.get("summary", "")
                            results = sqrb.get("results", [])
                            is_finish = cb.get("is_finish", False)
                            block_id = cb.get("block_id", "")
                            parsed_results = [
                                {
                                    "title": r.get("text_card", {}).get("title", ""),
                                    "url": r.get("text_card", {}).get("url", ""),
                                    "summary": r.get("text_card", {}).get("summary", ""),
                                    "source": r.get("text_card", {}).get("source_name", ""),
                                }
                                for r in results
                                if r.get("text_card")
                            ]
                            search_data = {
                                "type": "search",
                                "summary": summary,
                                "queries": queries,
                                "is_finish": is_finish,
                                "block_id": block_id,
                                "results": parsed_results,
                            }
                            # Always emit search_info when there are results,
                            # so the consumer can track incremental updates.
                            # The consumer (_stream_chat) deduplicates by block_id.
                            if parsed_results:
                                yield CompletionChunk(
                                    tool_info=f"[search] {summary}",
                                    search_info=search_data,
                                    block_type=bt,
                                )
                            else:
                                # Still emit tool_info for loading indicators
                                yield CompletionChunk(
                                    tool_info=f"[search] {summary}",
                                    block_type=bt,
                                )

                # --- process patch_op content strings (new STREAM_CHUNK format) ---
                for patch in obj.get("patch_op", []):
                    pv = patch.get("patch_value", {})
                    if isinstance(pv, dict) and "content" in pv:
                        content_str = pv.get("content", "")
                        if isinstance(content_str, str) and content_str:
                            try:
                                content_obj = json.loads(content_str)
                                t = content_obj.get("text", "")
                                if t:
                                    if in_thinking:
                                        yield CompletionChunk(thinking=t, block_type=10000)
                                    else:
                                        yield CompletionChunk(text=t, block_type=10000)
                            except (json.JSONDecodeError, TypeError):
                                pass

                if obj.get("verbose_type"):
                    yield CompletionChunk(
                        tool_info=(
                            f"[{obj['verbose_type']}] "
                            f"{str(obj.get('verbose_payload', ''))[:200]}"
                        ),
                    )

        yield CompletionChunk(is_done=True)

    async def chat_stream_completion(
        self,
        text: str,
        need_deep_think: int = 0,
        bot_id: Optional[str] = None,
        image_attachments: Optional[List[Dict[str, str]]] = None,
        file_attachments: Optional[List["UploadedFile"]] = None,
    ) -> AsyncIterator[CompletionChunk]:
        """Stream from /chat/completion with automatic captcha handling.

        Yields ``CompletionChunk`` objects.  When a rate-limit / verify challenge
        is returned by the server, the configured ``captcha_handler`` is invoked
        and the request is retried transparently (up to ``max_captcha_retries``
        times).  If no handler is set or retries are exhausted, a
        ``DoubaoRateLimitError`` is raised.

        Args:
            text: User message text.
            need_deep_think: 0=quick, 1=think, 2=auto, 3=expert.
            bot_id: Override default bot_id.
            image_attachments: Pre-uploaded images (see ``upload_image``).
            file_attachments: Pre-uploaded files (see ``upload_file``).
        """
        for attempt in range(self.max_captcha_retries + 1):
            has_yielded_content = False
            try:
                async for chunk in self._chat_stream_completion_once(
                    text, need_deep_think, bot_id, image_attachments, file_attachments
                ):
                    if chunk.text or chunk.thinking:
                        has_yielded_content = True
                    yield chunk
                return  # Clean finish — no error
            except DoubaoRateLimitError as exc:
                # Never retry if we already yielded content to the caller —
                # retrying would produce duplicate/corrupted output.
                if has_yielded_content:
                    raise
                # Re-raise immediately if no handler or no verify challenge
                if not self._captcha_handler or not exc.verify_data:
                    raise
                if attempt >= self.max_captcha_retries:
                    raise
                resolved = await self._captcha_handler.handle(
                    exc.verify_data, self.fp, self.device_id
                )
                if not resolved:
                    raise

    async def chat_completion(
        self,
        text: str,
        need_deep_think: int = 0,
        bot_id: Optional[str] = None,
        image_attachments: Optional[List[Dict[str, str]]] = None,
        file_attachments: Optional[List["UploadedFile"]] = None,
        on_chunk: Optional[Callable[[str], None]] = None,
        on_think_chunk: Optional[Callable[[str], None]] = None,
    ) -> CompletionResult:
        """Non-streaming wrapper around ``chat_stream_completion``."""
        result = CompletionResult()
        text_parts: List[str] = []
        think_parts: List[str] = []

        async for chunk in self.chat_stream_completion(
            text=text,
            need_deep_think=need_deep_think,
            bot_id=bot_id,
            image_attachments=image_attachments,
            file_attachments=file_attachments,
        ):
            if chunk.is_done:
                break
            if chunk.conversation_id:
                result.conversation_id = chunk.conversation_id
            if chunk.text:
                text_parts.append(chunk.text)
                if on_chunk:
                    on_chunk(chunk.text)
            if chunk.thinking:
                think_parts.append(chunk.thinking)
                if on_think_chunk:
                    on_think_chunk(chunk.thinking)
            if chunk.tool_info:
                result.tool_events.append(chunk.tool_info)
            if chunk.search_info:
                result.search_results.append(chunk.search_info)

        result.text = "".join(text_parts)
        result.thinking_text = "".join(think_parts)
        return result

    async def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation from Doubao to keep the sidebar clean.

        Uses POST /samantha/thread/delete with the conversation_id (as thread_id)
        obtained from the SSE_ACK event during chat completion.

        Returns True if deletion succeeded, False otherwise.
        """
        if not conversation_id or conversation_id == "0":
            return False

        url = f"{self.BASE_URL}/samantha/thread/delete"
        payload = {"thread_id": str(conversation_id)}
        headers = {
            "Content-Type": "application/json",
            "x-tt-passport-csrf-token": (
                self.cookies.get("passport_csrf_token")
                or self.cookies.get("passport_csrf_token_default")
                or ""
            ),
        }

        try:
            async with self._session.post(
                url,
                params=self._security_params(),
                json=payload,
                headers=headers,
                cookies=self.cookies,
            ) as resp:
                if resp.status != 200:
                    log.warning(
                        "delete_conversation %s: HTTP %d", conversation_id, resp.status
                    )
                    return False
                data = await resp.json()
                code = data.get("code", -1)
                if code == 0:
                    log.debug("Deleted conversation %s", conversation_id)
                    return True
                log.warning(
                    "delete_conversation %s: code=%d msg=%s",
                    conversation_id, code, data.get("msg", ""),
                )
                return False
        except Exception as exc:
            log.warning("delete_conversation %s failed: %s", conversation_id, exc)
            return False

    # ---- 新统一端点协议（2026-10-19 抓包地面真值） ----
    # 真实前端走 POST /chat/completion（免签名；浏览器自带 a_bogus 反而触发 710022004），
    # 模型/比例/风格选择挂在 chat_ability.ability_param 里，与 samantha 的 skill content
    # 字典是两套协议。samantha 格式会把 content 剥成纯文本走 agent 通道，模型选择被忽略
    # （实测：image-model 键不被解析；视频的 video-model 同样可能被忽略）。

    def _new_endpoint_params(self) -> Dict[str, str]:
        """新端点 /chat/completion 的安全参数（抓包自真实前端，aid=497858）。"""
        return {
            "aid": "497858", "real_aid": "497858",
            "device_id": "7698374278500353570",
            "tea_uuid": "7698374278500353570",
            "web_id": "7698374278500353570",
            "device_platform": "web",
            "doubao_device_platform": "web",
            "doubao_pc_version": "3.41.4",
            "language": "zh", "region": "CN", "sys_region": "CN",
            "pkg_type": "release_version", "version_code": "20800",
            "pc_version": "3.41.4",
            "chromium_version": CHROMIUM_BUILD,
            "client_platform": "pc_client", "runtime": "web",
            "runtime_version": "3.5.4", "samantha_web": "1",
            "use-olympus-account": "1",
            "fp": "verify_mvfb2gzc_nST1AVAS_3Mpe_4uAR_8WAF_mxtjs",
            "web_tab_id": str(uuid.uuid4()),
        }

    @staticmethod
    def _build_ability_payload(
        prompt: str, ability_type: int,
        ability_param_inner: Dict[str, Any], reply_format: str,
        text_suffix: str = "",
    ) -> Dict[str, Any]:
        """构造新端点 payload（client_meta/messages/option/chat_ability）。

        ability_param_inner 为 ability_param 的**内部结构**（json.dumps 后放入）：
          - 图片（ability_type=3）用嵌套包装：
            {"ability_param": {...}, "ability_type": 1}
          - 视频（ability_type=17）用扁平结构：
            {"ratio", "model", "duration", "input_box_content"}
        （两者均为 2026-10-19 抓包自真实前端的地面真值。）
        """
        now_ms = int(time.time() * 1000)
        return {
            "client_meta": {
                "local_conversation_id": f"local_{now_ms}",
                "conversation_id": "",
                "bot_id": "7338286299411103781",
                "last_section_id": "",
                "last_message_index": None,
                "local_permissions": [
                    {"permission_name": "ACCESS_COARSE_LOCATION", "status": 3},
                    {"permission_name": "ACCESS_FINE_LOCATION", "status": 3},
                    {"permission_name": "ACCESS_BACKGROUND_LOCATION", "status": 3},
                ],
            },
            "messages": [{
                "local_message_id": str(uuid.uuid4()),
                "content_block": [{
                    "block_type": 10000,
                    "content": {
                        "text_block": {
                            "text": f"{reply_format % prompt}{text_suffix}",
                            "icon_url": "", "icon_url_dark": "", "summary": "",
                        },
                        "pc_event_block": "",
                    },
                    "block_id": str(uuid.uuid4()),
                    "parent_id": "",
                    "meta_info": [],
                    "extra_info": {},
                    "append_fields": [],
                }],
                "message_status": 0,
            }],
            "option": {
                "send_message_scene": "",
                "create_time_ms": now_ms,
                "collect_id": "",
                "is_audio": False,
                "answer_with_suggest": False,
                "tts_switch": False,
                "need_deep_think": 0,
                "click_clear_context": False,
                "from_suggest": False,
                "is_regen": False,
                "is_replace": False,
                "is_from_click_option": False,
                "is_from_click_softlink": False,
                "disable_sse_cache": False,
                "select_text_action": "",
                "is_select_text": False,
                "resend_for_regen": False,
                "scene_type": 0,
                "unique_key": str(uuid.uuid4()),
                "start_seq": 0,
                "need_create_conversation": True,
                "conversation_init_option": {"need_ack_conversation": True},
                "regen_query_id": [],
                "edit_query_id": [],
                "regen_instruction": "",
                "no_replace_for_regen": False,
                "message_from": 0,
                "shared_app_name": "",
                "shared_app_id": "",
                "sse_recv_event_options": {"support_chunk_delta": True},
                "support_lazy_fetch_stream": True,
                "is_ai_playground": False,
                "is_old_user": True,
                "general_task_param": {"server_do_compare": True},
                "recovery_option": {"is_recovery": False,
                                    "req_create_time_sec": int(time.time()),
                                    "append_sse_event_scene": 0},
                "message_storage_type": 0,
                "related_deleted_message_ids": {},
                "connector_info_list": [],
                "model_config": {"model_item_key": "", "model_extra_params": {}},
                "aggregate_params": {
                    "mention_skill_list": "[]",
                    "mention_plugin_list": "[]",
                    "mention_ext": "[{}]",
                    "conversation_mode": "1",
                    "mode_id": "",
                    "model_item_key": "",
                    "agent_mode": "",
                    "reasoning_effort": "",
                    "provider_id": "",
                },
                "conversation_mode": 1,
            },
            "chat_ability": {
                "ability_type": ability_type,
                "ability_param": json.dumps(ability_param_inner, ensure_ascii=False),
            },
            "user_context": [],
            "ext": {
                "answer_with_suggest": "0",
                "sub_conv_firstmet_type": "1",
                "general_task_param": "{\"server_do_compare\":true}",
                "collection_id": "",
                "is_finish": "1",
                "conversation_init_option": "{\"need_ack_conversation\":true}",
                "commerce_credit_config_enable": "0",
            },
        }

    async def _post_ability(
        self, payload: Dict[str, Any], timeout: float
    ) -> str:
        """POST 新端点 /chat/completion（免签名），返回 SSE 原文。"""
        params = self._new_endpoint_params()
        url = (
            f"{self.BASE_URL}/chat/completion?"
            f"{urlencode(dict(sorted(params.items())))}"
        )
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Agw-Js-Conv": "str",
            "Referer": "https://www.doubao.com/chat/",
        }
        async with self.session.post(
            url,
            data=json.dumps(payload, ensure_ascii=False),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise DoubaoChatError(
                    f"ability request failed ({resp.status}): {error_text[:500]}"
                )
            return (await resp.read()).decode("utf-8", errors="replace")

    def _parse_image_ability_sse(
        self, raw: str, prompt: str, requested_model: str
    ) -> "ImageGenerationResult":
        """解析新端点的生图 SSE（命名事件 + patch_op 增量结构）。

        成品在 STREAM_CHUNK 的 patch_op → content_block → block_type=2074
        → creation_block.creations[].image；URL 优先级
        image_ori_raw（image_raw 无水印）> image_ori > image_thumb。
        """
        result = ImageGenerationResult(prompt=prompt, model=requested_model)
        notice_parts: List[str] = []

        def _collect_image(img: Dict[str, Any]) -> None:
            if not isinstance(img, dict):
                return
            ori = img.get("image_ori", {}) or {}
            raw_img = img.get("image_ori_raw", {}) or {}
            thumb = img.get("image_thumb", {}) or {}
            result.images.append(GeneratedImage(
                key=img.get("key", ""),
                thumb_url=thumb.get("url", ""),
                ori_url=ori.get("url", ""),
                raw_url=raw_img.get("url", ""),
                ori_raw_url=raw_img.get("url", ""),
                width=ori.get("width", 0) or thumb.get("width", 0),
                height=ori.get("height", 0) or thumb.get("height", 0),
                format=ori.get("format", "") or thumb.get("format", ""),
            ))

        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            event_name = ""
            data_str = ""
            for line in block.strip().split("\n"):
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_str += line[5:].strip()
            if not data_str:
                continue
            if event_name == "STREAM_ERROR":
                try:
                    err = json.loads(data_str)
                    code = err.get("error_code", "")
                    if code in RISK_ERROR_CODES:
                        raise DoubaoRateLimitError(
                            f"ability rate limited: {code}")
                    raise DoubaoChatError(
                        f"ability stream error: {str(err)[:300]}")
                except json.JSONDecodeError:
                    pass
            if event_name not in ("STREAM_CHUNK", "FULL_MSG_NOTIFY",
                                  "STREAM_MSG_NOTIFY"):
                continue
            try:
                d = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            # 增量 patch
            for patch in d.get("patch_op", []) or []:
                pv = patch.get("patch_value", {})
                if not isinstance(pv, dict):
                    continue
                for cb in pv.get("content_block", []) or []:
                    if not isinstance(cb, dict) or cb.get("block_type") != 2074:
                        continue
                    creation = (cb.get("content", {}) or {}).get(
                        "creation_block", {}) or {}
                    for cr in creation.get("creations", []) or []:
                        if isinstance(cr, dict) and cr.get("image"):
                            _collect_image(cr["image"])
            # 完整消息（FULL_MSG_NOTIFY）
            msg = d.get("message", {})
            if isinstance(msg, dict):
                for cb in msg.get("content_block", []) or []:
                    if not isinstance(cb, dict) or cb.get("block_type") != 2074:
                        continue
                    creation = (cb.get("content", {}) or {}).get(
                        "creation_block", {}) or {}
                    for cr in creation.get("creations", []) or []:
                        if isinstance(cr, dict) and cr.get("image"):
                            _collect_image(cr["image"])
            # 服务端提示文本（付费墙等）
            text_blob = data_str
            for kw in ("订阅标准套餐专属能力", "不可用于创作", "升级正式订阅",
                       "免费体验套餐"):
                if kw in text_blob:
                    notice_parts.append(kw)

        if notice_parts and not result.images:
            result.notice = (
                "当前账号无该模型的创作额度（服务端返回订阅提示），"
                "未出图。可切换到免费档模型，或升级订阅。"
            )
        # 去重（同一张图可能同时出现在 patch 与 full notify 里）
        seen = set()
        uniq = []
        for img in result.images:
            k = img.ori_raw_url or img.ori_url or img.thumb_url or img.key
            if k and k not in seen:
                seen.add(k)
                uniq.append(img)
        result.images = uniq
        return result

    async def _generate_image_ability(
        self,
        prompt: str,
        ratio: Optional[str],
        image_model: str,
        style: str,
        timeout: float,
    ) -> "ImageGenerationResult":
        """新协议文生图：模型/比例/风格精确选择（chat_ability 通道）。"""
        model = image_model or "Seedream 4.0"
        text = prompt
        if style:
            text = f"{prompt}。图片风格为{IMAGE_STYLE_LABELS.get(style, style)}"
        # 图片的 ability_param 是嵌套包装（抓包真值）
        inner = {
            "ability_param": {
                "model": model,
                "ratio": ratio or "auto",
                "input_box_content": {
                    "user_input_content": text,
                    "reply_message_format": "生成图片：%s",
                },
            },
            "ability_type": 1,
        }
        payload = self._build_ability_payload(
            prompt, 3, inner, "生成图片：%s",
        )
        raw = await self._post_ability(payload, timeout)
        return self._parse_image_ability_sse(raw, prompt, model)

    @staticmethod
    def _parse_ability_ack(raw: str) -> Tuple[str, str]:
        """从新端点 SSE 提取 (conversation_id, agent 文本)。

        SSE_ACK 的 ack_client_meta 里有真实 conversation_id；
        STREAM_CHUNK 的文本块里是 agent 回复（付费墙提示等）。
        """
        conv_id, _local, text = DoubaoChatClient._parse_ability_ack_full(raw)
        return conv_id, text

    @staticmethod
    def _parse_ability_ack_full(raw: str) -> Tuple[str, str, str]:
        """从新端点 SSE 提取 (conversation_id, local_conversation_id, agent 文本)。

        local_conversation_id 用于第二步「确认」追问时续接会话上下文。

        文本提取规则（2026-10-19 抓包真值）：agent 回复的完整文本分散在
        **两类事件**里，必须都收集并按流顺序拼接——
          - ``STREAM_CHUNK`` 的 patch_op 只带**首字符**（建块时的初始 patch）；
          - 其余全文在后续的 ``CHUNK_DELTA`` 增量里（``{"text": "..."}``）。
        只解析 patch_op 会只拿到一个字（曾导致确认卡识别失败、视频任务
        永远停在确认步）。
        """
        conv_id = ""
        local_id = ""
        texts: List[str] = []
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            event_name = ""
            data_str = ""
            for line in block.strip().split("\n"):
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_str += line[5:].strip()
            if not data_str:
                continue
            try:
                d = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if event_name == "SSE_ACK":
                meta = d.get("ack_client_meta", {}) or {}
                cid = meta.get("conversation_id", "")
                if cid and str(cid).isdigit() and len(str(cid)) >= 15:
                    conv_id = str(cid)
                lid = meta.get("local_conversation_id", "")
                if lid:
                    local_id = str(lid)
            # 文本来源 1：STREAM_CHUNK 的 patch_op（首字符）
            for patch in d.get("patch_op", []) or []:
                pv = patch.get("patch_value", {})
                if not isinstance(pv, dict):
                    continue
                for cb in pv.get("content_block", []) or []:
                    if not isinstance(cb, dict) or cb.get("block_type") != 10000:
                        continue
                    tb = (cb.get("content", {}) or {}).get("text_block", {}) or {}
                    t = tb.get("text", "")
                    if t:
                        texts.append(t)
            # 文本来源 2：CHUNK_DELTA 增量（全文主体）
            if event_name == "CHUNK_DELTA":
                t = d.get("text", "") or d.get("delta", "") or ""
                if t:
                    texts.append(t)
        return conv_id, local_id, "".join(texts).strip()

    @staticmethod
    def _is_video_confirm_card(agent_text: str) -> bool:
        """判断 agent 回复是否是「视频生成参数确认」卡（需二次确认）。

        确认卡特征文本（2026-10-19 抓包真值）：
          - 「请先确认以下参数」+「视频生成参数确认」+「确认后我再开始生成视频」
        真实前端此处也没有按钮，用户靠文字回复「确认」推进。
        """
        if not agent_text:
            return False
        return ("确认" in agent_text
                and ("参数" in agent_text or "确认后" in agent_text))

    async def _send_ability_followup(
        self,
        conversation_id: str,
        local_conversation_id: str,
        text: str,
        ability_inner: Optional[Dict[str, Any]] = None,
        timeout: float = 180,
    ) -> str:
        """在已有会话里发一条文本追问（如「确认」），返回 SSE 原文。

        用于视频生成两步流程的第二步：确认卡回复。
        ability_inner 非空时附带 chat_ability（与首条一致，服务端据此
        延续视频任务上下文）。
        """
        now_ms = int(time.time() * 1000)
        payload = self._build_ability_payload(
            text, 17, ability_inner or {}, "生成视频：%s",
        )
        # 覆盖 client_meta：续接会话而非新建
        payload["client_meta"]["local_conversation_id"] = (
            local_conversation_id or f"local_{now_ms}")
        payload["client_meta"]["conversation_id"] = str(conversation_id)
        # 追问消息的文本就是「确认」本身
        payload["messages"][0]["content_block"][0]["content"]["text_block"]["text"] = text
        payload["option"]["need_create_conversation"] = False
        return await self._post_ability(payload, timeout)

    async def _generate_video_ability(
        self,
        prompt: str,
        ratio: Optional[str],
        video_model: str,
        video_duration: int,
        timeout: float,
        poll_timeout: float,
    ) -> "VideoGenerationResult":
        """新协议文生视频：模型/时长/比例精确选择（chat_ability ability_type=17）。

        提交响应是短 ACK（异步渲染），成品随后推送进会话，
        走与 samantha 链路相同的 latest_messagelist 轮询取回。
        """
        # 视频的 ability_param 是扁平结构（抓包真值）
        inner = {
            "ratio": ratio or "auto",
            "model": video_model,
            "duration": video_duration,
            "input_box_content": {
                "user_input_content": prompt,
                "reply_message_format": "生成视频：%s",
            },
        }
        payload = self._build_ability_payload(
            prompt, 17, inner, "生成视频：%s",
            text_suffix=f"，{video_duration}s",
        )
        raw = await self._post_ability(payload, timeout)

        result = VideoGenerationResult(prompt=prompt, model=video_model)
        conv_id, local_id, agent_text = self._parse_ability_ack_full(raw)
        result.conversation_id = conv_id
        result.agent_reply = agent_text

        # 风控
        if "710022004" in raw or "710022002" in raw:
            result.agent_reply = (
                f"{agent_text}\n[风控] 请求被豆包网关拦截（710022004），"
                "请稍后重试或更换网络环境。"
            ).strip()
            return result
        # 付费墙
        if any(k in agent_text for k in ("不可用于创作", "升级正式订阅",
                                         "免费体验套餐", "订阅标准套餐专属能力")):
            result.agent_reply += (
                "（当前账号无该视频模型的创作额度，需更换账号或升级订阅）")
            return result

        # 第二步：确认卡 → 回复「确认」→ 服务端才真正提交渲染任务
        # （2026-10-19 抓包真值：真实前端也无按钮，靠文字回复推进；
        #  确认后响应出现 block_type=10040 thinking_block
        #  「视频生成已提交 / 视频生成中」）
        if conv_id and self._is_video_confirm_card(agent_text):
            try:
                raw2 = await self._send_ability_followup(
                    conv_id, local_id, "确认", ability_inner=inner,
                    timeout=timeout,
                )
                if "710022004" in raw2 or "710022002" in raw2:
                    result.agent_reply += (
                        "\n[风控] 确认请求被豆包网关拦截（710022004），"
                        "请稍后重试。")
                    return result
                _, _, agent_text2 = self._parse_ability_ack_full(raw2)
                if agent_text2:
                    result.agent_reply = agent_text2
                if any(k in agent_text2 for k in (
                        "不可用于创作", "升级正式订阅", "免费体验套餐",
                        "订阅标准套餐专属能力")):
                    result.agent_reply += (
                        "（当前账号无该视频模型的创作额度，"
                        "需更换账号或升级订阅）")
                    return result
            except Exception as e:  # noqa: BLE001
                result.agent_reply += f"\n[确认步骤失败] {e}"
                return result

        if conv_id:
            await self._poll_conversation_video(result, set(), poll_timeout)
        return result

    async def generate_image(
        self,
        prompt: str,
        ratio: Optional[str] = None,
        ref_image_key: Optional[str] = None,
        ref_image_keys: Optional[List[str]] = None,
        timeout: float = 120,
        memory_type: Optional[int] = None,
        save_history: Optional[bool] = None,
        no_watermark: bool = True,
        image_model: str = "",
        style: str = "",
        new_protocol: bool = True,
    ) -> "ImageGenerationResult":
        """Generate images using Doubao's AI image generation.

        双协议（2026-10-19 起）：
          - **文生图**默认走新统一端点 ``/chat/completion`` + ``chat_ability``
            （真实前端同款）：模型/比例/风格精确选择，响应回显生效模型。
          - **图生图**（带 ref key）走旧 ``/samantha/chat/completion``
            agent 通道（content_type=2009 + skill_type=3），无模型选择。

        Args:
            prompt: Text description of the image to generate.
            ratio: Aspect ratio string (e.g. "1:1", "16:9", "9:16", "4:3", "3:4").
                   If None, the server picks a default (usually 1:1).
            ref_image_key: Optional image key (from upload_image) to use as reference (single image).
            ref_image_keys: Optional list of image keys to use as reference (multi-image).
            timeout: Max seconds to wait for generation.
            memory_type: Optional 0 (save to web history) or 2 (ephemeral).
            save_history: If True, sets memory_type=0.
            no_watermark: If True (default), forces memory_type=0 so the server
                sends watermark-free templates (dwm3 / image_raw).
            image_model: 官方 model key（图片项 item_id=45033999706177282 的
                「模型」选择器 selector id=30，option_key 即显示文本，仅新协议生效）：
                - ``Seedream 5.0 Flash``（专业快速，默认推荐）
                - ``Seedream 5.0 Pro``（专业出图・4 倍消耗，需订阅，免费账号不出图）
                - ``Seedream 4.5``（日常生成）
                - ``Seedream 4.0``（基础生图）
                留空 = Seedream 4.0。
            style: 官方风格 key（「风格」选择器 selector id=4 的 option_key），
                如 ``skill_image_styles_film``（电影写真）。按官方 message_text
                规则注入自然语言（「图片风格为电影写真」）。共 32 种，
                完整列表见 ``IMAGE_STYLES`` / ``doubao_models`` 工具。
            new_protocol: 文生图是否走新端点（默认 True；False 走旧 agent 通道）。

        Returns:
            ImageGenerationResult with list of generated images.
        """
        # 收集参考图 key
        keys = []
        if ref_image_keys:
            if isinstance(ref_image_keys, str):
                keys.append(ref_image_keys)
            elif isinstance(ref_image_keys, (list, tuple)):
                keys.extend([k for k in ref_image_keys if k])
        if ref_image_key and ref_image_key not in keys:
            keys.insert(0, ref_image_key)

        # 文生图 + 新协议：模型/比例/风格精确选择
        if new_protocol and not keys:
            try:
                return await self._generate_image_ability(
                    prompt, ratio, image_model, style, timeout
                )
            except DoubaoRateLimitError:
                raise  # 风控不重试，交给上层提示
            except DoubaoChatError:
                # 新端点失败时退回旧 agent 通道（不传模型，旧协议不支持）
                log.warning("new endpoint failed, fallback to samantha agent channel")

        text = prompt
        if style:
            text = f"{prompt}。图片风格为{IMAGE_STYLE_LABELS.get(style, style)}"
        content_dict: Dict[str, Any] = {"text": text}
        if ratio:
            content_dict["ratio"] = ratio
        if image_model:
            # 与视频链路的 video-model 同构：结构化字段 + option_key 原样传递
            content_dict["image-model"] = image_model

        message: Dict[str, Any] = {
            "content": json.dumps(content_dict, ensure_ascii=False),
            "content_type": 2009,  # SamanthaImageInput
            "attachments": [],
            "references": [],
            "skill": {
                "skill_type": 3,
                "skill_type_no_default": 3,
                "skill_id": "3",
                "skill_id_no_default": "3",
            },
        }
        if keys:
            message["attachments"] = [
                {"type": "image", "key": k, "extra": {"refer_types": "overall"}}
                for k in keys
            ]

        if ratio:
            message["content"] = json.dumps(
                {"text": text, "ratio": ratio}, ensure_ascii=False
            )

        eff_mem_type = resolve_memory_type(memory_type, save_history)
        if no_watermark and memory_type is None and save_history is None:
            # 服务端只有在「保存到云端历史」时才下发去水印模板
            # (cgen_dwm3 / image_raw)；阅后即焚(memory_type=2)只会给 lwm3 带水印版。
            eff_mem_type = 0
        sent_event: Dict[str, Any] = {
            "messages": [message],
            "completion_option": {
                "is_regen": False,
                "with_suggest": True,
                "need_create_conversation": True,
                "launch_stage": 1,
                "is_replace": False,
                "is_delete": False,
                "is_ai_playground": False,
                "memory_type": eff_mem_type,
                "message_from": 0,
                "use_deep_think": False,
                "use_auto_cot": False,
                "resend_for_regen": False,
                "enable_commerce_credit": False,
                "action_bar_skill_id": 3,
            },
            "evaluate_option": {"web_ab_params": ""},
            "local_conversation_id": str(uuid.uuid4()),
            "local_message_id": str(uuid.uuid4()),
        }

        url = (
            f"{self.BASE_URL}/samantha/chat/completion?"
            f"{urlencode(self._security_params())}"
        )
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Agw-Js-Conv": "str",
        }

        async with self.session.post(
            url,
            data=json.dumps(sent_event, ensure_ascii=False),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise DoubaoChatError(
                    f"generate_image failed ({resp.status}): {error_text[:500]}"
                )
            raw = (await resp.read()).decode("utf-8", errors="replace")

        self._check_gateway_error(raw)

        result = ImageGenerationResult(prompt=prompt)
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            data_str = ""
            for line in block.strip().split("\n"):
                if line.startswith("data:"):
                    data_str = line[5:].strip()
            if not data_str:
                continue

            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            event_type = data.get("event_type")
            if event_type == 2005:
                detail = data.get("event_data", "")
                raise DoubaoChatError(f"generate_image error: {str(detail)[:500]}")
            if event_type != 2001:
                continue

            event_data_str = data.get("event_data", "")
            try:
                ed = json.loads(event_data_str) if isinstance(event_data_str, str) else event_data_str
            except json.JSONDecodeError:
                continue

            msg = ed.get("message", {})
            if msg.get("content_type") != 2010:
                continue

            content_str = msg.get("content", "")
            try:
                content = json.loads(content_str) if isinstance(content_str, str) else content_str
            except json.JSONDecodeError:
                continue

            for item in content.get("data", []):
                if not isinstance(item, dict):
                    continue
                ori = item.get("image_ori", {}) or {}
                raw = item.get("image_raw", {}) or {}
                thumb = item.get("image_thumb", {}) or {}
                img = GeneratedImage(
                    key=item.get("key", ""),
                    thumb_url=thumb.get("url", ""),
                    ori_url=ori.get("url", ""),
                    raw_url=raw.get("url", ""),
                    ori_raw_url=(item.get("image_ori_raw", {}) or {}).get("url", ""),
                    width=ori.get("width", 0) or thumb.get("width", 0),
                    height=ori.get("height", 0) or thumb.get("height", 0),
                    format=ori.get("format", "") or thumb.get("format", ""),
                )
                result.images.append(img)

        # 全文扫描兜底：生图事件结构随版本变动（2010 / 2074 等），
        # 直接从 SSE 原文里捞无水印模板 URL，最稳。
        wm_urls = _extract_watermark_free_urls(raw)
        if wm_urls:
            if not result.images:
                result.images.append(GeneratedImage())
            first = result.images[0]
            if wm_urls.get("image_raw"):
                first.ori_raw_url = wm_urls["image_raw"]
            if wm_urls.get("dwm3") and not first.ori_url:
                first.ori_url = wm_urls["dwm3"]
            if wm_urls.get("dwm3") and not first.raw_url:
                first.raw_url = wm_urls["dwm3"]

        return result

    async def generate_video(
        self,
        prompt: str,
        ratio: Optional[str] = None,
        camera_movement: Optional[str] = None,
        ref_image_key: Optional[str] = None,
        ref_image_keys: Optional[List[str]] = None,
        timeout: float = 300,
        memory_type: Optional[int] = None,
        save_history: Optional[bool] = None,
        video_model: str = "seedance_v2.0_mini",
        video_duration: int = 10,
        launch_stage: int = 2,
        poll_timeout: float = 900,
        new_protocol: bool = True,
    ) -> "VideoGenerationResult":
        """Generate video using Doubao's AI video generation.

        双协议（2026-10-19 起）：
          - **新统一端点** ``/chat/completion`` + ``chat_ability``
            （ability_type=17，真实前端同款）：模型/时长/比例精确选择，
            服务端按模型额度门控（免费账号请求需订阅档会明确返回不出图）。
            提交后服务端先回「参数确认卡」，自动二次回复「确认」，
            任务才真正进入渲染（响应出现 block_type=10040
            thinking_block「视频生成已提交」）。
          - **旧 agent 通道** ``/samantha/chat/completion``
            （content_type=2020 + skill_type=17）：作为兜底，
            模型选择可能被 agent 忽略（实测会自定档位）。

        Args:
            prompt: Text description of the video to generate.
            ratio: Aspect ratio (e.g. "16:9", "9:16", "1:1").
            camera_movement: Camera movement style (optional).
            ref_image_key: Optional image key (from upload_image) for img2video (single image).
            ref_image_keys: Optional list of image keys for multi-image reference.
            timeout: Max seconds to wait for video generation (default 300).
            memory_type: Optional 0 (save to web history) or 2 (ephemeral).
            save_history: If True, sets memory_type=0.
            video_model: 官方 model key，四档（来自 /alice/slot/action_bar_v3/get_item_conf
                视频项 item_id=45337547893174274 的「模型」选择器 selector id=6）：
                - ``seedance_v2.0_mini``（默认，日常生成，免费额度可跑）
                - ``seedance_v2.0``（2.0 Fast，快速出片，免费额度可跑）
                - ``seedance_v2.0_std``（2.0，进阶画面表现，2x 消耗，🔒需订阅）
                - ``seedance_v2.5``（旗舰 30 秒，5x 消耗，🔒需订阅）
            video_duration: 官方时长档，只支持 5 / 10（默认 10）。
            launch_stage: 旧 agent 通道参数；``2``= 直接进入生成。
            poll_timeout: 取回成品的最长等待秒数（默认 900=15 分钟）。
            new_protocol: 是否走新端点（默认 True；失败自动回退旧通道）。

        Returns:
            VideoGenerationResult with list of generated videos.
        """
        # 新协议：模型/时长/比例精确选择（文生视频；图生视频仍走旧通道）
        keys = []
        if ref_image_keys:
            if isinstance(ref_image_keys, str):
                keys.append(ref_image_keys)
            elif isinstance(ref_image_keys, (list, tuple)):
                keys.extend([k for k in ref_image_keys if k])
        if ref_image_key and ref_image_key not in keys:
            keys.insert(0, ref_image_key)

        if new_protocol and not keys:
            try:
                return await self._generate_video_ability(
                    prompt, ratio, video_model, video_duration,
                    timeout, poll_timeout,
                )
            except (DoubaoRateLimitError, DoubaoChatError) as exc:
                # 新端点传输层失败（风控/网络）→ 回退旧 agent 通道
                log.warning(
                    "video new endpoint failed (%s), fallback to samantha",
                    str(exc)[:120],
                )

        model_label = {
            "seedance_v2.0_mini": "Seedance 2.0 Mini",
            "seedance_v2.0": "Seedance 2.0 Fast",
            "seedance_v2.0_std": "Seedance 2.0",
            "seedance_v2.5": "Seedance 2.5",
        }.get(video_model, video_model)
        ratio_text = f"、比例 {ratio}" if ratio else ""

        # 官方 reply_message_format（见 /alice/slot/action_bar_v3/get_item_conf
        #   → input_box_content.reply_message_format = "帮我生成一个视频：%s"）
        #
        # 🔴 2026-10-19 实测结论：把 selector 值同时写进**自然语言**，
        # 是唯一能让 agent 真正进入生成（而非反复追问参数）的做法。
        # 只传结构化字段时 agent 会说「时长未指定」并反问；
        # 写进文本后它直接回「我先按默认方案生成：10 秒、16:9…本次使用
        # Seedance 2.0 Mini 生成」。结构化字段一并保留，两条路都走。
        content_dict: Dict[str, Any] = {
            "text": (
                f"帮我生成一个视频：{prompt}\n"
                f"参数：{model_label}，时长 {video_duration} 秒{ratio_text}。"
                f"请直接开始生成，不要再向我确认参数。"
            ),
            "video-model": video_model,
            "video-duration": str(video_duration),
        }
        if ratio:
            content_dict["video-ratio"] = ratio
        if camera_movement:
            content_dict["camera_movement"] = camera_movement

        message: Dict[str, Any] = {
            "content": json.dumps(content_dict, ensure_ascii=False),
            "content_type": 2020,  # SamanthaVideoGenerationInput
            "attachments": [],
            "references": [],
            "skill": {
                "skill_type": 17,
                "skill_type_no_default": 17,
                "skill_id": "17",
                "skill_id_no_default": "17",
            },
        }

        for k in keys:
            message["attachments"].append({"type": "image", "key": k})

        # 🔴 2026-10-19 关键修复：视频提交必须 memory_type=0（保存云端）。
        # 阅后即焚(2)的会话不落库 → agent 成品推送无处落地 → 任务孤儿化
        # （线程列表不出现、query_video_gen_info 无记录、永远拿不到 vid）。
        # 图片链路恰好因为去水印需求强制了 0，所以图片能取回而视频不能。
        if memory_type is not None:
            eff_mem_type = int(memory_type)
        elif save_history is not None:
            eff_mem_type = 0 if save_history else 2
        else:
            eff_mem_type = 0
        sent_event: Dict[str, Any] = {
            "messages": [message],
            "completion_option": {
                "is_regen": False,
                "with_suggest": True,
                "need_create_conversation": True,
                "launch_stage": launch_stage,
                "is_replace": False,
                "is_delete": False,
                "is_ai_playground": False,
                "memory_type": eff_mem_type,
                "message_from": 0,
                "use_deep_think": False,
                "use_auto_cot": False,
                "resend_for_regen": False,
                "enable_commerce_credit": False,
                "action_bar_skill_id": 17,
            },
            "evaluate_option": {"web_ab_params": ""},
            "local_conversation_id": str(uuid.uuid4()),
            "local_message_id": str(uuid.uuid4()),
        }

        # 提交前快照会话列表（SSE 拿不到 conv_id 时，靠对比它发现新会话）
        before_ids = await self._snapshot_conversation_ids()

        url = self._plain_url("/samantha/chat/completion")
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Agw-Js-Conv": "str",
        }

        # Phase 1: Submit video generation request
        # launch_stage=2 会让 agent 走完整推理链路再回复，实测 60s 不够，
        # 这里给足 180s（sock_read 由 session 级 timeout 控制）。
        async with self.session.post(
            url,
            data=json.dumps(sent_event, ensure_ascii=False),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=180),
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise DoubaoChatError(
                    f"generate_video failed ({resp.status}): {error_text[:500]}"
                )
            raw = (await resp.read()).decode("utf-8", errors="replace")

        # Extract task_id from fin_reason in CMPL events
        task_id = self._extract_async_task_id(raw)

        if not task_id:
            # No async task — maybe error in initial response or sync result
            # Check for error messages in text content
            text_parts = []
            for block in raw.split("\n\n"):
                if not block.strip():
                    continue
                for line in block.strip().split("\n"):
                    if line.startswith("data:"):
                        try:
                            data = json.loads(line[5:].strip())
                            if data.get("event_type") == 2001:
                                ed = json.loads(data["event_data"])
                                msg = ed.get("message", {})
                                if msg.get("content_type") == 2001:
                                    c = json.loads(msg["content"])
                                    text_parts.append(c.get("text", ""))
                        except (json.JSONDecodeError, KeyError):
                            pass
            full_text = "".join(text_parts)
            if "服务过载" in full_text or "重试" in full_text:
                raise DoubaoChatError(
                    "generate_video: 服务过载，请稍后重试"
                )
            # Try parsing as sync response (content_type=2021)
            result = self._parse_video_sse(raw, prompt)

            # 异步/agent 通道：把会话与 agent 回复带出去，供上层告知用户
            conv_id, agent_text = self._parse_sse_agent_meta(raw)
            result.conversation_id = conv_id
            result.agent_reply = agent_text
            result.prompt = prompt

            # 风控拦截：IP 被 shark_admin 标记时 agent 直接不回
            if "710022004" in raw or "710022002" in raw:
                result.agent_reply = (
                    f"{agent_text}\n[风控] 请求被豆包网关拦截（710022004），"
                    "请稍后重试或更换网络环境。"
                ).strip()
                return result

            # 付费墙：免费体验套餐不可用于创作（账号问题，非协议问题）
            if "不可用于创作" in agent_text or "升级正式订阅" in agent_text:
                result.agent_reply += "（当前账号无视频创作额度，需更换有每日免费额度的账号）"
                return result

            # 参数追问：agent 还在反问、没真正提交任务
            if not conv_id and ("时长" in agent_text or "确认" in agent_text):
                return result

            # 任务已提交 → 轮询会话，等云端渲染完成后取回成品
            await self._poll_conversation_video(result, before_ids, poll_timeout)
            return result

        # Phase 2: Poll /samantha/chat/async/stream for video result
        return await self._poll_async_video(task_id, prompt, timeout)

    # ---- 会话轮询取回（2026-10-19 实测打通） ----

    async def _post_json(
        self, path: str, body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """POST JSON 到豆包 API（免签名）并解析为 dict。"""
        url = self._plain_url(path)
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.doubao.com/chat",
        }
        async with self.session.post(
            url,
            data=json.dumps(body or {}, ensure_ascii=False),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            status = resp.status
            text = await resp.text()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text[:2000], "_http_status": status}

    async def _snapshot_conversation_ids(self) -> set:
        """线程列表里的会话 ID 集合（用于发现提交后新建的会话）。"""
        try:
            d = await self._post_json("/samantha/im/conversation/list", {})
            return {
                str(t["conversation"]["conversation_id"])
                for t in d.get("data", {}).get("thread_list", [])
            }
        except Exception:  # noqa: BLE001
            return set()

    async def _discover_new_conversation(self, before_ids: set) -> str:
        """SSE 没给 conv_id 时，对比线程列表找提交后新建的会话。"""
        for _ in range(12):
            try:
                d = await self._post_json("/samantha/im/conversation/list", {})
                for t in d.get("data", {}).get("thread_list", []):
                    cid = str(t["conversation"]["conversation_id"])
                    if cid not in before_ids:
                        return cid
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(5)
        return ""

    async def _poll_conversation_video(
        self,
        result: "VideoGenerationResult",
        before_ids: set,
        poll_timeout: float,
    ) -> None:
        """轮询会话消息，直到视频卡片（block_type=2074）出现并回填 videos。

        轮询端点 ``POST /alice/conversation/latest_messagelist``，
        body ``{"request_list": [{"conversation_id": <字符串>, "message_index": 0, "count": 20}]}``
        —— conversation_id 必须传**字符串**，传数字返回空。
        """
        conv_id = result.conversation_id
        if not conv_id:
            conv_id = await self._discover_new_conversation(before_ids)
            result.conversation_id = conv_id
        if not conv_id:
            if not result.agent_reply:
                result.agent_reply = "任务已提交，但未能定位会话，无法取回成品。"
            return

        url = self._plain_url("/alice/conversation/latest_messagelist")
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.doubao.com/chat",
        }
        body = json.dumps(
            {
                "request_list": [
                    {
                        "conversation_id": str(conv_id),
                        "message_index": 0,
                        "count": 20,
                    }
                ]
            },
            ensure_ascii=False,
        )
        start = time.time()
        while time.time() - start < poll_timeout:
            try:
                async with self.session.post(
                    url,
                    data=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    text = await resp.text()
                d = json.loads(text)
                msgs: List[Dict[str, Any]] = []
                for group in d.get("data", {}).get("message_map", {}).values():
                    if isinstance(group, list):
                        msgs.extend(group)
                msgs.sort(key=lambda m: m.get("index", 0))
                video = self._extract_video_from_messages(msgs)
                if video is not None:
                    result.videos.append(video)
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(15)
        if not result.agent_reply:
            result.agent_reply = "视频仍在生成中，请在豆包网页端会话查看。"

    @staticmethod
    def _extract_video_from_messages(
        msgs: List[Dict[str, Any]]
    ) -> Optional[GeneratedVideo]:
        """从会话消息里找视频卡片（block_type=2074 → creations → video）。

        优先取 ``video_model.video_list.video_1.main_url``（base64 编码，
        720p 完整文件）；``download_url`` 实测只能拿到截断文件，仅作兜底。
        """
        for m in msgs:
            content = m.get("content", "")
            if not isinstance(content, str) or "2074" not in content:
                continue
            try:
                blocks = json.loads(content)
            except json.JSONDecodeError:
                continue
            if not isinstance(blocks, list):
                continue
            for b in blocks:
                if not isinstance(b, dict) or b.get("block_type") != 2074:
                    continue
                try:
                    card = json.loads(b.get("content", "{}"))
                except json.JSONDecodeError:
                    continue
                for cr in card.get("creations") or []:
                    v = cr.get("video") or {}
                    if not v:
                        continue
                    width = int(v.get("width") or 0)
                    height = int(v.get("height") or 0)
                    url = ""
                    vm_str = v.get("video_model", "")
                    if vm_str:
                        try:
                            vm = (
                                json.loads(vm_str)
                                if isinstance(vm_str, str)
                                else vm_str
                            )
                            for q in ("video_1", "video_2"):
                                vi = vm.get("video_list", {}).get(q) or {}
                                main_b64 = vi.get("main_url", "")
                                if main_b64:
                                    url = base64.b64decode(main_b64).decode(
                                        "utf-8", errors="replace"
                                    )
                                    # 真实尺寸在 vwidth/vheight（top-level 的
                                    # width/height 是封面缩略图尺寸 384x216）
                                    width = int(vi.get("vwidth") or width)
                                    height = int(vi.get("vheight") or height)
                                    break
                        except Exception:  # noqa: BLE001
                            url = ""
                    if not url:
                        url = v.get("download_url", "")
                    cover = v.get("cover") or {}
                    cover_url = (cover.get("image_preview") or {}).get("url", "")
                    if url:
                        return GeneratedVideo(
                            video_url=url,
                            cover_url=cover_url,
                            width=width,
                            height=height,
                            duration=float(v.get("duration") or 0.0),
                            vid=v.get("vid", ""),
                        )
        return None

    @staticmethod
    def _parse_sse_agent_meta(raw: str) -> Tuple[str, str]:
        """从 SSE 响应提取 (conversation_id, agent 回复文本)。

        豆包的 SSE 是**双层转义** JSON：外层 event_data 是字符串，
        里面的字段又带一层 \\" 转义。正则很容易被这层转义挡住
        （``"tts_content":"..."`` 实际是 ``\\"tts_content\\":\\"...``），
        所以这里逐层 json.loads 正规解析，不用正则。
        """
        conv_id = ""
        agent_text = ""
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            for line in block.strip().split("\n"):
                if not line.startswith("data:"):
                    continue
                try:
                    outer = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if outer.get("event_type") != 2001:
                    continue
                ed = outer.get("event_data")
                if isinstance(ed, str):
                    try:
                        ed = json.loads(ed)
                    except json.JSONDecodeError:
                        continue
                if not isinstance(ed, dict):
                    continue
                # 首个 echo 事件的 conversation_id 可能是 "0"（尚未建会话），
                # 只接受真实长数字 ID
                cid = ed.get("conversation_id")
                if cid and str(cid).isdigit() and len(str(cid)) >= 15:
                    conv_id = str(cid)
                # tts_content 是增量分片的，拼接才是完整回复
                tts = ed.get("tts_content")
                if tts:
                    agent_text += tts
        return conv_id, agent_text.strip()

    def _extract_async_task_id(self, raw: str) -> Optional[str]:
        """Extract async task_id from SSE response (fin_reason.async_task.id)."""
        self._check_gateway_error(raw)
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            for line in block.strip().split("\n"):
                if not line.startswith("data:"):
                    continue
                try:
                    data = json.loads(line[5:].strip())
                    if data.get("event_type") != 2001:
                        continue
                    ed = json.loads(data["event_data"])
                    fin_reason = ed.get("fin_reason", {})
                    if not fin_reason:
                        continue
                    if fin_reason.get("reason") == 1:  # FinReasonAsyncTask
                        async_task = fin_reason.get("async_task", {})
                        tid = async_task.get("id", "")
                        if tid:
                            return tid
                except (json.JSONDecodeError, KeyError):
                    continue
        return None

    async def _poll_async_video(
        self, task_id: str, prompt: str, timeout: float
    ) -> "VideoGenerationResult":
        """Poll /samantha/chat/async/stream SSE for video generation result."""
        # Build URL without fp param (async/stream doesn't use it)
        params = self._security_params()
        params.pop("fp", None)
        url = (
            f"{self.BASE_URL}/samantha/chat/async/stream?"
            f"{urlencode(params)}"
        )
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Agw-Js-Conv": "str",
        }
        body = json.dumps({"task_id": task_id, "event_id": 0})

        async with self.session.post(
            url,
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise DoubaoChatError(
                    f"generate_video async poll failed ({resp.status}): "
                    f"{error_text[:500]}"
                )
            raw = (await resp.read()).decode("utf-8", errors="replace")

        return self._parse_video_sse(raw, prompt)

    def _parse_video_sse(
        self, raw: str, prompt: str
    ) -> "VideoGenerationResult":
        """Parse SSE response for video generation (content_type=2021)."""
        self._check_gateway_error(raw)
        result = VideoGenerationResult(prompt=prompt)
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            data_str = ""
            for line in block.strip().split("\n"):
                if line.startswith("data:"):
                    data_str = line[5:].strip()
            if not data_str:
                continue
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            event_type = data.get("event_type")
            if event_type == 2005:
                detail = data.get("event_data", "")
                raise DoubaoChatError(
                    f"generate_video error: {str(detail)[:500]}"
                )
            if event_type != 2001:
                continue

            event_data_str = data.get("event_data", "")
            try:
                ed = (
                    json.loads(event_data_str)
                    if isinstance(event_data_str, str)
                    else event_data_str
                )
            except json.JSONDecodeError:
                continue

            msg = ed.get("message", {})
            if msg.get("content_type") != 2021:
                continue

            content_str = msg.get("content", "")
            try:
                content = (
                    json.loads(content_str)
                    if isinstance(content_str, str)
                    else content_str
                )
            except json.JSONDecodeError:
                continue

            # Video response: extract video_model similar to music
            for item in content.get("data", [content]):
                if not isinstance(item, dict):
                    continue
                # Try direct video_url field
                video_url = (
                    item.get("video_url", "")
                    or item.get("url", "")
                )
                # Or decode from video_model (base64 like music)
                if not video_url:
                    vm_str = item.get("video_model", "")
                    if vm_str:
                        try:
                            vm = (
                                json.loads(vm_str)
                                if isinstance(vm_str, str)
                                else vm_str
                            )
                            vlist = vm.get("video_list", {})
                            for _q, vinfo in vlist.items():
                                main_b64 = vinfo.get("main_url", "")
                                if main_b64:
                                    video_url = base64.b64decode(
                                        main_b64
                                    ).decode("utf-8", errors="replace")
                                    break
                        except (json.JSONDecodeError, Exception):
                            pass

                cover_url = (
                    item.get("cover_url", "")
                    or item.get("cover", {}).get("url", "")
                )
                video = GeneratedVideo(
                    video_url=video_url,
                    cover_url=cover_url,
                    width=item.get("width", 0),
                    height=item.get("height", 0),
                    duration=item.get("duration", 0.0),
                )
                if video.video_url:
                    result.videos.append(video)

        return result

    async def generate_music(
        self,
        prompt: str,
        lyric: Optional[str] = None,
        genre: Optional[str] = None,
        mood: Optional[str] = None,
        gender: Optional[str] = None,
        theme: Optional[str] = None,
        generation_type: Optional[str] = None,
        timeout: float = 300,
        memory_type: Optional[int] = None,
        save_history: Optional[bool] = None,
    ) -> "MusicGenerationResult":
        """Generate music using Doubao's AI music generation.

        Uses /samantha/chat/completion with content_type=2005
        (SamanthaMusicGenInput) and skill_type=9 (SkillMusicGen).

        Args:
            prompt: Text description of the music to generate.
            lyric: Explicit lyrics for the song (optional).
            genre: Music genre, e.g. "pop", "rock", "jazz" (optional).
            mood: Mood/emotion, e.g. "happy", "sad" (optional).
            gender: Vocalist gender preference (optional).
            theme: Theme of the song (optional).
            generation_type: Generation mode (optional).
            timeout: Timeout in seconds.
            memory_type: Optional 0 (save to web history) or 2 (ephemeral).
            save_history: If True, sets memory_type=0.

        Returns:
            MusicGenerationResult with list of generated tracks.
        """
        content_dict: Dict[str, Any] = {"text": prompt}
        if lyric:
            content_dict["lyric"] = lyric
        if genre:
            content_dict["genre"] = genre
        if mood:
            content_dict["mood"] = mood
        if gender:
            content_dict["gender"] = gender
        if theme:
            content_dict["theme"] = theme
        if generation_type:
            content_dict["generation_type"] = generation_type

        message: Dict[str, Any] = {
            "content": json.dumps(content_dict, ensure_ascii=False),
            "content_type": 2005,  # SamanthaMusicGenInput
            "attachments": [],
            "references": [],
            "skill": {
                "skill_type": 9,
                "skill_type_no_default": 9,
                "skill_id": "9",
                "skill_id_no_default": "9",
            },
        }

        eff_mem_type = resolve_memory_type(memory_type, save_history)
        sent_event: Dict[str, Any] = {
            "messages": [message],
            "completion_option": {
                "is_regen": False,
                "with_suggest": True,
                "need_create_conversation": True,
                "launch_stage": 1,
                "is_replace": False,
                "is_delete": False,
                "is_ai_playground": False,
                "memory_type": eff_mem_type,
                "message_from": 0,
                "use_deep_think": False,
                "use_auto_cot": False,
                "resend_for_regen": False,
                "enable_commerce_credit": False,
                "action_bar_skill_id": 9,
            },
            "evaluate_option": {"web_ab_params": ""},
            "local_conversation_id": str(uuid.uuid4()),
            "local_message_id": str(uuid.uuid4()),
        }

        url = (
            f"{self.BASE_URL}/samantha/chat/completion?"
            f"{urlencode(self._security_params())}"
        )
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Agw-Js-Conv": "str",
        }

        async with self.session.post(
            url,
            data=json.dumps(sent_event, ensure_ascii=False),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise DoubaoChatError(
                    f"generate_music failed ({resp.status}): {error_text[:500]}"
                )
            raw = (await resp.read()).decode("utf-8", errors="replace")

        return self._parse_music_response(raw, prompt)

    def _parse_music_response(
        self, raw: str, prompt: str
    ) -> "MusicGenerationResult":
        """Parse SSE response for music generation.

        Music response uses content_type=2006 with tasks dict structure.
        Audio URL is base64-encoded inside video_model.video_list.*.main_url.
        """
        self._check_gateway_error(raw)
        result = MusicGenerationResult(prompt=prompt)
        # We want the LAST content_type=2006 event with is_finish=True
        final_content = None
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            data_str = ""
            for line in block.strip().split("\n"):
                if line.startswith("data:"):
                    data_str = line[5:].strip()
            if not data_str:
                continue
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            event_type = data.get("event_type")
            if event_type == 2005:
                detail = data.get("event_data", "")
                raise DoubaoChatError(
                    f"generate_music error: {str(detail)[:500]}"
                )
            if event_type != 2001:
                continue

            event_data_str = data.get("event_data", "")
            try:
                ed = (
                    json.loads(event_data_str)
                    if isinstance(event_data_str, str)
                    else event_data_str
                )
            except json.JSONDecodeError:
                continue

            msg = ed.get("message", {})
            if msg.get("content_type") not in (2006, 2004):
                continue

            content_str = msg.get("content", "")
            try:
                content = (
                    json.loads(content_str)
                    if isinstance(content_str, str)
                    else content_str
                )
            except json.JSONDecodeError:
                continue

            # Keep updating — we want the final (most complete) version
            if ed.get("is_finish") or not final_content:
                final_content = content

        if not final_content:
            return result

        # Parse tasks dict: {"0": {...}, "1": {...}}
        tasks = final_content.get("tasks", {})
        if isinstance(tasks, dict):
            tasks_list = list(tasks.values())
        elif isinstance(tasks, list):
            tasks_list = tasks
        else:
            tasks_list = []

        for task in tasks_list:
            if not isinstance(task, dict):
                continue
            if task.get("music_gen_failed"):
                msg = task.get("music_gen_failed_msg", "unknown error")
                raise DoubaoChatError(f"generate_music failed: {msg}")

            # Extract audio URL from video_model
            audio_url = ""
            duration = 0.0
            video_model_str = task.get("video_model", "")
            if video_model_str:
                try:
                    vm = (
                        json.loads(video_model_str)
                        if isinstance(video_model_str, str)
                        else video_model_str
                    )
                    duration = vm.get("video_duration", 0.0)
                    vlist = vm.get("video_list", {})
                    for _quality, vinfo in vlist.items():
                        main_url_b64 = vinfo.get("main_url", "")
                        if main_url_b64:
                            audio_url = base64.b64decode(
                                main_url_b64
                            ).decode("utf-8", errors="replace")
                            break
                except (json.JSONDecodeError, Exception):
                    pass

            # Extract cover URL
            cover_url = ""
            cover = task.get("cover", {})
            if isinstance(cover, dict):
                cover_ori = cover.get("image_ori", {}) or {}
                cover_url = cover_ori.get("url", "")

            track = GeneratedMusic(
                audio_url=audio_url,
                title=task.get("title", ""),
                duration=duration,
                lyrics=task.get("lyric", ""),
                cover_url=cover_url,
                vid=task.get("vid", ""),
            )
            if track.audio_url or track.title:
                result.tracks.append(track)

        return result

    # ------------------------------------------------------------------
    # File upload (TOS / ImageX flow)
    # ------------------------------------------------------------------

    async def upload_file(
        self,
        file_data: bytes,
        filename: str,
    ) -> "UploadedFile":
        """Upload a file to Doubao's storage (ByteDance TOS via ImageX proxy).

        Supported formats: PDF, TXT, DOCX, XLSX, PPTX, CSV, MD, code files,
        images, and more (see UPLOAD_SUPPORTED_EXTENSIONS).

        Flow:
          1. POST /alice/resource/prepare_upload -> STS credentials + service_id
          2. GET  /top/v1?Action=ApplyImageUpload -> upload address + auth token
          3. POST https://{tos_host}/upload/v1/{store_uri} -> upload binary
          4. POST /top/v1?Action=CommitImageUpload -> confirm upload

        Args:
            file_data: Raw file bytes.
            filename: Original filename (used for extension detection).

        Returns:
            UploadedFile with uri, name, size, file_type.
            Pass this to completion/chat methods via file_attachments param.
        """
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext and ext not in UPLOAD_SUPPORTED_EXTENSIONS:
            log.warning("File extension '%s' may not be supported", ext)

        file_size = len(file_data)
        crc32 = format(zlib.crc32(file_data) & 0xFFFFFFFF, "08x")
        params = self._security_params()

        # Step 1: Prepare upload — get STS credentials
        prepare_url = f"{self.BASE_URL}/alice/resource/prepare_upload?{urlencode(params)}"
        async with self.session.post(
            prepare_url,
            json={"tenant_id": "5", "scene_id": "5", "resource_type": 1},
        ) as resp:
            body = await resp.json()
            if body.get("code") != 0:
                raise DoubaoChatError(
                    f"prepare_upload failed: code={body.get('code')} msg={body.get('msg')}"
                )
            data = body["data"]
            service_id = data["service_id"]
            auth_token = data["upload_auth_token"]
            ak = auth_token["access_key"]
            sk = auth_token["secret_key"]
            st = auth_token["session_token"]

        # Step 2: ApplyImageUpload via /top/v1 proxy
        file_ext = f".{ext}" if ext else ""
        apply_url = (
            f"{self.BASE_URL}/top/v1?"
            f"Action=ApplyImageUpload&Version=2018-08-01"
            f"&ServiceId={service_id}&NeedFallback=true"
            f"&FileSize={file_size}&FileExtension={file_ext}"
            f"&s=jdnfglwfkl"
        )
        sign_headers = _aws_sign_v4("GET", apply_url, "", ak, sk, st)
        async with self.session.get(apply_url, headers=sign_headers) as resp:
            body = await resp.json()
            result_data = body.get("Result")
            if not result_data:
                err = body.get("ResponseMetadata", {}).get("Error", {})
                raise DoubaoChatError(
                    f"ApplyImageUpload failed: {err.get('Code')} {err.get('Message')}"
                )
            upload_addr = result_data["UploadAddress"]
            store_info = upload_addr["StoreInfos"][0]
            store_uri = store_info["StoreUri"]
            tos_auth = store_info["Auth"]
            session_key = upload_addr["SessionKey"]
            upload_hosts = upload_addr.get("UploadHosts", [])

        # Step 3: Upload binary to TOS
        tos_host = upload_hosts[0] if upload_hosts else "tos-mya2lf.vodupload.com"
        upload_url = f"https://{tos_host}/upload/v1/{store_uri}"
        upload_headers = {
            "Authorization": tos_auth,
            "Content-CRC32": crc32,
        }
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
        ) as tos_session:
            async with tos_session.post(
                upload_url, data=file_data, headers=upload_headers
            ) as resp:
                tos_resp = await resp.json()
                if tos_resp.get("code") != 2000:
                    raise DoubaoChatError(
                        f"TOS upload failed: {tos_resp.get('message', tos_resp)}"
                    )

        # Step 4: CommitImageUpload
        commit_url = (
            f"{self.BASE_URL}/top/v1?"
            f"Action=CommitImageUpload&Version=2018-08-01"
            f"&ServiceId={service_id}"
        )
        commit_body = json.dumps({"SessionKey": session_key})
        sign_headers2 = _aws_sign_v4("POST", commit_url, commit_body, ak, sk, st)
        sign_headers2["Content-Type"] = "application/json"
        async with self.session.post(
            commit_url, data=commit_body, headers=sign_headers2
        ) as resp:
            body = await resp.json()
            results = body.get("Result", {}).get("Results", [])
            if not results or results[0].get("UriStatus") != 2000:
                raise DoubaoChatError(
                    f"CommitImageUpload failed: {body}"
                )

        log.info("File uploaded: %s -> %s", filename, store_uri)
        return UploadedFile(
            uri=store_uri,
            name=filename,
            size=file_size,
            file_type=ext,
        )

    async def upload_image(
        self,
        image_bytes: bytes,
        filename: str = "image.png",
    ) -> Dict[str, str]:
        """Upload an image and return attachment metadata.

        Two-step flow:
          1. POST /samantha/pages/upload_image -> {uri, url}
          2. POST /alice/message/get_file_url  -> {uri, main_url (CDN)}

        Returns dict with keys: uri, cdn_url, name, format, width, height.
        """
        params = self._security_params()
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "png"

        upload_url = (
            f"{self.BASE_URL}/samantha/pages/upload_image?"
            f"{urlencode(params)}"
        )
        form = aiohttp.FormData()
        form.add_field(
            "data", image_bytes,
            filename=filename,
            content_type=f"image/{ext}",
        )
        form.add_field("file_type", ext)

        # 独立 session: 避免 self.session 默认 Content-Type: application/json 覆盖 multipart
        upload_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/135.0.0.0 Safari/537.36"
            ),
            "Origin": "https://www.doubao.com",
            "Referer": "https://www.doubao.com/chat",
            "x-tt-passport-csrf-token": (
                self.cookies.get("passport_csrf_token")
                or self.cookies.get("passport_csrf_token_default")
                or ""
            ),
        }
        async with aiohttp.ClientSession(
            cookies=self.cookies,
            headers=upload_headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as upload_session:
            async with upload_session.post(upload_url, data=form) as resp:
                if resp.status != 200:
                    raise DoubaoChatError(
                        f"Image upload failed ({resp.status}): "
                        f"{(await resp.text())[:500]}"
                    )
                body = await resp.json()
                if body.get("code") != 0:
                    raise DoubaoChatError(
                        f"Image upload error: {body.get('msg', body)}"
                    )
                upload_data = body["data"]
                uri_short = upload_data["uri"]

        file_url_endpoint = (
            f"{self.BASE_URL}/alice/message/get_file_url?"
            f"{urlencode(params)}"
        )
        async with self.session.post(
            file_url_endpoint,
            json={
                "uris": [uri_short],
                "type": "image",
                "format": ext,
                "expire_second": 3600,
            },
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status != 200:
                raise DoubaoChatError(
                    f"get_file_url failed ({resp.status}): "
                    f"{(await resp.text())[:500]}"
                )
            body = await resp.json()
            if body.get("code") != 0:
                raise DoubaoChatError(
                    f"get_file_url error: {body.get('msg', body)}"
                )
            data = body.get("data", {})
            file_urls = data.get("file_urls", []) if isinstance(data, dict) else []
            if not file_urls:
                raise DoubaoChatError("get_file_url returned no file_urls")
        img_w = 1024
        img_h = 1024
        try:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(image_bytes)) as pil_img:
                img_w, img_h = pil_img.size
                if pil_img.format:
                    ext = pil_img.format.lower()
        except Exception:
            pass

        file_info = file_urls[0]
        return {
            "uri": file_info["uri"],
            "cdn_url": file_info.get("main_url") or file_info.get("url", ""),
            "name": filename,
            "format": ext,
            "width": int(file_info.get("width") or img_w),
            "height": int(file_info.get("height") or img_h),
        }

    async def get_file_download_url(
        self,
        uri: str,
        expire_seconds: int = 3600,
    ) -> str:
        """Get a temporary CDN download URL for a previously uploaded file.

        Args:
            uri: TOS URI from upload_file() or upload_image().
            expire_seconds: URL validity period (default 1 hour, max unknown).

        Returns:
            Public CDN URL string for downloading the file.
        """
        params = self._security_params()
        ext = uri.rsplit(".", 1)[-1] if "." in uri else ""
        file_url_endpoint = (
            f"{self.BASE_URL}/alice/message/get_file_url?"
            f"{urlencode(params)}"
        )
        async with self.session.post(
            file_url_endpoint,
            json={
                "uris": [uri],
                "type": "file",
                "format": ext,
                "expire_second": expire_seconds,
            },
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status != 200:
                raise DoubaoChatError(
                    f"get_file_url failed ({resp.status}): "
                    f"{(await resp.text())[:500]}"
                )
            body = await resp.json()
            if body.get("code") != 0:
                raise DoubaoChatError(
                    f"get_file_url error: {body.get('msg', body)}"
                )
            data = body.get("data", {})
            file_urls = data.get("file_urls", []) if isinstance(data, dict) else []
            if not file_urls:
                raise DoubaoChatError("get_file_url returned no file_urls")
            return file_urls[0].get("main_url", "")

    @classmethod
    def from_session(
        cls,
        session_file: str = ".doubao_session.json",
        bot_id: str = DEFAULT_BOT_ID,
        timeout_seconds: int = 120,
        captcha_handler: Union[None, str, "CaptchaHandler"] = "auto",
        max_captcha_retries: int = 3,
    ) -> "DoubaoChatClient":
        """
        Create a client from a saved ``.doubao_session.json`` file.

        The file is written by the GUI (QR login or Chromium load) and
        contains cookies, device params, and the ``fp_verified`` flag.

        Args:
            session_file: Path to the session JSON file.
            captcha_handler: See ``__init__`` for details.
        """
        # vendored: session 文件就是 JSON，直接读（原为 from .session import load_session）
        session = json.loads(Path(session_file).read_text(encoding="utf-8"))
        cookies = session["cookies"]
        params = session["params"]

        if not cookies.get("sessionid"):
            raise DoubaoChatError(
                f"No sessionid in {session_file}. "
                "Use QR login or provide cookies via /v1/session/update."
            )

        # msToken is optional — API works without it, and fake values
        # trigger rate limiting (710022002). Empty string is safest.
        ms_token = cookies.get("msToken", "")
        device_id = params.get("device_id") or "714003710229497"
        web_id = params.get("web_id") or "7604137868021548590"
        fp = params.get("fp") or "verify_mlcfw5f7_TPq0YmFD_NrsC_4RuQ_BJPg_M5W7i58I7wV0"

        return cls(
            cookies=cookies,
            ms_token=ms_token,
            device_id=device_id,
            web_id=web_id,
            fp=fp,
            bot_id=bot_id,
            timeout_seconds=timeout_seconds,
            captcha_handler=captcha_handler,
            max_captcha_retries=max_captcha_retries,
        )
