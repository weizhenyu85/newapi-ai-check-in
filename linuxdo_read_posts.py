#!/usr/bin/env python3
"""
使用 Camoufox 登录 Linux.do 并浏览帖子
"""

import asyncio
import hashlib
import json
import os
import sys
import random
from datetime import datetime
from dotenv import load_dotenv
from camoufox.async_api import AsyncCamoufox
from utils.browser_utils import take_screenshot, save_page_content_to_file
from utils.notify import notify

# 默认缓存目录，与 checkin.py 保持一致
DEFAULT_STORAGE_STATE_DIR = "storage-states"

# 帖子 ID 缓存目录
TOPIC_ID_CACHE_DIR = "linuxdo_reads"

# 阅读配置
MAX_SCROLL_TIME = 30  # 单篇帖子最大滚动时间（秒）
MAX_POSTS_COUNT = 5000  # 跳过评论数超过此值的帖子
BROWSE_TIME = 3600  # 连续浏览时间（秒），超过后休息
REST_TIME = 300  # 休息时间（秒）


class LinuxDoReadPosts:
    """Linux.do 帖子浏览类"""

    def __init__(
        self,
        username: str,
        password: str,
        storage_state_dir: str = DEFAULT_STORAGE_STATE_DIR,
        proxy: dict | None = None,
    ):
        """初始化

        Args:
            username: Linux.do 用户名
            password: Linux.do 密码
            storage_state_dir: 缓存目录，默认与 checkin.py 共享
            proxy: 代理配置，格式: {"server": "http://user:pass@proxy.com:8080"}
        """
        self.username = username
        self.password = password
        self.storage_state_dir = storage_state_dir
        self.proxy = proxy
        # 使用用户名哈希生成缓存文件名，与 checkin.py 保持一致
        self.username_hash = hashlib.sha256(username.encode("utf-8")).hexdigest()[:8]

        os.makedirs(self.storage_state_dir, exist_ok=True)
        os.makedirs(TOPIC_ID_CACHE_DIR, exist_ok=True)

    async def _is_logged_in(self, page) -> bool:
        """检查是否已登录

        通过访问 https://linux.do/ 后检查 URL 是否跳转到登录页面来判断

        Args:
            page: Camoufox 页面对象

        Returns:
            是否已登录
        """
        try:
            print(f"ℹ️ {self.username}: Checking login status...")
            await page.goto("https://linux.do/", wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)  # 等待可能的重定向

            current_url = page.url
            print(f"ℹ️ {self.username}: Current URL: {current_url}")

            # 如果跳转到登录页面，说明未登录
            if current_url.startswith("https://linux.do/login"):
                print(f"ℹ️ {self.username}: Redirected to login page, not logged in")
                return False

            print(f"✅ {self.username}: Already logged in")
            return True
        except Exception as e:
            print(f"⚠️ {self.username}: Error checking login status: {e}")
            return False

    async def _do_login(self, page) -> bool:
        """执行登录流程

        Args:
            page: Camoufox 页面对象

        Returns:
            登录是否成功
        """
        try:
            print(f"ℹ️ {self.username}: Starting login process...")

            # 如果当前不在登录页面，先导航到登录页面
            if not page.url.startswith("https://linux.do/login"):
                await page.goto("https://linux.do/login", wait_until="domcontentloaded")

            await page.wait_for_timeout(2000)

            # 填写用户名
            await page.fill("#login-account-name", self.username)
            await page.wait_for_timeout(2000)

            # 填写密码
            await page.fill("#login-account-password", self.password)
            await page.wait_for_timeout(2000)

            # 点击登录按钮
            await page.click("#login-button")
            await page.wait_for_timeout(10000)

            await save_page_content_to_file(page, "login_result", self.username)

            # 检查是否遇到 Cloudflare 验证
            current_url = page.url
            print(f"ℹ️ {self.username}: URL after login: {current_url}")

            if "linux.do/challenge" in current_url:
                print(
                    f"⚠️ {self.username}: Cloudflare challenge detected, "
                    "Camoufox should bypass it automatically. Waiting..."
                )
                # 等待 Cloudflare 验证完成，最多等待60秒
                try:
                    await page.wait_for_url("https://linux.do/", timeout=60000)
                    print(f"✅ {self.username}: Cloudflare challenge bypassed")
                except Exception:
                    print(f"⚠️ {self.username}: Cloudflare challenge timeout")

            # 再次检查是否登录成功
            current_url = page.url
            if current_url.startswith("https://linux.do/login"):
                print(f"❌ {self.username}: Login failed, still on login page")
                await take_screenshot(page, "login_failed", self.username)
                return False

            print(f"✅ {self.username}: Login successful")
            return True

        except Exception as e:
            print(f"❌ {self.username}: Error during login: {e}")
            await take_screenshot(page, "login_error", self.username)
            return False

    async def _fetch_topic_list(self, page, max_topics: int = 100) -> list[dict]:
        """通过 API 获取帖子列表

        先尝试获取未读帖子，如果没有则获取最新帖子。
        过滤掉评论数过多的帖子。

        Args:
            page: Camoufox 页面对象（用于携带 cookie 发请求）
            max_topics: 最大获取数量

        Returns:
            帖子列表 [{"id": int, "title": str, "posts_count": int}, ...]
        """
        topic_list = []

        for endpoint in ["unread", "latest"]:
            pg = 0
            retry = 0
            while len(topic_list) < max_topics and retry < 3:
                try:
                    url = f"https://linux.do/{endpoint}.json?no_definitions=true&page={pg}"
                    data = await page.evaluate(
                        f"""async () => {{
                            const resp = await fetch("{url}");
                            return await resp.json();
                        }}"""
                    )
                    topics = data.get("topic_list", {}).get("topics", [])
                    if not topics:
                        break
                    for t in topics:
                        if t.get("posts_count", 0) < MAX_POSTS_COUNT:
                            topic_list.append({
                                "id": t["id"],
                                "title": t.get("title", ""),
                                "posts_count": t.get("posts_count", 0),
                            })
                    pg += 1
                except Exception as e:
                    print(f"⚠️ {self.username}: Failed to fetch {endpoint} page {pg}: {e}")
                    retry += 1

            if topic_list:
                print(f"ℹ️ {self.username}: Got {len(topic_list)} topics from /{endpoint}")
                break
            else:
                print(f"ℹ️ {self.username}: No topics from /{endpoint}, trying next...")

        # 打乱顺序，避免多账号读同样的帖子
        random.shuffle(topic_list)
        return topic_list[:max_topics]

    async def _read_posts(self, page, max_posts: int) -> tuple[int, int]:
        """浏览帖子

        通过 API 获取帖子列表，逐个打开并滚动浏览。
        每篇帖子最多滚动 MAX_SCROLL_TIME 秒。
        连续浏览 BROWSE_TIME 秒后休息 REST_TIME 秒。

        Args:
            page: Camoufox 页面对象
            max_posts: 最大浏览帖子数

        Returns:
            (最后浏览的帖子ID, 实际阅读数量)
        """
        topic_list = await self._fetch_topic_list(page, max_posts)
        if not topic_list:
            print(f"⚠️ {self.username}: No topics available to read")
            return 0, 0

        read_count = 0
        last_topic_id = 0
        browse_start = asyncio.get_event_loop().time()

        for topic in topic_list:
            if read_count >= max_posts:
                break

            # 休息检查：连续浏览超过 BROWSE_TIME 秒后休息
            elapsed = asyncio.get_event_loop().time() - browse_start
            if elapsed >= BROWSE_TIME:
                print(f"ℹ️ {self.username}: Browsed {int(elapsed)}s, resting {REST_TIME}s...")
                await page.wait_for_timeout(REST_TIME * 1000)
                browse_start = asyncio.get_event_loop().time()

            topic_id = topic["id"]
            topic_url = f"https://linux.do/t/topic/{topic_id}"

            try:
                print(f"ℹ️ {self.username}: Opening topic {topic_id} ({topic.get('title', '')[:30]})...")
                await page.goto(topic_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(random.randint(2000, 3000))

                # 检查帖子是否有效
                timeline_element = await page.query_selector(".timeline-replies")
                if not timeline_element:
                    print(f"⚠️ {self.username}: Topic {topic_id} invalid, skipping")
                    continue

                inner_text = await timeline_element.inner_text()
                print(f"✅ {self.username}: Topic {topic_id} - Progress: {inner_text.strip()}")

                # 滚动浏览，限时 MAX_SCROLL_TIME 秒
                await self._scroll_to_read(page)

                read_count += 1
                last_topic_id = topic_id

                # 模拟阅读间隔
                await page.wait_for_timeout(random.randint(1000, 2000))

                if read_count % 20 == 0:
                    print(f"ℹ️ {self.username}: Progress: {read_count}/{max_posts}")

            except Exception as e:
                print(f"⚠️ {self.username}: Error reading topic {topic_id}: {e}")

        return last_topic_id, read_count

    async def _scroll_to_read(self, page) -> None:
        """自动滚动浏览帖子内容

        限时 MAX_SCROLL_TIME 秒，到底或超时就停。

        Args:
            page: Camoufox 页面对象
        """
        start_time = asyncio.get_event_loop().time()
        last_current_page = 0

        while True:
            # 超时检查
            if asyncio.get_event_loop().time() - start_time > MAX_SCROLL_TIME:
                print(f"ℹ️ {self.username}: Scroll timeout ({MAX_SCROLL_TIME}s), moving on")
                break

            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(random.randint(800, 2000))

            # 检查是否到底
            timeline_element = await page.query_selector(".timeline-replies")
            if not timeline_element:
                break

            inner_html = await timeline_element.inner_text()
            try:
                parts = inner_html.strip().split("/")
                if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
                    current_page = int(parts[0].strip())
                    total_pages = int(parts[1].strip())

                    if current_page >= total_pages:
                        break
                    if current_page == last_current_page:
                        break
                    last_current_page = current_page
                else:
                    break
            except (ValueError, IndexError):
                pass

    async def run(self, max_posts: int = 100) -> tuple[bool, dict]:
        """执行浏览帖子任务

        Args:
            max_posts: 最大浏览帖子数，默认 100

        Returns:
            (成功标志, 结果信息字典)
        """
        print(f"ℹ️ {self.username}: Starting Linux.do read posts task")

        # 缓存文件路径，与 checkin.py 保持一致
        cache_file_path = f"{self.storage_state_dir}/linuxdo_{self.username_hash}_storage_state.json"

        async with AsyncCamoufox(
            headless=False,
            humanize=True,
            locale="en-US",
        ) as browser:
            # 加载缓存的 storage state（如果存在）
            storage_state = cache_file_path if os.path.exists(cache_file_path) else None
            if storage_state:
                print(f"ℹ️ {self.username}: Restoring storage state from cache")
            else:
                print(f"ℹ️ {self.username}: No cache file found, starting fresh")

            # 配置代理
            if self.proxy:
                print(f"ℹ️ {self.username}: Using proxy: {self.proxy.get('server', 'unknown')}")
                context = await browser.new_context(storage_state=storage_state, proxy=self.proxy)
            else:
                print(f"ℹ️ {self.username}: No proxy configured, using direct connection")
                context = await browser.new_context(storage_state=storage_state)
            page = await context.new_page()

            try:
                # 检查是否已登录
                is_logged_in = await self._is_logged_in(page)

                # 如果未登录，执行登录流程
                if not is_logged_in:
                    login_success = await self._do_login(page)
                    if not login_success:
                        return False, {"error": "Login failed"}

                    # 保存会话状态
                    await context.storage_state(path=cache_file_path)
                    print(f"✅ {self.username}: Storage state saved to cache file")

                # 浏览帖子
                print(f"ℹ️ {self.username}: Starting to read posts...")
                last_topic_id, read_count = await self._read_posts(page, max_posts)

                print(f"✅ {self.username}: Successfully read {read_count} posts")
                return True, {
                    "read_count": read_count,
                    "last_topic_id": last_topic_id,
                }

            except Exception as e:
                print(f"❌ {self.username}: Error occurred: {e}")
                await take_screenshot(page, "error", self.username)
                return False, {"error": str(e)}
            finally:
                await page.close()
                await context.close()


def load_linuxdo_accounts() -> list[dict]:
    """从 ACCOUNTS 环境变量加载 Linux.do 账号

    Returns:
        包含 linux.do 账号信息的列表，每个元素为:
        {"username": str, "password": str}
    """
    accounts_str = os.getenv("ACCOUNTS")
    if not accounts_str:
        print("❌ ACCOUNTS environment variable not found")
        return []

    try:
        accounts_data = json.loads(accounts_str)

        if not isinstance(accounts_data, list):
            print("❌ ACCOUNTS must be a JSON array")
            return []

        linuxdo_accounts = []
        seen_usernames = set()

        for i, account in enumerate(accounts_data):
            if not isinstance(account, dict):
                print(f"⚠️ ACCOUNTS[{i}] must be a dictionary, skipping")
                continue

            username = account.get("username")
            password = account.get("password")

            if not username or not password:
                print(f"⚠️ ACCOUNTS[{i}] missing username or password, skipping")
                continue

            # 根据 username 去重
            if username in seen_usernames:
                print(f"ℹ️ Skipping duplicate account: {username}")
                continue

            seen_usernames.add(username)
            linuxdo_accounts.append(
                {
                    "username": username,
                    "password": password,
                }
            )

        return linuxdo_accounts

    except json.JSONDecodeError as e:
        print(f"❌ Failed to parse ACCOUNTS: {e}")
        return []
    except Exception as e:
        print(f"❌ Error loading ACCOUNTS: {e}")
        return []


async def main():
    """主函数"""
    load_dotenv(override=True)

    print("🚀 Linux.do read posts script started")
    print(f'🕒 Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    # 加载配置了 linux.do 的账号
    accounts = load_linuxdo_accounts()

    if not accounts:
        print("❌ No accounts with linux.do configuration found")
        return

    print(f"ℹ️ Found {len(accounts)} account(s) with linux.do configuration")

    # 如果指定了 ACCOUNT_INDEX，只处理对应索引的账号（用于 matrix 并行）
    account_index = os.getenv("ACCOUNT_INDEX")
    if account_index is not None:
        idx = int(account_index)
        if 0 <= idx < len(accounts):
            print(f"ℹ️ ACCOUNT_INDEX={idx}, only processing account: {accounts[idx]['username']}")
            accounts = [accounts[idx]]
        else:
            print(f"ℹ️ ACCOUNT_INDEX={idx} out of range (total: {len(accounts)}), skipping")
            return

    # 加载全局代理配置
    global_proxy = None
    proxy_str = os.getenv("PROXY")
    if proxy_str:
        try:
            # 尝试解析为 JSON
            global_proxy = json.loads(proxy_str)
            print(f"⚙️ Global proxy loaded from PROXY environment variable (dict format)")
        except json.JSONDecodeError:
            # 如果不是 JSON，则视为字符串
            global_proxy = {"server": proxy_str}
            print(f"⚙️ Global proxy loaded from PROXY environment variable: {proxy_str}")
    else:
        print(f"ℹ️ No global proxy configured")

    # 收集结果用于通知
    results = []

    # 为每个账号执行任务
    for account in accounts:
        print(f"\n{'='*50}")
        print(f"📌 Processing: {account['username']}")
        print(f"{'='*50}")

        try:
            # 获取账号级代理或使用全局代理
            account_proxy = account.get("proxy", global_proxy)

            reader = LinuxDoReadPosts(
                username=account["username"],
                password=account["password"],
                proxy=account_proxy,
            )

            start_time = datetime.now()
            success, result = await reader.run(random.randint(200, 300))
            end_time = datetime.now()
            duration = end_time - start_time

            # 格式化时长为 HH:MM:SS
            total_seconds = int(duration.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

            print(f"Result: success={success}, result={result}, duration={duration_str}")

            # 记录结果
            results.append(
                {
                    "username": account["username"],
                    "success": success,
                    "result": result,
                    "duration": duration_str,
                }
            )
        except Exception as e:
            print(f"❌ {account['username']}: Exception occurred: {e}")
            results.append(
                {
                    "username": account["username"],
                    "success": False,
                    "result": {"error": str(e)},
                    "duration": "00:00:00",
                }
            )

    # 发送通知
    if results:
        notification_lines = [
            f'🕒 执行时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            "",
        ]

        total_read_count = 0
        has_failure = False
        for r in results:
            username = r["username"]
            duration = r["duration"]
            if r["success"]:
                read_count = r["result"].get("read_count", 0)
                total_read_count += read_count
                last_topic_id = r["result"].get("last_topic_id", "unknown")
                topic_url = f"https://linux.do/t/topic/{last_topic_id}"
                notification_lines.append(
                    f"✅ {username}: 已阅读 {read_count} 篇帖子 ({duration})\n" f"   最后帖子: {topic_url}"
                )
            else:
                has_failure = True
                error = r["result"].get("error", "未知错误")
                # 检查是否是代理相关错误
                if "proxy" in error.lower() or "connection" in error.lower() or "timeout" in error.lower():
                    notification_lines.append(f"❌ {username}: 代理连接失败 - {error} ({duration})")
                else:
                    notification_lines.append(f"❌ {username}: {error} ({duration})")

        # 添加阅读总数
        notification_lines.append("")
        notification_lines.append(f"📊 总计阅读: {total_read_count} 篇帖子")

        # 添加失败提示
        if has_failure:
            notification_lines.append("")
            notification_lines.append("⚠️ 部分账号执行失败，请检查代理配置或网络连接")

        notify_content = "\n".join(notification_lines)
        notify.push_message("Linux.do 阅读帖子", notify_content, msg_type="text")


def run_main():
    """运行主函数的包装函数"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⚠️ Program interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error occurred during program execution: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_main()
