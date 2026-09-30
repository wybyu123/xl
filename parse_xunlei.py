#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
迅雷分享 → 秒传 JSON（Playwright 模拟浏览器）

对齐油猴「光鸭秒传」思路：
  1. 打开分享页，自动填提取码
  2. 拦截 api-pan.xunlei.com 全部相关响应，抓 pass_code_token / device / captcha
  3. 在页面内用 fetch 递归调用 /drive/v1/share/detail（不依赖用户点进文件夹）
  4. gcid：优先 hash；为空则从 thumbnail_link 的 gcid= 解析（分享页常见）
  5. 输出标准秒传 JSON

依赖:
  pip install playwright
  playwright install --with-deps chromium

运行:
  python parse_xunlei.py
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

# =====================================================================
# 任务列表：每项可以是完整链接（可带 ?pwd=）
# =====================================================================
OUTPUT_DIR = "output"
TASKS = [
    "https://pan.xunlei.com/s/VP1-tZ2IghVc0v-JplODurv_A1?pwd=m75p",
]

# 无头；GitHub Actions 保持 True
HEADLESS = os.environ.get("HEADLESS", "1") not in ("0", "false", "False")
# 等待 pass_code_token 的最长时间（秒）
WAIT_TOKEN_SEC = int(os.environ.get("WAIT_TOKEN_SEC", "60"))
# 每次 API 间隔，降低风控
API_DELAY_MS = int(os.environ.get("API_DELAY_MS", "120"))
CLIENT_ID = "Xqp0kJBXWhwaTpB6"
# =====================================================================

SCRIPT_VERSION = "1.0.0"
SCRIPT_AUTHOR = "sumuve"
API_HOST = "api-pan.xunlei.com"


def format_size(n: int | float) -> str:
    n = float(n or 0)
    if n < 1024:
        return f"{int(n)} B"
    units = ["KB", "MB", "GB", "TB"]
    v = n / 1024.0
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.2f} {units[i]}"


def parse_task(task_str: str) -> tuple[str, str]:
    """解析分享链接中的 share_id 与 pwd。"""
    share_id_match = re.search(r"xunlei\.com/s/([a-zA-Z0-9_-]+)", task_str)
    if not share_id_match:
        share_id_match = re.search(r"/s/([a-zA-Z0-9_-]+)", task_str)
    pwd_match = re.search(r"(?:pwd|password|pass_code)=([a-zA-Z0-9]+)", task_str, re.I)
    if not pwd_match:
        pwd_match = re.search(
            r"(?:提取码|密码)\s*[:：]\s*([a-zA-Z0-9]+)", task_str
        )
    share_id = share_id_match.group(1) if share_id_match else ""
    pwd = pwd_match.group(1) if pwd_match else ""
    return share_id, pwd


def extract_gcid_from_url(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"[?&]gcid=([0-9A-Fa-f]{40})\b", url)
    if m:
        return m.group(1).lower()
    m = re.search(r"[?&]g=([0-9A-Fa-f]{40})\b", url)
    if m:
        return m.group(1).lower()
    return ""


def pick_gcid(item: dict) -> str:
    """与油猴一致：hash 优先，其次 thumbnail_link 里的 gcid=。"""
    for k in ("hash", "gcid", "sha1", "fileHash", "contentHash", "merkleRoot"):
        v = str(item.get(k) or "").strip()
        if re.fullmatch(r"[0-9A-Fa-f]{40}", v):
            return v.lower()
    for k in ("thumbnail_link", "thumbnailLink"):
        g = extract_gcid_from_url(str(item.get(k) or ""))
        if g:
            return g
    params = item.get("params")
    if isinstance(params, dict):
        for v in params.values():
            s = str(v or "").strip()
            if re.fullmatch(r"[0-9A-Fa-f]{40}", s):
                return s.lower()
            g = extract_gcid_from_url(s)
            if g:
                return g
    return ""


def pick_md5(item: dict, gcid: str) -> str:
    for k in ("md5_checksum", "md5Checksum", "md5", "etag", "md5sum", "contentMd5"):
        v = str(item.get(k) or "").strip().lower().strip('"')
        if re.fullmatch(r"[0-9a-f]{32}", v):
            return v
    if gcid and re.fullmatch(r"[0-9a-f]{40}", gcid):
        return gcid[:32]
    return ""


def pick_download_url(item: dict) -> str:
    links = item.get("links")
    if isinstance(links, dict):
        for key in ("application/octet-stream", "*/*", "default"):
            obj = links.get(key)
            if isinstance(obj, dict) and obj.get("url"):
                u = str(obj["url"])
                if u.startswith("http") and "thumbnail" not in u and "88cdn" not in u:
                    return u
        for obj in links.values():
            if isinstance(obj, dict) and obj.get("url"):
                u = str(obj["url"])
                if u.startswith("http") and "thumbnail" not in u:
                    return u
    for k in ("web_content_link", "webContentLink", "downloadUrl", "download_url", "url"):
        u = str(item.get(k) or "").strip()
        if u.startswith("http") and "thumbnail" not in u and "88cdn" not in u:
            return u
    return ""


def is_folder(item: dict) -> bool:
    kind = str(item.get("kind") or "").lower()
    if "folder" in kind or kind == "drive#folder":
        return True
    if item.get("type") == "folder" or item.get("is_dir") is True:
        return True
    if (
        str(item.get("folder_type") or "")
        and int(item.get("size") or 0) == 0
        and not item.get("file_extension")
        and not item.get("mime_type")
    ):
        return True
    return False


def file_entry(
    item: dict,
    path: str,
    share_id: str,
    pass_code_token: str,
) -> dict:
    gcid = pick_gcid(item)
    md5 = pick_md5(item, gcid)
    dl = pick_download_url(item)
    if not gcid and dl:
        gcid = extract_gcid_from_url(dl)
        if gcid and not md5:
            md5 = gcid[:32]
    return {
        "size": str(int(item.get("size") or 0)),
        "path": path if path.startswith("/") else "/" + path,
        "gcid": gcid or "",
        "md5": md5 or "",
        "fileId": str(item.get("id") or ""),
        "cid": "",
        "parentId": str(item.get("parent_id") or ""),
        "downloadUrl": dl or "",
        "sourceXunlei": True,
        "shareId": share_id,
        "passCodeToken": pass_code_token,
    }


class CaptureState:
    def __init__(self) -> None:
        self.share_id = ""
        self.pass_code_token = ""
        self.device_id = ""
        self.captcha_token = ""
        self.raw_hits = 0

    def on_request(self, url: str, headers: dict) -> None:
        if API_HOST not in url:
            return
        low = {str(k).lower(): v for k, v in (headers or {}).items()}
        did = low.get("x-device-id") or low.get("x-guid")
        if did:
            self.device_id = str(did)
        cap = low.get("x-captcha-token")
        if cap and len(str(cap)) > 16:
            self.captcha_token = str(cap)
        try:
            q = parse_qs(urlparse(url).query)
            if q.get("share_id"):
                self.share_id = q["share_id"][0]
            if q.get("pass_code_token"):
                self.pass_code_token = unquote_token(q["pass_code_token"][0])
        except Exception:
            pass

    def on_response_body(self, url: str, data: dict) -> None:
        if API_HOST not in url:
            return
        self.raw_hits += 1
        # 响应体里的 token（/share 或 passcode 类接口）
        for k in ("pass_code_token", "passCodeToken"):
            if data.get(k):
                self.pass_code_token = str(data[k])
        try:
            q = parse_qs(urlparse(url).query)
            if q.get("share_id"):
                self.share_id = q["share_id"][0]
            if q.get("pass_code_token"):
                self.pass_code_token = unquote_token(q["pass_code_token"][0])
        except Exception:
            pass


def unquote_token(s: str) -> str:
    from urllib.parse import unquote

    try:
        return unquote(s)
    except Exception:
        return s


def try_fill_password(page, pwd: str) -> None:
    if not pwd:
        return
    selectors = [
        "input[placeholder*='提取']",
        "input[placeholder*='密码']",
        "input[placeholder*='访问']",
        "input[type='password']",
        "input.ant-input",
    ]
    filled = False
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_visible(timeout=2000):
                continue
            loc.fill(pwd)
            filled = True
            print(f"   [√] 已填写提取码 ({sel})")
            break
        except Exception:
            continue
    if not filled:
        print("   [!] 未找到提取码输入框（可能已自动解锁或无需提取码）")
        return

    for text in ("确定", "提取文件", "提取", "提交", "验证", "确认"):
        try:
            btn = page.get_by_role("button", name=re.compile(text))
            if btn.count() > 0:
                btn.first.click(timeout=3000)
                print(f"   [√] 点击按钮: {text}")
                page.wait_for_timeout(2500)
                return
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
        page.wait_for_timeout(2000)
    except Exception:
        pass


def wait_token(page, state: CaptureState, timeout_sec: int) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if state.pass_code_token:
            return
        page.wait_for_timeout(400)
    raise TimeoutError(
        f"{timeout_sec}s 内未捕获 pass_code_token。"
        "请检查提取码、分享是否有效，或 GitHub runner 是否能访问迅雷。"
    )


def fetch_detail_all(
    page,
    state: CaptureState,
    share_id: str,
    pass_code_token: str,
    parent_id: str,
) -> list[dict]:
    """在浏览器上下文内 fetch share/detail，自动翻页。"""
    js = """
    async ({ shareId, passCodeToken, parentId, deviceId, captcha, clientId, delayMs }) => {
      const sleep = (ms) => new Promise(r => setTimeout(r, ms));
      const all = [];
      let pageToken = '';
      for (let page = 0; page < 80; page++) {
        const params = new URLSearchParams({
          share_id: shareId,
          parent_id: parentId || '',
          pass_code_token: passCodeToken,
          limit: '100',
          keyword: '',
          page_token: pageToken || '',
          scene: 'NORMAL',
          order: 'DEFAULT_ORDER',
          thumbnail_size: 'SIZE_MEDIUM',
        });
        if (deviceId) {
          params.set('device_id', deviceId);
          params.set('did', deviceId);
        }
        const headers = {
          'Accept': '*/*',
          'Content-Type': 'application/json',
          'X-Client-Id': clientId || 'Xqp0kJBXWhwaTpB6',
        };
        if (captcha) {
          headers['X-Captcha-Token'] = captcha;
        }
        if (deviceId) {
          headers['X-Device-Id'] = deviceId;
          headers['x-device-id'] = deviceId;
          headers['X-Guid'] = deviceId;
        }
        const url = 'https://api-pan.xunlei.com/drive/v1/share/detail?' + params.toString();
        let resp, text;
        try {
          resp = await fetch(url, { method: 'GET', headers, credentials: 'include' });
          text = await resp.text();
        } catch (e) {
          return { error: String(e), files: all };
        }
        let data;
        try { data = JSON.parse(text); } catch (e) {
          return { error: 'json_parse status=' + resp.status, files: all, preview: text.slice(0, 180) };
        }
        if (!resp.ok) {
          return {
            error: 'http_' + resp.status,
            files: all,
            body: data,
          };
        }
        const files = data.files || [];
        for (const f of files) all.push(f);
        pageToken = data.next_page_token || '';
        if (!pageToken) {
          return { files: all, parent: data.parent || null };
        }
        await sleep(delayMs || 100);
      }
      return { files: all };
    }
    """
    result = page.evaluate(
        js,
        {
            "shareId": share_id,
            "passCodeToken": pass_code_token,
            "parentId": parent_id or "",
            "deviceId": state.device_id or "",
            "captcha": state.captcha_token or "",
            "clientId": CLIENT_ID,
            "delayMs": API_DELAY_MS,
        },
    )
    if not isinstance(result, dict):
        return []
    if result.get("error"):
        print(f"   [!] detail parent={parent_id or '(root)'}: {result.get('error')}")
        body = result.get("body")
        if body:
            print(f"       body: {json.dumps(body, ensure_ascii=False)[:200]}")
        return list(result.get("files") or [])
    return list(result.get("files") or [])


def collect_recursive(
    page,
    state: CaptureState,
    share_id: str,
    pass_code_token: str,
    parent_id: str,
    path_prefix: str,
) -> list[dict]:
    items = fetch_detail_all(page, state, share_id, pass_code_token, parent_id)
    out: list[dict] = []
    print(f"   [+] 目录 [{path_prefix or '/'}] 节点数: {len(items)}")
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        name = str(item.get("name") or "unnamed").strip() or "unnamed"
        rel = f"{path_prefix}/{name}" if path_prefix else name
        if is_folder(item):
            print(f"   → 递归文件夹: {rel}")
            out.extend(
                collect_recursive(
                    page,
                    state,
                    share_id,
                    pass_code_token,
                    str(item["id"]),
                    rel,
                )
            )
        else:
            out.append(
                file_entry(
                    item,
                    "/" + rel.lstrip("/"),
                    share_id,
                    pass_code_token,
                )
            )
    return out


def process_one(page, task: str, idx: int) -> dict[str, Any]:
    share_id, pwd = parse_task(task)
    if not share_id:
        print(f"[{idx}] 无效任务，跳过: {task}")
        return {}

    print(f"\n[任务 {idx}] Share ID: {share_id}  pwd: {pwd or '(无)'}")
    state = CaptureState()
    state.share_id = share_id

    def on_request(req):
        try:
            state.on_request(req.url, req.headers)
        except Exception:
            pass

    def on_response(resp):
        try:
            if API_HOST not in resp.url or resp.status != 200:
                return
            # 先从 URL 拿 token（detail 请求必带）
            state.on_request(resp.url, resp.request.headers)
            try:
                data = resp.json()
            except Exception:
                return
            if isinstance(data, dict):
                state.on_response_body(resp.url, data)
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)

    target_url = f"https://pan.xunlei.com/s/{share_id}"
    if pwd:
        target_url += f"?pwd={pwd}"
    print(f" └─ 加载: {target_url}")

    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"   [!] goto 异常，继续: {e}")

    page.wait_for_timeout(2000)
    try_fill_password(page, pwd)
    page.wait_for_timeout(2000)

    # 再等网络里出现 token
    print(" └─ 等待 pass_code_token …")
    try:
        wait_token(page, state, WAIT_TOKEN_SEC)
    except TimeoutError as e:
        # 兜底：有时 token 只在首次 share 接口里，再等一会点一下页面
        print(f"   [!] {e}")
        page.wait_for_timeout(3000)
        if not state.pass_code_token:
            # 尝试再填一次密码
            try_fill_password(page, pwd)
            page.wait_for_timeout(4000)
        if not state.pass_code_token:
            raise

    print(
        f"   [√] pass_code_token={state.pass_code_token[:16]}… "
        f"device={state.device_id[:12] if state.device_id else '-'} "
        f"captcha={'有' if state.captcha_token else '无'}"
    )

    # 根目录名
    root_name = ""
    try:
        root_files = fetch_detail_all(
            page, state, share_id, state.pass_code_token, ""
        )
        # 再拉一次拿 parent 名：evaluate 里已返回 parent，这里简单用页面 title
        title = page.title() or ""
        root_name = re.sub(r"\s*[-|].*$", "", title).strip()
    except Exception:
        root_files = []

    print(" └─ 递归收集文件…")
    files = collect_recursive(
        page,
        state,
        share_id,
        state.pass_code_token,
        "",
        root_name,
    )

    # 若根名为空且只有一层，path 仍正确
    no_gcid = sum(1 for f in files if not f.get("gcid"))
    total_size = sum(int(f.get("size") or 0) for f in files)
    out_data = {
        "scriptVersion": SCRIPT_VERSION,
        "scriptAuthor": SCRIPT_AUTHOR,
        "totalFilesCount": len(files),
        "totalSize": total_size,
        "formattedTotalSize": format_size(total_size),
        "files": files,
        "sourceTag": "xunlei",
        "shareId": share_id,
        "passCodeToken": state.pass_code_token,
    }
    if no_gcid:
        out_data["skippedNoGcidCount"] = no_gcid
        print(f"   [!] 其中 {no_gcid} 个无 gcid")

    try:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)
    except Exception:
        pass

    return out_data


def run() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        page = context.new_page()

        for idx, task in enumerate(TASKS, 1):
            try:
                out_data = process_one(page, task, idx)
            except Exception as e:
                print(f" ❌ 任务失败: {e}")
                share_id, _ = parse_task(task)
                out_data = {
                    "scriptVersion": SCRIPT_VERSION,
                    "scriptAuthor": SCRIPT_AUTHOR,
                    "totalFilesCount": 0,
                    "files": [],
                    "sourceTag": "xunlei",
                    "shareId": share_id,
                    "passCodeToken": "",
                    "error": str(e),
                }

            if not out_data:
                continue
            sid = out_data.get("shareId") or f"task{idx}"
            out_path = os.path.join(OUTPUT_DIR, f"share_{sid}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(out_data, f, ensure_ascii=False, indent=2)
            print(
                f" ✅ 写入 {out_path}  文件数={out_data.get('totalFilesCount')}  "
                f"大小={out_data.get('formattedTotalSize', '-')}"
            )

        browser.close()


if __name__ == "__main__":
    run()
