"""
MT真人 即時房間數據模組
透過 Playwright 開啟 MT 頁面，攔截 WebSocket 取得所有桌台資訊。
提供 get_mt_rooms() 供 LINE Bot 呼叫。
"""

import asyncio
import json
import os
import time
import logging

logger = logging.getLogger(__name__)

# ── 快取 ──
_cached_rooms = None
_rooms_expiry = 0
ROOMS_TTL = 120  # 2 分鐘快取（房間數據變化較快）

# ── 桌台分類 ──
HALL_CHINESE = "中文廳"   # hall=1
HALL_ASIA = "亞洲廳"      # hall=2 / 3A 系列
HALL_INTL = "國際廳"      # hall=3
HALL_DT = "龍虎"          # DTG 開頭
HALL_NU = "牛牛"          # NUG 開頭
HALL_SB = "骰寶"          # SBG 開頭

TABLE_CATEGORY = {
    "BAG01": HALL_CHINESE, "BAG02": HALL_CHINESE, "BAG03": HALL_CHINESE,
    "BAG05": HALL_CHINESE, "BAG06": HALL_CHINESE, "BAG07": HALL_CHINESE,
    "BAG08": HALL_CHINESE, "BAG09": HALL_CHINESE, "BAG10": HALL_CHINESE,
    "BAG11": HALL_CHINESE, "BAG12": HALL_CHINESE, "BAG13": HALL_CHINESE,
    "BAG03A": HALL_ASIA,
    "DTG01": HALL_DT, "DTG02": HALL_DT, "DTG03": HALL_DT,
    "NUG01": HALL_NU,
    "SBG01": HALL_SB,
}


def _parse_trend(trend):
    """從 trend 物件解析莊閒和統計"""
    if not trend:
        return {}
    return {
        "current_round": trend.get("current_round", "?"),
        "current_shoe": trend.get("current_shoe", "?"),
        "total_round": trend.get("total_round", "0"),
        "banker_wins": trend.get("total_round_banker", "0"),
        "player_wins": trend.get("total_round_player", "0"),
        "tie_count": trend.get("total_round_tie", "0"),
        "banker_pair": trend.get("total_round_banker_pair", "0"),
        "player_pair": trend.get("total_round_player_pair", "0"),
    }


def _parse_table(t):
    """解析單個桌台資料"""
    table_id = t.get("table_id", "")
    dealer = t.get("dealer", {})
    trend = _parse_trend(t.get("trend"))
    category = TABLE_CATEGORY.get(table_id, "其他")

    return {
        "table_id": table_id,
        "table_name": t.get("table_name", ""),
        "table_type": t.get("table_type", ""),
        "category": category,
        "hall": t.get("hall", 0),
        "room_id": t.get("room_id", ""),
        "dealer_name": dealer.get("username", "未知"),
        "dealer_avatar": dealer.get("avatar_url", ""),
        "dealer_nation": dealer.get("nation", ""),
        "total_players": int(t.get("totalplayers", 0)),
        "game_sn": t.get("game_sn", ""),
        "state": t.get("state", 0),         # 0=正常, 其他=維護?
        "order_state": t.get("orderState", 0),  # 1=等待, 2=下注中
        **trend,
    }


async def _fetch_rooms_playwright():
    """用 Playwright 開啟 MT 頁面，攔截 WS 取得桌台列表"""
    from playwright.async_api import async_playwright
    from mt_token import get_mt_token

    token = await get_mt_token()
    mt_url = f"https://gsa.ofalive99.net/?token={token}&lang=zhtw"
    logger.info("開啟 MT 頁面取得房間數據...")

    tables_data = []
    tables_future = asyncio.get_event_loop().create_future()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        def on_ws(ws):
            if "game/ws" not in ws.url:
                return

            def on_frame(payload):
                if tables_future.done():
                    return
                try:
                    data = json.loads(payload)
                    action = data.get("action", "")
                    # 桌台列表回應
                    if isinstance(action, str) and "tables" in action and "err" in data:
                        msg = data.get("msg", {})
                        tables = msg.get("tables", [])
                        # tablesv2 回應的 tables 是 dict 包含 sort + tables
                        if isinstance(tables, dict):
                            tables = tables.get("tables", [])
                        if tables and not tables_future.done():
                            tables_future.set_result(tables)
                except Exception:
                    pass

            ws.on("framereceived", on_frame)

        page.on("websocket", on_ws)

        try:
            await page.goto(mt_url, wait_until="networkidle", timeout=60000)
            # 等待桌台數據（最多 20 秒）
            tables_data = await asyncio.wait_for(tables_future, timeout=20)
            logger.info("取得 %d 張桌台數據", len(tables_data))
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("等待桌台數據超時")
        except Exception as e:
            logger.error("取得房間數據失敗: %s", e)
        finally:
            await browser.close()

    # 解析桌台
    rooms = []
    for t in tables_data:
        try:
            rooms.append(_parse_table(t))
        except Exception as e:
            logger.warning("解析桌台失敗: %s", e)

    return rooms


async def get_mt_rooms(force_refresh=False):
    """取得 MT 房間列表（帶快取）"""
    global _cached_rooms, _rooms_expiry

    if not force_refresh and _cached_rooms and time.time() < _rooms_expiry:
        logger.info("使用快取房間數據 (%d 張桌台)", len(_cached_rooms))
        return _cached_rooms

    logger.info("重新取得 MT 房間數據...")
    rooms = await _fetch_rooms_playwright()
    if rooms:
        _cached_rooms = rooms
        _rooms_expiry = time.time() + ROOMS_TTL
    return rooms or _cached_rooms or []


def get_mt_rooms_sync(force_refresh=False):
    """同步版本"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(get_mt_rooms(force_refresh))
    finally:
        loop.close()


def get_rooms_by_category(rooms):
    """按類別分組"""
    groups = {}
    for r in rooms:
        cat = r["category"]
        groups.setdefault(cat, []).append(r)
    return groups


def format_room_summary(room):
    """格式化單個房間摘要（用於 LINE Bot 顯示）"""
    tid = room["table_id"]
    dealer = room["dealer_name"]
    players = room["total_players"]
    shoe = room.get("current_shoe", "?")
    rnd = room.get("current_round", "?")
    b_wins = room.get("banker_wins", "0")
    p_wins = room.get("player_wins", "0")
    t_count = room.get("tie_count", "0")

    return (
        f"🎯 {tid} ({room['table_name']})\n"
        f"👩 荷官: {dealer}\n"
        f"👥 在線: {players}\n"
        f"📊 靴{shoe} 局{rnd} | 莊{b_wins} 閒{p_wins} 和{t_count}"
    )


# ── 測試 ──
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

    rooms = get_mt_rooms_sync()
    print(f"\n{'='*50}")
    print(f"共取得 {len(rooms)} 張桌台")
    print(f"{'='*50}")

    groups = get_rooms_by_category(rooms)
    for cat, cat_rooms in groups.items():
        print(f"\n【{cat}】({len(cat_rooms)} 桌)")
        for r in cat_rooms:
            print(format_room_summary(r))
            print()
