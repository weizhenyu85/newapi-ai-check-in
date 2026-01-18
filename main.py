#!/usr/bin/env python3
"""
自动签到脚本
"""

import asyncio
import hashlib
import json
import sys
from datetime import datetime
from dotenv import load_dotenv
from utils.config import AppConfig
from utils.notify import notify
from utils.balance_hash import load_balance_hash, save_balance_hash
from checkin import CheckIn

load_dotenv(override=True)

BALANCE_HASH_FILE = "balance_hash.txt"


def generate_balance_hash(balances: dict) -> str:
    """生成余额数据的hash"""
    # 将包含 quota 和 used 的结构转换为 {account_name: [quota]} 格式用于 hash 计算
    simple_balances = {}
    if balances:
        for account_key, account_balances in balances.items():
            quota_list = []
            for _, balance_info in account_balances.items():
                quota_list.append(balance_info["quota"])
            simple_balances[account_key] = quota_list

    balance_json = json.dumps(simple_balances, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(balance_json.encode("utf-8")).hexdigest()[:16]


async def main():
    """运行签到流程

    Returns:
            退出码: 0 表示至少有一个账号成功, 1 表示全部失败
    """

    print("🚀 newapi.ai multi-account auto check-in script started (using Camoufox)")
    print(f'🕒 Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    app_config = AppConfig.load_from_env()
    print(f"⚙️ Loaded {len(app_config.providers)} provider(s)")

    # 检查账号配置
    if not app_config.accounts:
        print("❌ Unable to load account configuration, program exits")
        return 1
    
    print(f"⚙️ Found {len(app_config.accounts)} account(s)")

    # 加载余额hash
    last_balance_hash = load_balance_hash(BALANCE_HASH_FILE)

    # 为每个账号执行签到
    success_count = 0
    total_count = 0
    notification_content = []
    current_balances = {}
    need_notify = False  # 是否需要发送通知

    for i, account_config in enumerate(app_config.accounts):
        account_key = f"account_{i + 1}"
        account_name = account_config.get_display_name(i)
        if len(notification_content) > 0:
            notification_content.append("\n-------------------------------")

        try:
            provider_config = app_config.get_provider(account_config.provider)
            if not provider_config:
                print(f"❌ {account_name}: Provider '{account_config.provider}' configuration not found")
                need_notify = True
                notification_content.append(
                    f"[FAIL] {account_name}: Provider '{account_config.provider}' configuration not found"
                )
                continue

            print(f"🌀 Processing {account_name} using provider '{account_config.provider}'")
            checkin = CheckIn(account_name, account_config, provider_config, global_proxy=app_config.global_proxy)
            results = await checkin.execute()

            total_count += len(results)

            # 处理多个认证方式的结果
            account_success = False
            successful_methods = []
            failed_methods = []

            this_account_balances = {}
            # 构建详细的结果报告
            account_result = f"📣 {account_name} 摘要:\n"
            for auth_method, success, user_info in results:
                status = "✅ 成功" if success else "❌ 失败"
                account_result += f"  {status} - {auth_method} 认证\n"

                if success and user_info and user_info.get("success"):
                    account_success = True
                    success_count += 1
                    successful_methods.append(auth_method)
                    account_result += f"    💰 {user_info['display']}\n"
                    # 记录余额信息
                    current_quota = user_info["quota"]
                    current_used = user_info["used_quota"]
                    current_bonus = user_info["bonus_quota"]
                    this_account_balances[f"{auth_method}"] = {
                        "quota": current_quota,
                        "used": current_used,
                        "bonus": current_bonus,
                    }
                else:
                    failed_methods.append(auth_method)
                    error_msg = user_info.get("error", "未知错误") if user_info else "未知错误"
                    # 检查是否是代理相关错误
                    if "proxy" in str(error_msg).lower() or "connection" in str(error_msg).lower() or "timeout" in str(error_msg).lower():
                        account_result += f"    🔺 代理连接失败: {str(error_msg)}\n"
                    else:
                        account_result += f"    🔺 {str(error_msg)}\n"

            if account_success:
                current_balances[account_key] = this_account_balances

            # 如果所有认证方式都失败，需要通知
            if not account_success and results:
                need_notify = True
                print(f"🔔 {account_name} 所有认证方式失败，将发送通知")

            # 如果有失败的认证方式，也通知
            if failed_methods and successful_methods:
                need_notify = True
                print(f"🔔 {account_name} 部分认证方式失败，将发送通知")

            # 添加统计信息
            success_count_methods = len(successful_methods)
            failed_count_methods = len(failed_methods)

            account_result += f"\n📊 统计: {success_count_methods}/{len(results)} 种方式成功"
            if failed_count_methods > 0:
                account_result += f" ({failed_count_methods} 种失败)"

            notification_content.append(account_result)

        except Exception as e:
            print(f"❌ {account_name} 处理异常: {e}")
            need_notify = True  # 异常也需要通知
            notification_content.append(f"❌ {account_name} 异常: {str(e)[:100]}...")

    # 检查余额变化
    current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
    print(f"\n\nℹ️ 当前余额哈希: {current_balance_hash}, 上次余额哈希: {last_balance_hash}")
    if current_balance_hash:
        if last_balance_hash is None:
            # 首次运行
            need_notify = True
            print("🔔 检测到首次运行，将发送当前余额通知")
        elif current_balance_hash != last_balance_hash:
            # 余额有变化
            need_notify = True
            print("🔔 检测到余额变化，将发送通知")
        else:
            print("ℹ️ 未检测到余额变化")

    # 保存当前余额hash
    if current_balance_hash:
        save_balance_hash(BALANCE_HASH_FILE, current_balance_hash)

    if need_notify and notification_content:
        # 构建通知内容
        summary = [
            "-------------------------------",
            "📢 签到结果统计:",
            f"🔵 成功: {success_count}/{total_count}",
            f"🔴 失败: {total_count - success_count}/{total_count}",
        ]

        if success_count == total_count:
            summary.append("✅ 所有账号签到成功!")
        elif success_count > 0:
            summary.append("⚠️ 部分账号签到成功")
        else:
            summary.append("❌ 所有账号签到失败")

        time_info = f'🕓 执行时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'

        notify_content = "\n\n".join([time_info, "\n".join(notification_content), "\n".join(summary)])

        print(notify_content)
        notify.push_message("签到提醒", notify_content, msg_type="text")
        print("🔔 已发送通知（失败或余额变化）")
    else:
        print("ℹ️ 所有账号成功且余额无变化，跳过通知")

    # 设置退出码
    sys.exit(0 if success_count > 0 else 1)


def run_main():
    """运行主函数的包装函数"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⚠️ 程序被用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 程序执行过程中发生错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_main()
