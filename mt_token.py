"""
MT真人 Token 自動取得模組
純 HTTP API 取 token（不需要 Playwright / 瀏覽器）
"""

import asyncio
import json
import os
import time
import re
import logging
import requests

logger = logging.getLogger(__name__)

# --- 快取 ---
_cached_token = None
_token_expiry = 0
TOKEN_TTL = 600  # 10 分鐘快取

# --- Publisher ID ---
MT_PUBLISHER_ID = 19  # MT_REALITY


async def _fetch_mt_token_http():
    """純 HTTP API 取得 MT Token
    流程: getMasterAgentByWebsite → loginEzAction → enterGamePublisherLobby
    """
    api_base = "https://api.seogrwin1688.com"
    platform_url = os.getenv("GR_PLATFORM_URL", "https://seofufan.seogrwin1688.com/").rstrip("/")
    username = os.getenv("GR_USERNAME", "")
    password = os.getenv("GR_PASSWORD", "")

    if not username or not password:
        raise RuntimeError("GR_USERNAME / GR_PASSWORD 未設定")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": platform_url,
        "Referer": f"{platform_url}/",
    })

    # Step 1: Get masterAgent
    logger.info("[HTTP] Step1: getMasterAgentByWebsite...")
    r1 = session.post(f"{api_base}/ClientGateway/api/getMasterAgentByWebsite",
                      json={"website": "seogrwin1688.com"}, timeout=15)
    r1.raise_for_status()
    d1 = r1.json()
    if not d1.get("data"):
        raise RuntimeError(f"getMasterAgent 失敗: {d1}")
    master_agent = d1["data"]["account"]
    logger.info("[HTTP] masterAgent=%s", master_agent)

    # Step 2: Login
    logger.info("[HTTP] Step2: loginEzAction...")
    r2 = session.post(f"{api_base}/ClientGateway/api/loginEzAction",
                      json={"account": username.lower(), "password": password,
                            "masterAgent": master_agent}, timeout=15)
    r2.raise_for_status()
    d2 = r2.json()
    if not d2.get("data"):
        raise RuntimeError(f"login 失敗: {d2}")
    access_token = d2["data"]["Authorization"]
    member_id = d2["data"]["memberID"]
    logger.info("[HTTP] 登入成功, memberID=%s", member_id)

    # Step 3: enterGamePublisherLobby (MT_REALITY = 19)
    logger.info("[HTTP] Step3: enterGamePublisherLobby (MT=%d)...", MT_PUBLISHER_ID)
    r3 = session.post(f"{api_base}/ClientGateway/api/action/enterGamePublisherLobby",
                      json={
                          "Authorization": access_token,
                          "query": json.dumps({
                              "publisherID": MT_PUBLISHER_ID,
                              "memberID": member_id,
                          })
                      }, timeout=15)
    r3.raise_for_status()
    d3 = r3.json()
    if not d3.get("data"):
        raise RuntimeError(f"enterGamePublisherLobby 失敗: {d3}")

    lobby_url = d3["data"].get("lobbyURL", "")
    m = re.search(r'token=([a-fA-F0-9]{32})', lobby_url)
    if not m:
        raise RuntimeError(f"回應中找不到 token, lobbyURL={lobby_url}")

    token = m.group(1)
    logger.info("[HTTP] MT Token 取得成功: %s...", token[:8])
    return token


async def get_mt_token(force_refresh=False):
    """取得 MT Token（帶快取）"""
    global _cached_token, _token_expiry

    if not force_refresh and _cached_token and time.time() < _token_expiry:
        logger.info("使用快取 MT Token: %s...", _cached_token[:8])
        return _cached_token

    logger.info("取得 MT Token...")
    token = await _fetch_mt_token_http()
    _cached_token = token
    _token_expiry = time.time() + TOKEN_TTL
    return token


def get_mt_token_sync(force_refresh=False):
    """同步版本，供非 async 環境呼叫"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(get_mt_token(force_refresh))
    finally:
        loop.close()


# --- 測試用 ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()
    except FileNotFoundError:
        pass

    token = get_mt_token_sync()
    print(f"MT Token: {token}")
