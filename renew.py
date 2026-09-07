#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Falix Renew Pro v2

当前 Falix Timer 自动检查/续期程序。

功能：
- CloakBrowser (stealth Chromium, 绕过 Cloudflare managed challenge 指纹检测)
- Falix /timer?id=SERVER_ID
- 自动读取 Timer
- 低于阈值才尝试 Add Time
- 多服务器
- storage_state 登录
- CAPTCHA / Turnstile / Managed Challenge 自动通过 (坐标点击 iframe + 等待)
- 代理支持 (socks5h:// + cloakbrowser)
- Telegram 通知
- Screenshot + HTML 调试
- GitHub Actions 兼容

环境变量：

FALIX_SERVER_IDS
    服务器 ID，多个用逗号分隔

FALIX_STORAGE_STATE
    storage_state JSON 文件

RENEW_BELOW_MINUTES
    Timer 小于多少分钟时才尝试续期
    默认 20

HEADLESS
    true / false

PROXY_URI
    代理节点链接 (hysteria2/hy2/tuic/vless/vmess), 由 workflow 启动 sing-box
    转成本地 socks5://127.0.0.1:1080. 不配则直连.

TG_BOT_TOKEN
TG_CHAT_ID
"""

import asyncio
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# CloakBrowser 是 stealth Chromium, 绕过 CF 指纹检测; 接口与 Playwright Browser 兼容
# 优先用 cloakbrowser, 不可用时降级到 playwright (开发环境/调试)
# 重要: cloakbrowser.launch() 是 SYNC API, 不能在 asyncio 里 await (会抛
# "It looks like you are using Playwright Sync API inside the asyncio loop").
# 必须用 launch_context_async() (async 版本), 返回 BrowserContext (async API).
try:
    from cloakbrowser import launch_context_async as cloakbrowser_launch_async
    HAS_CLOAKBROWSER = True
except ImportError:
    HAS_CLOAKBROWSER = False
    from playwright.async_api import async_playwright

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# Config
# ============================================================

BASE_URL = "https://client.falixnodes.net"

TIMER_URL = (
    BASE_URL +
    "/timer?id={}"
)

DEBUG_DIR = Path("debug_output")

DEBUG_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

SERVER_IDS = [
    x.strip()
    for x in os.getenv(
        "FALIX_SERVER_IDS",
        ""
    ).split(",")
    if x.strip()
]

STORAGE_STATE = os.getenv(
    "FALIX_STORAGE_STATE",
    "falix_state.json",
).strip()

RENEW_BELOW_MINUTES = int(
    os.getenv(
        "RENEW_BELOW_MINUTES",
        "20",
    )
)

HEADLESS = (
    os.getenv(
        "HEADLESS",
        "true",
    ).lower()
    == "true"
)

# 代理: workflow 用 sing-box 启动 socks5 监听 127.0.0.1:1080, 通过 PROXY_URI 协议
# 脚本只看 ALL_PROXY/HTTPS_PROXY env (workflow 设置 socks5h://127.0.0.1:1080)
PROXY_URI = os.getenv("PROXY_URI", "").strip()
USE_PROXY = bool(
    os.getenv("ALL_PROXY")
    or os.getenv("HTTPS_PROXY")
    or os.getenv("HTTP_PROXY")
    or PROXY_URI
)

TG_BOT_TOKEN = os.getenv(
    "TG_BOT_TOKEN",
    "",
).strip()

TG_CHAT_ID = os.getenv(
    "TG_CHAT_ID",
    "",
).strip()



# ============================================================
# Logging
# ============================================================

def now():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def log(message):
    print(
        f"[{now()}] {message}",
        flush=True,
    )


def safe_name(server_id):
    return re.sub(
        r"[^a-zA-Z0-9_-]",
        "_",
        server_id,
    )


# ============================================================
# Debug
# ============================================================

async def save_debug(
    page,
    server_id,
    name,
):
    prefix = (
        DEBUG_DIR /
        f"{safe_name(server_id)}_{name}"
    )

    try:
        await page.screenshot(
            path=str(
                prefix.with_suffix(".png")
            ),
            full_page=True,
        )

        log(
            f"📸 Screenshot: "
            f"{prefix}.png"
        )

    except Exception as e:
        log(
            f"⚠️ Screenshot 失败: {e}"
        )

    try:
        html = await page.content()

        prefix.with_suffix(
            ".html"
        ).write_text(
            html,
            encoding="utf-8",
        )

        log(
            f"📄 HTML: "
            f"{prefix}.html"
        )

    except Exception as e:
        log(
            f"⚠️ HTML 保存失败: {e}"
        )


# ============================================================
# Telegram
# ============================================================

def send_tg(message):
    if not TG_BOT_TOKEN:
        return

    if not TG_CHAT_ID:
        return

    url = (
        "https://api.telegram.org/bot"
        f"{TG_BOT_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TG_CHAT_ID,
                "text": message,
                "disable_web_page_preview": "true",
            },
            timeout=15,
        )

        if not response.ok:
            log(
                "⚠️ Telegram 返回异常: "
                f"{response.status_code}"
            )

    except Exception as e:
        log(
            f"⚠️ Telegram 发送失败: {e}"
        )


# ============================================================
# Time parser
# ============================================================

def parse_time(text):
    if not text:
        return None

    text = text.strip().lower()

    # HH:MM:SS
    match = re.search(
        r"\b(\d{1,3}):(\d{2}):(\d{2})\b",
        text,
    )

    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = int(match.group(3))

        return (
            hours * 3600
            + minutes * 60
            + seconds
        )

    # MM:SS
    match = re.search(
        r"\b(\d{1,3}):(\d{2})\b",
        text,
    )

    if match:
        minutes = int(match.group(1))
        seconds = int(match.group(2))

        return (
            minutes * 60
            + seconds
        )

    # 1h 20m 30s (英文) 或 1小时 20分钟 30秒 (中文) 或 1小时20分30秒 (中文无空格)
    # 中文: 时/小时, 分/分钟, 秒
    hours = re.search(
        r"(\d+)\s*(?:h|小时|时)\b",
        text,
    )

    minutes = re.search(
        r"(\d+)\s*(?:m|分钟|分)\b",
        text,
    )

    seconds = re.search(
        r"(\d+)\s*(?:s|秒)\b",
        text,
    )

    if hours or minutes or seconds:
        return (
            (
                int(hours.group(1))
                if hours
                else 0
            )
            * 3600
            +
            (
                int(minutes.group(1))
                if minutes
                else 0
            )
            * 60
            +
            (
                int(seconds.group(1))
                if seconds
                else 0
            )
        )

    return None


# ============================================================
# Page text
# ============================================================

async def get_body_text(page):
    try:
        return await page.locator(
            "body"
        ).inner_text(
            timeout=5000
        )
    except Exception:
        return ""


async def wait_page_ready(page, timeout=20):
    """等待 Timer 页面真正就绪, 而不是固定睡 3 秒.

    Falix 页面加载后可能先显示 "Loading timer...",
    固定等待容易把尚未渲染完的页面误判为 "没有 Add Time".
    """
    ready_patterns = [
        r"\d{1,3}:\d{2}:\d{2}",      # HH:MM:SS
        r"\d{1,3}:\d{2}",            # MM:SS
        r"add\s*time",               # 英文按钮
        r"添加\s*时间",               # 中文按钮
        r"just a moment",            # CF managed challenge
        r"performing security",
        r"server is offline",        # 无 Timer 状态
        r"no active timer",
        r"start the server first",
        r"timer has expired",        # Timer 已过期
        r"loading timer",
        r"log in|sign in",           # 登录页
    ]
    end = time.time() + timeout

    while time.time() < end:
        text = (await get_body_text(page)).lower()

        if any(
            re.search(p, text)
            for p in ready_patterns
        ):
            return True

        await page.wait_for_timeout(
            1000
        )

    return False


async def classify_page_state(page):
    """识别 Timer 页面当前处于什么状态, 用于故障自诊断."""
    text = (await get_body_text(page)).lower()

    def has(*words):
        return all(w in text for w in words)

    if any(p in text for p in [
        "just a moment",
        "performing security verification",
        "checking your browser",
        "enable javascript and cookies",
    ]):
        return "CF_CHALLENGE"

    if (
        has("server is offline")
        or has("no active timer")
        or has("start the server first")
    ):
        return "OFFLINE_NO_TIMER"

    if (
        has("timer has expired")
        or has("has been stopped")
    ):
        return "EXPIRED"

    if has("loading timer"):
        return "LOADING"

    if (
        "add time" in text
        or "添加时间" in text
    ):
        return "HAS_ADD_TIME"

    return "UNKNOWN"


async def get_remaining_seconds(page):
    text = await get_body_text(page)

    if not text:
        return None

    clean = re.sub(
        r"\s+",
        " ",
        text,
    )

    log(
        "📄 页面文本: "
        + clean[:600]
    )

    # HH:MM:SS
    values = re.findall(
        r"\b\d{1,3}:\d{2}:\d{2}\b",
        text,
    )

    for value in values:
        seconds = parse_time(value)

        if (
            seconds is not None
            and seconds <= 7 * 24 * 3600
        ):
            return seconds

    # MM:SS
    values = re.findall(
        r"\b\d{1,3}:\d{2}\b",
        text,
    )

    for value in values:
        seconds = parse_time(value)

        if (
            seconds is not None
            and seconds <= 24 * 3600
        ):
            return seconds

    # 中文格式兜底: "N 小时 M 分钟 K 秒" 或 "N小时M分K秒" 或 "N天M小时"
    # parse_time 已经支持中文时/分/秒, 这里直接调用整个 text
    seconds = parse_time(text)
    if seconds is not None and seconds <= 7 * 24 * 3600:
        return seconds

    return None


def format_seconds(seconds):
    if seconds is None:
        return "UNKNOWN"

    hours = seconds // 3600

    minutes = (
        seconds % 3600
    ) // 60

    secs = seconds % 60

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{secs:02d}"
    )


# ============================================================
# CAPTCHA detection
# ============================================================

async def detect_captcha(page):
    """检测页面是否还在 CAPTCHA/Challenge 状态.

    返回 True 表示仍有 CAPTCHA 需要处理, False 表示已通过或没有 CAPTCHA.

    关键: 如果 cf-turnstile-response hidden input 已有 value, 说明 CF 已发放
    response token, Turnstile 已通过 - 即便 cf-turnstile div 和 iframe 仍在 DOM 里
    (它们不会消失, 只是 widget 显示成绿色对勾).
    """
    # 1. 优先检查 cf-turnstile-response 是否已有 value (Turnstile 通过的硬标志)
    try:
        has_response = await page.evaluate("""
        (() => {
            const inputs = document.querySelectorAll(
                'input[name="cf-turnstile-response"], input[id*="cf-chl-widget"][name="cf-turnstile-response"]'
            );
            for (const inp of inputs) {
                if (inp.value && inp.value.length > 10) {
                    return true;
                }
            }
            return false;
        })();
""")
        if has_response:
            return False  # Turnstile 已通过, 不算 CAPTCHA 待处理
    except Exception:
        pass

    # 2. 文本关键字检测 (会话挑战页 / "Just a moment" 等仍含这些词)
    patterns = [
        "captcha",
        "turnstile",
        "verify you are human",
        "verification",
        "cloudflare",
        "challenge",
    ]

    text = (
        await get_body_text(page)
    ).lower()

    # 3. "Just a moment..." / "Performing security verification" 是 CF Managed
    # Challenge 全屏页的特征, 这种情况 widget 不会有 response (CF 边缘层挑战,
    # 通过后会自动 redirect 到目标页, 不会留下 turnstile-response token)
    managed_challenge_text = any(
        phrase in text for phrase in [
            "just a moment",
            "performing security verification",
            "this website uses a security service",
        ]
    )

    for pattern in patterns:
        if pattern in text:
            # 命中关键字, 但除非是 managed challenge 全屏页, 否则可能是
            # Falix dashboard 自带 Turnstile widget (已经有 response 就不算)
            if managed_challenge_text:
                return True
            # 否则继续看 selector 判断
            break

    # 4. Selector 检测 (iframe / class)
    #    注意: CF Turnstile 的 iframe src 经常不带 "turnstile" 关键字
    #    (新版在 challenges.cloudflare.com / captcha-delivery.com 下),
    #    必须把常见模式都列上, 否则 challenge 页会被当成正常页漏检
    selectors = [
        'iframe[src*="turnstile"]',
        'iframe[src*="captcha"]',
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[src*="captcha-delivery.com"]',
        '[class*="captcha"]',
        '[id*="captcha"]',
    ]

    for selector in selectors:
        try:
            if await page.locator(
                selector
            ).count():
                # selector 命中, 但如果是 Turnstile widget 且其 hidden input
                # 已有 value, 则不算 (上面已检测并返回 False, 走不到这里)
                # 走到这里说明 Turnstile widget 还没通过
                return True
        except Exception:
            pass

    return False


# ============================================================
# CAPTCHA / Turnstile 自动通过 (移植自 zampto solve_turnstile)
# ============================================================

async def find_turnstile_iframe_box(page):
    """找到 Turnstile / CF Challenge iframe 的 bounding box.

    多策略查找 (按可靠性排序):
      A. 遍历 page.frames 找到 CF Turnstile frame, 用 frame_element() 拿 DOM 元素
      B. query_selector 多种 src / title 选择器
      C. 用 page.evaluate 在页面内遍历所有 iframe, getBoundingClientRect
    """
    # 策略 A: 从 page.frames 反查 iframe DOM 元素 (最可靠)
    try:
        for fr in page.frames:
            furl = (fr.url or "").lower()
            if ("challenges.cloudflare.com" in furl
                    or "turnstile" in furl
                    or "captcha" in furl):
                try:
                    iframe_el = await fr.frame_element()
                    if iframe_el:
                        box = await iframe_el.bounding_box()
                        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                            return iframe_el, box
                except Exception:
                    pass
    except Exception:
        pass

    # 策略 B: query_selector 多选择器
    for sel in [
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[src*="turnstile"]',
        'iframe[src*="captcha"]',
        'iframe[src*="cloudflare"]',
        'div.cf-turnstile iframe',
        'div[data-sitekey] iframe',
        'iframe[title*="Widget"]',
        'iframe[title*="Cloudflare"]',
        'iframe[title*="captcha"]',
    ]:
        try:
            iframe_el = await page.query_selector(sel)
            if iframe_el:
                box = await iframe_el.bounding_box()
                if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                    return iframe_el, box
        except Exception:
            pass

    # 策略 C: 在页面内用 JS 遍历所有 iframe, 拿 boundingClientRect
    try:
        js_get_box = """
        (() => {
            const iframes = document.querySelectorAll('iframe');
            const results = [];
            for (const ifr of iframes) {
                const r = ifr.getBoundingClientRect();
                results.push({
                    src: ifr.src || ifr.getAttribute('src') || '',
                    title: ifr.title || '',
                    w: Math.round(r.width), h: Math.round(r.height),
                    x: Math.round(r.left), y: Math.round(r.top),
                });
            }
            return JSON.stringify(results);
        })();
"""
        result = await page.evaluate(js_get_box)
        import json as _ij
        iframe_list = _ij.loads(result) if result else []
        for ifr in iframe_list:
            src = (ifr.get("src") or "").lower()
            title = (ifr.get("title") or "").lower()
            if (("challenges.cloudflare.com" in src or "turnstile" in src
                 or "captcha" in src or "widget" in title
                 or "turnstile" in title or "cloudflare" in title
                 or "captcha" in title)
                    and ifr.get("w", 0) > 0 and ifr.get("h", 0) > 0):
                return None, {
                    "x": ifr["x"], "y": ifr["y"],
                    "width": ifr["w"], "height": ifr["h"],
                }
    except Exception:
        pass

    # 策略 D: 找 Turnstile 容器 div (cf-chl-widget / .cf-turnstile) 的 bounding box
    # 某些情况下 iframe 在 DOM 里但 page.frames / query_selector 检测不到,
    # 但 widget 容器 div 一定在 (hidden input id 形如 cf-chl-widget-xxx_response).
    try:
        js_widget_box = """
        (() => {
            const boxes = [];
            const sels = [
                'div[id*="cf-chl-widget"]', 'div.cf-turnstile',
                'div[class*="cf-chl"]', '[id*="cf-chl-widget"]',
            ];
            for (const sel of sels) {
                for (const el of document.querySelectorAll(sel)) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        boxes.push({
                            id: el.id || el.className || sel,
                            x: Math.round(r.left), y: Math.round(r.top),
                            w: Math.round(r.width), h: Math.round(r.height),
                        });
                    }
                }
            }
            // 去重 (同一元素可能命中多个选择器)
            const seen = new Set();
            const uniq = [];
            for (const b of boxes) {
                const key = b.x + ',' + b.y + ',' + b.w + ',' + b.h;
                if (!seen.has(key)) { seen.add(key); uniq.push(b); }
            }
            return JSON.stringify(uniq);
        })();
"""
        result = await page.evaluate(js_widget_box)
        import json as _ij
        widget_list = _ij.loads(result) if result else []
        if widget_list:
            wb = widget_list[0]
            return None, {"x": wb["x"], "y": wb["y"],
                          "width": wb["w"], "height": wb["h"]}
    except Exception:
        pass

    return None, None


async def solve_captcha(page, max_wait=120, click_after=8):
    """等待 CAPTCHA / Turnstile / Managed Challenge 自动通过; 失败则坐标点击.

    行为:
      1. 检测到 CF challenge iframe 后, 优先等待自动通过
         (managed challenge 多数情况会自动评估, 不需要交互)
      2. iframe 持续存在超过 click_after 秒未消失, 用 page.mouse.click 坐标点击
         iframe 内复选框位置 (左上区域)
      3. 点击前做随机鼠标移动 + 短停顿 (拟人化, 避免 CF 检测)
      4. 第一次点击后等 15s, 不通过再点击不同 offset
      5. 整个流程上限 max_wait 秒 (managed challenge 默认 120s)

    跨域 iframe JS 不能 click 内部元素, 但 page.mouse.click 是浏览器层面的
    合成事件, 直接发 (x, y), 不受同源策略限制, 可"穿透" iframe.

    Managed Challenge 检测: page.url 含 challenges.cloudflare.com 或
    "Just a moment" / "Performing security verification" 等关键词

    返回 True 表示验证已通过, False 表示超时仍未通过.
    """
    import random as _rand
    log("🛡️ 等待 CAPTCHA/Turnstile/Managed Challenge 自动通过 "
        f"(最长 {max_wait}s, {click_after}s 后尝试坐标点击)...")
    end_time = time.time() + max_wait
    iframe_first_seen = None
    last_click_at = None
    click_count = 0
    last_diag_dump = 0

    while time.time() < end_time:
        try:
            # 1. 检查是否还在 CF challenge 页 (URL 含 /cdn-cgi/ 或 challenges.cloudflare.com)
            current_url = (page.url or "").lower()
            on_challenge_url = (
                "challenges.cloudflare.com" in current_url
                or "/cdn-cgi/" in current_url
            )

            # 2. 检查 iframe
            has_frame = False
            try:
                for fr in page.frames:
                    furl = (fr.url or "").lower()
                    if ("challenges.cloudflare.com" in furl
                            or "turnstile" in furl
                            or "captcha" in furl
                            or "/cdn-cgi/" in furl):
                        has_frame = True
                        break
            except Exception:
                pass

            # 3. 用 detect_captcha 复用判断 (已优先检查 cf-turnstile-response value)
            has_captcha = await detect_captcha(page)

            # 4. 全部消失 -> 通过
            # 注意: Turnstile widget 通过后 iframe 仍会留在 DOM 里 (只是变绿色对勾),
            # 所以不能只看 has_frame / on_challenge_url. detect_captcha 现在会检查
            # cf-turnstile-response 是否有 value, 有 value 即返回 False (已通过).
            # 因此只要 has_captcha=False 就算通过.
            if not has_captcha:
                if iframe_first_seen:
                    elapsed = time.time() - iframe_first_seen
                    log(f"  ✅ CAPTCHA/Challenge 已通过 (耗时 {elapsed:.1f}s)")
                else:
                    log("  ✅ 未出现 CAPTCHA, 无需验证")
                return True

            # 5. 记录首次出现时间
            if (has_frame or has_captcha or on_challenge_url) and iframe_first_seen is None:
                iframe_first_seen = time.time()
                log("  📍 CAPTCHA/Challenge 首次出现")
                if on_challenge_url:
                    log(f"  (页面 URL 含 challenges.cloudflare.com, 这是 CF Managed Challenge)")

            # 5.5 诊断: 每隔 ~15s 检查 turnstile-response value (帮判断 widget 通过时机)
            if iframe_first_seen and (time.time() - last_diag_dump) > 15:
                try:
                    response_info = await page.evaluate("""
                    (() => {
                        const inputs = document.querySelectorAll('input[name="cf-turnstile-response"]');
                        const results = [];
                        for (const inp of inputs) {
                            results.push({
                                id: inp.id || '(no-id)',
                                valueLen: (inp.value || '').length,
                                valuePrefix: (inp.value || '').slice(0, 30),
                            });
                        }
                        return JSON.stringify(results);
                    })();
""")
                    import json as _ij
                    responses = _ij.loads(response_info) if response_info else []
                    if responses:
                        for r in responses:
                            if r.get("valueLen", 0) > 10:
                                log(f"  🎫 cf-turnstile-response 已有 value (len={r['valueLen']} id={r['id']})")
                                log(f"     value 前缀: {r['valuePrefix']}...")
                            else:
                                log(f"  ⏳ cf-turnstile-response 还没 value (id={r['id']} len={r['valueLen']})")
                except Exception:
                    pass
                last_diag_dump = time.time()

            # 6. 等待超过 click_after 秒仍未通过 -> 坐标点击
            #    触发条件: 检测到 CAPTCHA (widget/input 存在) 即可尝试, 不必依赖
            #    iframe URL 检测 (有时 iframe 在 DOM 里但 page.frames 检测不到)
            if (iframe_first_seen is not None
                    and time.time() - iframe_first_seen >= click_after
                    and (has_frame or has_captcha)):
                iframe_el, box = await find_turnstile_iframe_box(page)
                if box:
                    # 3 种 offset 循环: 最左上 / 标准 / 更左上
                    click_offsets = [
                        (max(20, box['width'] * 0.08), box['height'] * 0.30),
                        (max(30, box['width'] * 0.10), box['height'] * 0.40),
                        (max(15, box['width'] * 0.05), box['height'] * 0.25),
                    ]
                    off = click_offsets[click_count % len(click_offsets)]
                    target_x = box['x'] + off[0]
                    target_y = box['y'] + off[1]
                    try:
                        # 拟人化: 先随机移动几下, 短停顿, 再点击
                        for _ in range(2):
                            await page.mouse.move(
                                box['x'] + _rand.uniform(50, max(100, box['width'])),
                                box['y'] + _rand.uniform(20, max(40, box['height'])),
                            )
                            await asyncio.sleep(_rand.uniform(0.2, 0.5))
                        await asyncio.sleep(_rand.uniform(0.3, 0.8))
                        await page.mouse.click(target_x, target_y)
                        last_click_at = time.time()
                        click_count += 1
                        log(f"  🖱️ 第 {click_count} 次坐标点击 CAPTCHA checkbox "
                            f"({target_x:.0f}, {target_y:.0f}) "
                            f"(iframe {box['width']:.0f}x{box['height']:.0f} "
                            f"offset {off[0]:.2f},{off[1]:.2f})")
                    except Exception as e:
                        log(f"  ⚠️ 坐标点击失败: {e}")
                    # 第一次点击后等 15s 再决定是否重试
                    if click_count == 1:
                        await asyncio.sleep(15)
                        continue
                else:
                    # 找不到 iframe box (可能是纯 managed challenge 无 visible widget)
                    # 只能继续等, 限流诊断避免日志爆炸
                    if time.time() - last_diag_dump > 15:
                        log("  ⏳ 找不到 visible iframe box (可能是 Managed Challenge "
                            "无 visible checkbox), 继续等待自动通过...")
                        last_diag_dump = time.time()

            await asyncio.sleep(3)
        except Exception as e:
            log(f"  ⚠️ CAPTCHA 等待异常: {e}")
            await asyncio.sleep(2)

    # 超时退出
    if last_click_at:
        log(f"⏰ CAPTCHA 已点击 {click_count} 次但 {max_wait}s 内仍未通过")
    else:
        log(f"⏰ CAPTCHA 验证超时 ({max_wait}s), 未能自动通过也未尝试点击")
    return False


# ============================================================
# Login detection
# ============================================================

async def check_login(page):
    url = page.url.lower()

    if (
        "/login" in url
        or "/auth" in url
    ):
        return False

    text = (
        await get_body_text(page)
    ).lower()

    login_words = [
        "log in",
        "login",
        "sign in",
    ]

    for word in login_words:
        if word in text:
            # 如果页面明显出现 Timer，
            # 则不能简单判定为登录失效
            if (
                "timer" not in text
                and "server" not in text
            ):
                return False

    return True


# ============================================================
# Add Time
# ============================================================

async def find_add_time_button(page):

    # 支持中英文按钮文本:
    # 英文: "Add Time"  /  中文: "添加时间"
    # Falix 改版兼容: 按钮可能是 button/a/div/[role=button],
    # 文本可能带额外内容 (如 "+1 Hour"), 因此加了任意可点击元素匹配
    # 和 JS 遍历兜底 (兼容 div/span 伪按钮).
    add_en = re.compile(
        r"add\s*time",
        re.I,
    )
    add_cn = re.compile(
        r"添加\s*时间",
    )

    candidates = [
        # 1. 英文 - role=button name 精确匹配
        page.get_by_role(
            "button",
            name=re.compile(
                r"^\s*Add\s+Time\s*$",
                re.I,
            ),
        ),

        # 2. 中文 - role=button name 精确匹配
        page.get_by_role(
            "button",
            name=re.compile(
                r"^\s*添加\s*时间\s*$",
            ),
        ),

        # 3. 英文 - get_by_text 精确匹配
        page.get_by_text(
            re.compile(
                r"^\s*Add\s+Time\s*$",
                re.I,
            ),
        ),

        # 4. 中文 - get_by_text 精确匹配
        page.get_by_text(
            re.compile(
                r"^\s*添加\s*时间\s*$",
            ),
        ),

        # 5. 英文 - 任意可点击元素 (button/a/input/[role=button]) 含 "Add Time"
        page.locator(
            'button, a, input[type="button"], input[type="submit"], [role="button"]'
        ).filter(
            has_text=add_en,
        ),

        # 6. 中文 - 任意可点击元素含 "添加时间"
        page.locator(
            'button, a, input[type="button"], input[type="submit"], [role="button"]'
        ).filter(
            has_text=add_cn,
        ),
    ]

    for locator in candidates:
        try:
            count = await locator.count()

            for index in range(count):
                item = locator.nth(index)

                if await item.is_visible():
                    return item

        except Exception:
            continue

    # 7. JS 兜底: 遍历全部元素, 按 "元素自身直接文本" 匹配
    #    (兼容 div/span 伪按钮), 优先可点击标签, 再取 DOM 最深
    #     (最接近真实按钮), 打标记后返回
    try:
        marked = await page.evaluate("""(() => {
            const RE = /add\\s*time|添加\\s*时间/i;
            const clickable = 'button, a, input[type="button"], input[type="submit"], [role="button"], [onclick]';
            const directText = el => Array.from(el.childNodes)
                .filter(n => n.nodeType === 3)
                .map(n => n.textContent || '')
                .join('')
                .replace(/\\s+/g, ' ')
                .trim();
            const depth = el => {
                let d = 0, p = el;
                while (p.parentElement) { d++; p = p.parentElement; }
                return d;
            };
            let best = null, bestScore = -1;
            for (const el of document.querySelectorAll('body *')) {
                const dt = directText(el);
                if (!dt || dt.length > 60 || !RE.test(dt)) continue;
                if (el.closest('[data-falix-addtime]')) continue;
                const score = (el.matches(clickable) ? 1000 : 0) + depth(el);
                if (score > bestScore) { bestScore = score; best = el; }
            }
            if (best) { best.setAttribute('data-falix-addtime', '1'); return true; }
            return false;
        })()""")

        if marked:
            loc = page.locator('[data-falix-addtime="1"]')

            count = await loc.count()

            for index in range(count):
                item = loc.nth(index)

                if await item.is_visible():
                    return item

    except Exception as e:
        log(
            f"⚠️ JS 查找 Add Time 兜底失败: {e}"
        )

    return None


async def click_add_time(
    page,
    server_id,
):
    button = (
        await find_add_time_button(page)
    )

    if button is None:
        log(
            "❌ 找不到 Add Time"
        )

        await save_debug(
            page,
            server_id,
            "add_time_missing",
        )

        return False

    try:
        await button.scroll_into_view_if_needed()

        await page.wait_for_timeout(
            1000
        )

        log(
            "🖱️ 点击 Add Time"
        )

        await button.click(
            timeout=10000
        )

        return True

    except Exception as e:
        log(
            f"❌ Add Time 点击失败: {e}"
        )

        await save_debug(
            page,
            server_id,
            "click_error",
        )

        return False


# ============================================================
# Wait for timer
# ============================================================

async def wait_timer_change(
    page,
    server_id,
    before,
    timeout=40,
):
    end_time = (
        time.time()
        + timeout
    )

    while time.time() < end_time:
        await page.wait_for_timeout(
            2000
        )

        current = (
            await get_remaining_seconds(
                page
            )
        )

        if current is None:
            continue

        log(
            "⏱️ Timer: "
            f"{format_seconds(before)}"
            " -> "
            f"{format_seconds(current)}"
        )

        if before is None:
            if current > 0:
                return True

        elif current > before + 30:
            return True

    return False


# ============================================================
# One server
# ============================================================

async def process_server(
    page,
    server_id,
):
    log("")
    log("=" * 70)
    log(
        f"🚀 开始处理服务器: "
        f"{server_id}"
    )
    log("=" * 70)

    url = TIMER_URL.format(
        server_id
    )

    log(
        f"🌐 {url}"
    )

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

    except PlaywrightTimeoutError:
        log(
            "⚠️ 页面加载超时，继续检查"
        )

    except Exception as e:
        log(
            f"❌ 打开页面失败: {e}"
        )

        await save_debug(
            page,
            server_id,
            "goto_error",
        )

        return False

    # 等待 Timer 页面真正渲染完成 (兼容 "Loading timer..." 慢加载)
    await wait_page_ready(page)

    # Login
    if not await check_login(page):
        log(
            "❌ Falix 登录状态失效"
        )

        await save_debug(
            page,
            server_id,
            "login_required",
        )

        send_tg(
            "❌ Falix 登录状态失效\n"
            f"Server ID: {server_id}"
        )

        return False

    # CAPTCHA - 自动尝试通过 (移植自 zampto solve_turnstile)
    if await detect_captcha(page):
        log("🛡️ 检测到 CAPTCHA / Turnstile, 尝试自动通过...")

        await save_debug(page, server_id, "captcha_before")

        # 尝试自动通过 CAPTCHA (先等自动通过, 失败则坐标点击 iframe)
        captcha_ok = await solve_captcha(page, max_wait=120, click_after=8)

        if not captcha_ok:
            log("❌ CAPTCHA 自动通过失败 (120s 内未通过)")

            await save_debug(page, server_id, "captcha_failed")

            send_tg(
                "⚠️ Falix CAPTCHA 自动通过失败\n"
                f"Server ID: {server_id}\n"
                "已尝试坐标点击 iframe checkbox (120s), 仍无法通过."
            )

            return False

        log("✅ CAPTCHA 已自动通过, 继续检查 Timer")

        await save_debug(page, server_id, "captcha_passed")

        # 通过后重新检查登录状态 (有时 CF 通过会跳转回登录页)
        if not await check_login(page):
            log("❌ CAPTCHA 通过后 Falix 登录状态失效")

            await save_debug(page, server_id, "login_required_after_captcha")

            send_tg(
                "❌ Falix 登录状态失效 (CAPTCHA 通过后)\n"
                f"Server ID: {server_id}"
            )

            return False

    before = (
        await get_remaining_seconds(
            page
        )
    )

    log(
        "⏱️ 当前 Timer: "
        f"{format_seconds(before)}"
    )

    # 未读到 Timer: 页面可能停在异常状态 (CF challenge / 无 Timer / 已过期),
    # 直接提示, 不要误判为 "Add Time 按钮异常"
    if before is None:
        state = await classify_page_state(page)

        if state in (
            "CF_CHALLENGE",
            "OFFLINE_NO_TIMER",
            "EXPIRED",
            "LOADING",
        ):
            log(
                f"⚠️ 未读取到 Timer, 页面状态: {state}"
            )

            send_tg(
                "⚠️ Falix Timer 未读取到\n"
                f"Server ID: {server_id}\n"
                f"页面状态: {state}"
            )

            return False

    # Timer 足够，不续期
    if before is not None:
        threshold = (
            RENEW_BELOW_MINUTES
            * 60
        )

        if before > threshold:
            log(
                "✅ Timer 尚未低于阈值"
            )

            send_tg(
                "⏭️ Falix Timer 充足, 无需续期\n"
                f"Server ID: {server_id}\n"
                f"Timer: {format_seconds(before)}"
            )

            return True

    # Add Time
    button = (
        await find_add_time_button(page)
    )

    if button is None:
        state = await classify_page_state(page)

        snippet = (
            (await get_body_text(page))
            .replace("\n", " ")
        )[:200]

        log(
            "❌ 当前页面没有 Add Time "
            f"(页面状态: {state})"
        )

        await save_debug(
            page,
            server_id,
            "button_missing",
        )

        send_tg(
            "❌ Falix Add Time 按钮异常\n"
            f"Server ID: {server_id}\n"
            f"页面状态: {state}\n"
            f"页面文本: {snippet}"
        )

        return False

    log(
        "✅ 找到 Add Time"
    )

    # Click
    clicked = await click_add_time(
        page,
        server_id,
    )

    if not clicked:
        return False

    await page.wait_for_timeout(
        2000
    )

    # 点击后可能出现验证 - 自动尝试通过
    if await detect_captcha(page):
        log("🛡️ 点击 Add Time 后出现 CAPTCHA, 尝试自动通过...")

        await save_debug(page, server_id, "captcha_after_click_before")

        captcha_ok = await solve_captcha(page, max_wait=120, click_after=8)

        if not captcha_ok:
            log("❌ 点击后 CAPTCHA 自动通过失败 (120s 内未通过)")

            await save_debug(page, server_id, "captcha_after_click_failed")

            send_tg(
                "⚠️ Falix 点击 Add Time 后 CAPTCHA 自动通过失败\n"
                f"Server ID: {server_id}\n"
                "已尝试坐标点击 iframe checkbox (120s), 仍无法通过."
            )

            return False

        log("✅ 点击后 CAPTCHA 已自动通过, 验证续期结果")

        await save_debug(page, server_id, "captcha_after_click_passed")

    # Verify
    success = await wait_timer_change(
        page,
        server_id,
        before,
    )

    after = (
        await get_remaining_seconds(
            page
        )
    )

    if success:
        log(
            "🎉 续期成功"
        )

        log(
            "⏱️ 新 Timer: "
            f"{format_seconds(after)}"
        )

        send_tg(
            "✅ Falix 续期成功\n"
            f"Server ID: {server_id}\n"
            f"Timer: {format_seconds(after)}"
        )

        await save_debug(
            page,
            server_id,
            "success",
        )

        return True

    log(
        "❌ 点击后 Timer 没有增加"
    )

    await save_debug(
        page,
        server_id,
        "renew_failed",
    )

    send_tg(
        "❌ Falix 续期失败\n"
        f"Server ID: {server_id}\n"
        f"Before: {format_seconds(before)}\n"
        f"After: {format_seconds(after)}"
    )

    return False


# ============================================================
# Main
# ============================================================

async def main():

    if not SERVER_IDS:
        log(
            "❌ 没有设置 FALIX_SERVER_IDS"
        )

        sys.exit(1)

    log(
        "======================================"
    )

    log(
        "Falix Renew Pro v2 (Falix 自动续期)"
    )

    log(
        f"服务器数量: {len(SERVER_IDS)}"
    )

    log(
        f"续期阈值: {RENEW_BELOW_MINUTES} 分钟"
    )

    log(
        f"无头模式: {HEADLESS}"
    )

    log(
        "======================================"
    )

    if not Path(
        STORAGE_STATE
    ).exists():
        log(
            "❌ storage_state 不存在:"
        )

        log(
            f"   {STORAGE_STATE}"
        )

        log(
            "请先运行 generate_storage.py"
        )

        os._exit(1)

    # 选择浏览器引擎: 优先 cloakbrowser (stealth, 绕 CF 指纹), 否则降级 playwright
    log(f"🔍 Browser engine: {'cloakbrowser (stealth)' if HAS_CLOAKBROWSER else 'playwright (raw)'}")
    log(f"🔍 Proxy: {'enabled (socks5h://127.0.0.1:1080)' if USE_PROXY else 'disabled (direct)'}")

    if HAS_CLOAKBROWSER:
        # CloakBrowser: 专用 stealth Chromium, 用 launch_context_async (async API)
        # launch_context_async 直接返回 BrowserContext (async), 不需要先 launch 再 new_context
        # 它内部启动 stealth Chromium, 已注入绕过 CF 指纹检测的 patches:
        #   * 真实 Chromium 二进制 (非 playwright 自带的)
        #   * UA / navigator.webdriver / canvas / WebGL 指纹修改
        #   * 屏蔽 CDP (Chrome DevTools Protocol) 痕迹
        proxy_arg = None
        if USE_PROXY:
            # socks5 = socks5 with hostname resolution on proxy side (避免 DNS 泄露)
            # 注意: cloakbrowser 接受字符串 URL 或 Playwright proxy dict
            proxy_arg = "socks5://127.0.0.1:1080"

        # launch_context_async 接受所有 new_context() 的 kwargs (storage_state, viewport, ...)
        context = await cloakbrowser_launch_async(
            headless=HEADLESS,
            proxy=proxy_arg,
            # geoip=True 让 cloakbrowser 根据 IP 自动设置时区/locale/语言,
            # 模拟真实用户环境 (CF managed challenge 会检查这些)
            geoip=True,
            # humanize=True 让 cloakbrowser 注入人类化鼠标移动轨迹,
            # 进一步降低被识别为自动化的概率
            humanize=True,
            # 以下 kwargs 透传给 browser.new_context()
            storage_state=STORAGE_STATE,
            viewport={
                "width": 1440,
                "height": 1000,
            },
        )

        page = await context.new_page()

        page.set_default_timeout(
            15000
        )

        results = []

        for server_id in SERVER_IDS:

            try:
                result = (
                    await process_server(
                        page,
                        server_id,
                    )
                )

            except Exception as e:
                log(
                    f"❌ 未处理异常: {e}"
                )

                await save_debug(
                    page,
                    server_id,
                    "exception",
                )

                send_tg(
                    "💥 Falix 脚本异常\n"
                    f"Server ID: {server_id}\n"
                    f"Error: {e}"
                )

                result = False

            results.append(
                (
                    server_id,
                    result,
                )
            )

        # launch_context_async 返回的 context 关闭时会自动关闭 underlying browser
        await context.close()

    else:
        # Fallback: 纯 playwright (开发/调试用, 必被 CF 拒)
        log("⚠️ cloakbrowser 不可用, 降级到 playwright (CF managed challenge 可能无法通过)")

        async with async_playwright() as playwright:

            proxy_arg = None
            if USE_PROXY:
                proxy_arg = {"server": "socks5://127.0.0.1:1080"}

            browser = await playwright.chromium.launch(
                headless=HEADLESS,
                proxy=proxy_arg,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )

            context = await browser.new_context(
                storage_state=STORAGE_STATE,
                viewport={
                    "width": 1440,
                    "height": 1000,
                },
                locale="en-US",
                timezone_id="Asia/Tokyo",
            )

            page = await context.new_page()

            page.set_default_timeout(
                15000
            )

            results = []

            for server_id in SERVER_IDS:

                try:
                    result = (
                        await process_server(
                            page,
                            server_id,
                        )
                    )

                except Exception as e:
                    log(
                        f"❌ 未处理异常: {e}"
                    )

                    await save_debug(
                        page,
                        server_id,
                        "exception",
                    )

                    send_tg(
                        "💥 Falix 脚本异常\n"
                        f"Server ID: {server_id}\n"
                        f"Error: {e}"
                    )

                    result = False

                results.append(
                    (
                        server_id,
                        result,
                    )
                )

            await context.close()

            await browser.close()

    log("")
    log("=" * 70)
    log("处理结果")
    log("=" * 70)

    success = 0

    for server_id, result in results:

        status = (
            "✅ OK"
            if result
            else "❌ FAILED"
        )

        log(
            f"{server_id}: {status}"
        )

        if result:
            success += 1

    log(
        f"成功: {success}/"
        f"{len(results)}"
    )

    # 汇总 TG 通知 (每个服务器状态)
    summary_lines = ["🎮 Falix 自动续期", f"✅ 成功: {success}/{len(results)}"]
    for server_id, result in results:
        st = "✅ OK" if result else "❌ FAILED"
        summary_lines.append(f"{st} {server_id}")
    send_tg("\n".join(summary_lines))

    if success != len(results):
        # 用 os._exit 替代 sys.exit: 避免 Playwright event loop 在退出时
        # 干扰 SystemExit, 导致 exit code 非 0 (zampto 项目同款修复)
        os._exit(1)

    # 全部成功也用 os._exit(0), 保证 workflow 显示 success
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())
