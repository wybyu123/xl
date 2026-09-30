import json
import os
import re
from playwright.sync_api import sync_playwright

OUTPUT_DIR = "output"
TASKS = [
    "https://pan.xunlei.com/s/VP1-tZ2IghVc0v-JplODurv_A1?pwd=m75p",
]


def parse_task(task_str: str) -> tuple:
    """解析分享链接中的 share_id 与 pwd"""
    share_id_match = re.search(r"xunlei\.com/s/([a-zA-Z0-9_-]+)", task_str)
    pwd_match = re.search(r"pwd=([a-zA-Z0-9]+)", task_str)

    share_id = share_id_match.group(1) if share_id_match else ""
    pwd = pwd_match.group(1) if pwd_match else ""
    return share_id, pwd


def run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        page = context.new_page()

        for idx, task in enumerate(TASKS, 1):
            share_id, pwd = parse_task(task)
            if not share_id:
                print(f"[{idx}] 无效任务，跳过: {task}")
                continue

            print(f"\n[任务 {idx}] 开始解析 Share ID: {share_id}")

            captured_files = []
            pass_code_token = ""

            def handle_response(response):
                nonlocal pass_code_token, captured_files
                url = response.url

                # 捕获 passcode token 响应
                if (
                    "/drive/v1/share/passcode" in url
                    and response.status == 200
                ):
                    try:
                        res_json = response.json()
                        pass_code_token = res_json.get("pass_code_token", "")
                        print(
                            f"   [√] 成功拦截并获取 pass_code_token:"
                            f" {pass_code_token[:10]}..."
                        )
                    except Exception:
                        pass

                # 捕获文件列表明细响应
                elif (
                    "/drive/v1/share/detail" in url and response.status == 200
                ):
                    try:
                        res_json = response.json()
                        files = res_json.get("files", [])
                        for item in files:
                            if item.get("kind") != "drive#folder":
                                gcid = item.get("gcid", "")
                                md5_val = (
                                    item.get("hash")
                                    or item.get("md5")
                                    or (gcid[:32] if gcid else "")
                                )
                                captured_files.append({
                                    "size": str(item.get("size", "0")),
                                    "path": "/" + item.get("name", ""),
                                    "gcid": gcid,
                                    "md5": md5_val,
                                    "fileId": item.get("id", ""),
                                    "cid": item.get("cid", ""),
                                    "parentId": item.get("parent_id", ""),
                                    "downloadUrl": item.get("web_content_link")
                                    or item.get("download_url")
                                    or "",
                                    "sourceXunlei": True,
                                    "shareId": share_id,
                                    "passCodeToken": pass_code_token,
                                })
                        print(
                            f"   [+] 成功拦截获取 {len(files)} 个文件节点数据"
                        )
                    except Exception:
                        pass

            page.on("response", handle_response)

            target_url = f"https://pan.xunlei.com/s/{share_id}?pwd={pwd}"
            print(f" └─ 正在加载页面: {target_url}")

            # 核心修改：等待 DOM 加载完成即可，避免因后台长连接导致 Timeout
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"   [!] 页面加载异常/超时，尝试继续执行: {e}")

            # 给异步 API 请求预留 3 秒响应缓冲
            page.wait_for_timeout(3000)

            # 检查是否有提取码输入框并自动填表提交
            try:
                pwd_input = page.locator(
                    "input[placeholder*='提取码'], input[placeholder*='密码']"
                )
                if pwd_input.is_visible(timeout=3000):
                    print(" └─ 发现提取码输入框，自动填写并提交...")
                    pwd_input.fill(pwd)
                    btn = page.locator(
                        "button:has-text('提取'), button:has-text('确定')"
                    )
                    btn.click()
                    page.wait_for_timeout(3000)
            except Exception:
                pass

            # 组装输出 JSON
            out_data = {
                "scriptVersion": "1.0.0",
                "scriptAuthor": "sumuve",
                "totalFilesCount": len(captured_files),
                "files": captured_files,
                "sourceTag": "xunlei",
                "shareId": share_id,
                "passCodeToken": pass_code_token,
            }

            out_path = os.path.join(OUTPUT_DIR, f"share_{share_id}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(out_data, f, ensure_ascii=False, indent=2)

            print(
                f" ✅ 任务完成，写入 {out_path}，包含文件: {len(captured_files)} 个"
            )

            # 移除当前页面的响应监听器
            page.remove_listener("response", handle_response)

        browser.close()


if __name__ == "__main__":
    run()
