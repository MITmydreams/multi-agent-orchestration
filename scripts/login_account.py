#!/usr/bin/env python3
"""登录新 Telegram 账号，生成 Telethon session 文件"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
SESSIONS_DIR = "tdlib_sessions"

async def login():
    phone = input("手机号（带国际区号，如 10000000003）: ").strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    # 选代理
    print("\n选择代理:")
    print("  0 - 不用代理（直连）")
    print("  1 - #1 127.0.0.1")
    print("  2 - #2 127.0.0.1")
    print("  3 - #3 127.0.0.1")
    print("  4 - #4 127.0.0.1")
    print("  5 - #5 127.0.0.1")
    proxy_choice = input("代理编号 [0-5]: ").strip()

    proxy = None
    if proxy_choice != "0":
        import json
        proxies = json.load(open("config/proxies.json"))
        for p in proxies:
            if p["id"] == int(proxy_choice):
                proxy = ("socks5", p["host"], p["port"], True, p["username"], p["password"])
                break

    # 选 proxy bucket 目录
    bucket_id = proxy_choice if proxy_choice != "0" else "1"
    session_dir = os.path.join(SESSIONS_DIR, f"account_{bucket_id}")
    os.makedirs(session_dir, exist_ok=True)
    phone_clean = phone.lstrip("+")
    session_path = os.path.join(session_dir, phone_clean)

    print(f"\nSession 将保存到: {session_path}.session")
    print(f"代理: {proxy[1] if proxy else '直连'}")
    print()

    client = TelegramClient(session_path, API_ID, API_HASH, proxy=proxy)
    await client.connect()

    # 发送验证码
    print("发送验证码中...")
    await client.send_code_request(phone)

    code = input("输入收到的验证码: ").strip()

    try:
        await client.sign_in(phone, code)
        print("\n✅ 登录成功！")
    except SessionPasswordNeededError:
        password = input("需要 2FA 密码: ").strip()
        await client.sign_in(password=password)
        print("\n✅ 登录成功（2FA验证通过）！")

    me = await client.get_me()
    print(f"\n账号信息:")
    print(f"  ID:       {me.id}")
    print(f"  名字:     {me.first_name}")
    print(f"  用户名:   @{me.username or '未设置'}")
    print(f"  电话:     {phone}")
    print(f"  Session:  {session_path}.session")

    await client.disconnect()
    print("\n✅ Session 文件已保存，可以接入系统了。")

if __name__ == "__main__":
    asyncio.run(login())
