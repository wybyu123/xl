import json
import os
import re
from playwright.sync_api import sync_playwright

OUTPUT_DIR = "output"
TASKS = [
    "https://pan.xunlei.com/s/VP1-tZ2IghVc0v-JplODurv_A1?pwd=m75p",
]


def parse_task(task_str: str) -> tuple:
    share_id_match = re.search(r"xunlei\.com/s/([a-zA-Z0-9_-]+)", task_str)
    pwd_match = re.search(r"pwd=([a-zA-Z0-9]+)", task_str)
    share_id = share_id_match.group(1) if share_id_match else ""
    pwd = pwd_match.group(1) if pwd_match else ""
    return share_id, pwd


def run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with sync_playwright() as p:
        # 使用 Chromium 并模拟真实桌面浏览器特征
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )

        # 屏蔽 webdriver 特征标志，防止被云盘反爬识别
        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () =>"
            " undefined})"
        )

        for idx, task in enumerate(TASKS, 1):
            share_id, pwd = parse_task(task)
            if not share_id:
                print(f"[{idx}] 无效任务，跳过: {task}")
                continue

            print(f"\n================ [任务 {idx}] ================")
            print(f"Share ID : {share_id}")
            print(f"提取码   : {pwd}")

            captured_files = []
            pass_code_token = ""

            # 全局监听网络请求，打印迅雷相关的所有 API 响应
            def handle_response(response):
                nonlocal pass_code_token, captured_files
                url = response.url

                if "xunlei.com" in url and (
                    "share" in url or "passcode" in url or "detail" in url
                ):
                    print(f"   [网络拦截] Status: {response.status} -> {url}")

                # 捕获 passcode token 响应
                if "passcode" in url and response.status == 200:
                    try:
                        res_json = response.json()
                        pass_code_token = res_json.get("pass_code_token", "")
                        print(
                            f"   [√] 成功获取 pass_code_token:"
                            f" {pass_code_token}"
                        )
                    except Exception as e:
                        print(f"   [!] 解析 passcode JSON 失败: {e}")

                # 捕获文件列表响应
                elif "detail" in url and response.status == 200:
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
                        print(f"   [+] 成功提取 {len(files)} 个文件信息")
                    except Exception as e:
                        print(f"   [!] 解析 detail JSON 失败: {e}")

            page.on("response", handle_response)

            target_url = f"https://pan.xunlei.com/s/{share_id}?pwd={pwd}"
            print(f" └─ 打开页面: {target_url}")

            try:
                page.goto(
                    target_url, wait_until="domcontentloaded", timeout=30000
                )
            except Exception as e:
                print(f"   [!] 页面打开超时/异常: {e}")

            page.wait_for_timeout(3000)
            print(f" └─ 页面标题: {page.title()}")

            # 尝试填充提取码并提交
            try:
                # 寻找输入框（匹配任何可用的输入框）
                input_elem = page.locator("input").first
                if input_elem.is_visible(timeout=3000):
                    print(" └─ 找到输入框，注入提取码并回车...")
                    input_elem.fill(pwd)
                    page.wait_for_timeout(500)
                    input_elem.press("Enter")

                    # 同时也尝试点击可能的“提取”或“确定”按钮
                    submit_btn = page.locator(
                        "button, div[role='button']"
                    ).filter(has_text=re.compile(r"提取|确定|提交|提取文件"))
                    if submit_btn.count() > 0:
                        submit_btn.first.click()

                    page.wait_for_timeout(4000)
            except Exception as e:
                print(f" └─ 填表交互过程异常/未找到输入框: {e}")

            # 如果抓取失败，保存现场截图供诊断
            if len(captured_files) == 0:
                debug_img = os.path.join(OUTPUT_DIR, f"debug_{share_id}.png")
                page.screenshot(path=debug_img)
                print(f"   [!] 节点抓取数量为 0，已保存页面截图至: {debug_img}")

            # 写入结果 JSON
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

            page.remove_listener("response", handle_response)

        browser.close()


if __name__ == "__main__":
    run()
