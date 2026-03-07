"""
MT真人 即時牌路追蹤模組
用 curl_cffi 純 WebSocket 連接 MT 遊戲伺服器，
自動累積每桌牌路歷史，供 LINE Bot 直接查詢。
（不需要 Playwright / 瀏覽器）
"""

import asyncio
import json
import time
import threading
import logging
import traceback as _tb

logger = logging.getLogger(__name__)

# ── 全域牌路存儲 ──
_table_history = {}   # { "BAG01": ["莊","閒","莊",...], ... }
_table_info = {}      # { "BAG01": {"shoe": 10699, "round": 5, ...}, ... }
_lock = threading.Lock()
_listener_running = False
_listener_thread = None

WINNER_MAP = {1: "莊", 2: "閒", 3: "和"}
MAX_HISTORY = 200     # 每桌最多保留 200 局


def get_table_history(table_id):
    """取得指定桌台的牌路歷史（執行緒安全）"""
    with _lock:
        return list(_table_history.get(table_id, []))


def get_table_info(table_id):
    """取得指定桌台的即時資訊"""
    with _lock:
        return dict(_table_info.get(table_id, {}))


def get_all_tables():
    """取得所有有紀錄的桌台 ID"""
    with _lock:
        return list(_table_history.keys())


def get_all_history():
    """取得所有桌台的牌路（深拷貝）"""
    with _lock:
        return {k: list(v) for k, v in _table_history.items()}


def _on_show_win(body):
    """處理 show_win 事件"""
    table_id = body.get("table_id", "")
    winner = body.get("winner")
    shoe = body.get("shoe")
    rnd = body.get("round")

    if not table_id or winner not in WINNER_MAP:
        return

    result = WINNER_MAP[winner]

    with _lock:
        # 檢查是否換靴（shoe 變了 → 清空歷史）
        info = _table_info.get(table_id, {})
        old_shoe = info.get("shoe")
        if old_shoe is not None and shoe is not None and str(shoe) != str(old_shoe):
            logger.info("[%s] 換靴 %s → %s，清空歷史", table_id, old_shoe, shoe)
            _table_history[table_id] = []

        # 累積牌路
        hist = _table_history.setdefault(table_id, [])
        hist.append(result)
        if len(hist) > MAX_HISTORY:
            _table_history[table_id] = hist[-MAX_HISTORY:]

        # 更新桌台資訊
        _table_info[table_id] = {
            "shoe": shoe,
            "round": rnd,
            "last_result": result,
            "last_update": time.time(),
        }

    logger.info("[%s] 靴%s 局%s → %s (歷史 %d 局)",
                table_id, shoe, rnd, result, len(hist))


def _on_tables_response(tables_list):
    """從 tables 回應初始化各桌的牌路統計及房間資訊"""
    with _lock:
        for t in tables_list:
            tid = t.get("table_id", "")
            trend = t.get("trend", {})
            if not tid:
                continue
            shoe = trend.get("current_shoe") if trend else None
            rnd = trend.get("current_round", "0") if trend else "0"

            # 從 bead_plate2 解碼歷史
            if trend:
                bead = trend.get("bead_plate2", "")
                if bead and tid not in _table_history:
                    history = _decode_bead_plate(bead)
                    if history:
                        _table_history[tid] = history
                        logger.info("[%s] 從 bead_plate2 初始化 %d 局歷史", tid, len(history))

            # 儲存完整桌台資訊（供房間選單用）
            dealer = t.get("dealer", {})
            _table_info[tid] = {
                "shoe": shoe,
                "round": rnd,
                "last_update": time.time(),
                "table_name": t.get("table_name", ""),
                "dealer_name": dealer.get("username", "未知") if isinstance(dealer, dict) else "未知",
                "total_players": int(t.get("totalplayers", 0)),
                "banker_wins": trend.get("total_round_banker", "0") if trend else "0",
                "player_wins": trend.get("total_round_player", "0") if trend else "0",
                "tie_count": trend.get("total_round_tie", "0") if trend else "0",
                "hall": t.get("hall", 0),
                "state": t.get("state", 0),
            }
        logger.info("[tables] 已更新 %d 張桌台資訊", len(tables_list))


def _decode_bead_plate(bead_str):
    """
    解碼 bead_plate2 字串為牌路歷史
    格式：每 2 字元一組，代表一局
    第一個字元（十位）：0=一般, 1=閒對, 2=莊對, 3=雙對
    第二個字元（個位）：1=閒, 2=莊, 3=和
    用 # 分隔 bead plate 的不同行（每行6局）
    
    例如："020202010202#020102020101" 
    → 02=莊, 02=莊, 02=莊, 01=閒, 02=莊, 02=莊 # 02=莊, 01=閒, ...
    """
    history = []
    # # 分隔行，但數據是按行排列的，需要讀完整行
    flat = bead_str.replace("#", "")
    for i in range(0, len(flat) - 1, 2):
        code = flat[i:i+2]
        winner_digit = code[1]  # 個位數是結果
        if winner_digit == "1":
            history.append("閒")
        elif winner_digit == "2":
            history.append("莊")
        elif winner_digit == "3":
            history.append("和")
        # 其他忽略
    return history


def _log(msg):
    """Listener 專用日誌（確保在 gunicorn 背景執行緒中也能輸出）"""
    print(f"[Listener] {msg}", flush=True)


async def _run_listener():
    """主監聽迴圈：HTTP 取 token → curl_cffi 純 WS 連接 MT 伺服器"""
    global _listener_running
    _log("_run_listener 開始執行")

    try:
        from curl_cffi.requests import AsyncSession
        _log("curl_cffi import OK")
        from mt_token import get_mt_token
        _log("mt_token import OK")
    except Exception as e:
        _log(f"import 失敗: {e}\n{_tb.format_exc()}")
        return

    WS_URL = "wss://a1.ofalive99.net/game/ws"
    reconnect_delay = 10

    while _listener_running:
        try:
            # ---- Step1: HTTP API 取 token ----
            _log("Step1: 取得 MT token (HTTP)...")
            token = await get_mt_token()
            if not token:
                _log("token 為空，重試")
                await asyncio.sleep(reconnect_delay)
                continue
            _log(f"Step1 OK: token={token[:16]}...")

            # ---- Step2: curl_cffi WS 連線（模擬 Chrome TLS）----
            _log(f"Step2: WS 連線 {WS_URL}...")

            async with AsyncSession(impersonate="chrome") as session:
                ws = await session.ws_connect(
                    WS_URL,
                    headers={
                        "Origin": "https://gs1.ofalive99.net",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/125.0.0.0 Safari/537.36",
                    }
                )
                _log("Step2 OK: WS 已連線")

                # ---- Step3: 認證 ----
                auth_msg = {
                    "method": "POST",
                    "action": {"name": "/api/v1/authenticate"},
                    "body": {"type": 3, "token": token}
                }
                await ws.send(json.dumps(auth_msg))
                _log("Step3: 已發送 authenticate")

                # 等待認證回應（最多讀 10 條訊息）
                # 伺服器可能不回 authenticate action，而是直接推送 member/statistics
                auth_ok = False
                for _i in range(10):
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        if isinstance(raw, tuple): raw = raw[0]
                        if isinstance(raw, bytes): raw = raw.decode("utf-8", errors="replace")
                        data = json.loads(raw)
                        action = data.get("action", "")
                        aname = action.get("name", "") if isinstance(action, dict) else str(action)
                        err = data.get("err", "")
                        _log(f"AUTH_WAIT#{_i}: action={aname[:80]} err={err} msg_preview={str(data.get('msg',''))[:200]}")
                        if "authenticate" in aname:
                            if err:
                                _log(f"認證失敗: {err}，重新取 token...")
                                from mt_token import get_mt_token as _refresh_token
                                token = await _refresh_token(force_refresh=True)
                                break
                            auth_ok = True
                            _log("✅ 認證成功 (explicit)")
                            break
                        elif "member" in aname:
                            # member/statistics = 伺服器認證後推送的用戶資料
                            auth_ok = True
                            _log("✅ 認證成功 (implicit: member/statistics received)")
                            break
                    except asyncio.TimeoutError:
                        _log("等待認證回應超時")
                        break
                    except Exception as ex:
                        _log(f"AUTH_WAIT#{_i} 異常: {ex}")
                        continue

                if not auth_ok:
                    _log("認證未確認，仍嘗試發送 tables 請求...")

                # 消化所有待處理的 member/statistics 訊息
                drained = 0
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2)
                        if isinstance(raw, tuple): raw = raw[0]
                        if isinstance(raw, bytes): raw = raw.decode("utf-8", errors="replace")
                        drained += 1
                    except asyncio.TimeoutError:
                        break
                if drained:
                    _log(f"已消化 {drained} 條待處理訊息")

                # 延遲確保伺服器準備好
                await asyncio.sleep(2)

                # ---- Step4: 請求桌台資料（所有廳）----
                for room_id in [1, 2, 3]:
                    tables_msg = {
                        "method": "GET",
                        "action": {
                            "name": "/api/v1/gametype/*/game/*/room/*/tables",
                            "data": {"gametype_id": 3, "game_id": 1, "room_id": room_id}
                        }
                    }
                    await ws.send(json.dumps(tables_msg))
                    await asyncio.sleep(0.5)
                _log("Step4: 已發送 tables 請求 (room 1,2,3)")

                # 也請求龍虎
                dt_msg = {
                    "method": "GET",
                    "action": {
                        "name": "/api/v1/gametype/*/game/*/room/*/tables",
                        "data": {"gametype_id": 3, "game_id": 2, "room_id": 1}
                    }
                }
                await ws.send(json.dumps(dt_msg))
                _log("Step4: 已發送龍虎 tables 請求")

                _log("✅ WS 已連線，開始監聽即時開牌")
                reconnect_delay = 10
                last_ping = time.time()
                msg_count = 0
                seen_actions = set()

                # ---- Step5: 持續監聽 ----
                while _listener_running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    except asyncio.TimeoutError:
                        # 60 秒沒訊息，發 ping 保活
                        if time.time() - last_ping > 55:
                            try:
                                await ws.send(json.dumps({"action": "/ping"}))
                                last_ping = time.time()
                                _log(f"sent ping (total msgs={msg_count}, actions={seen_actions})")
                            except Exception:
                                _log("ping 失敗，重新連線")
                                break
                        continue

                    # curl_cffi 可能返回 tuple (msg, type)
                    if isinstance(raw, tuple):
                        msg_text = raw[0]
                    else:
                        msg_text = raw
                    if isinstance(msg_text, bytes):
                        msg_text = msg_text.decode("utf-8", errors="replace")

                    msg_count += 1

                    try:
                        data = json.loads(msg_text)
                    except (json.JSONDecodeError, TypeError):
                        if msg_count <= 5:
                            _log(f"MSG#{msg_count} JSON解析失敗: {str(msg_text)[:200]}")
                        continue

                    action = data.get("action", "")
                    if isinstance(action, dict):
                        aname = action.get("name", "")
                    else:
                        aname = str(action)

                    # 記錄新出現的 action 類型
                    if aname not in seen_actions:
                        seen_actions.add(aname)
                        _log(f"NEW_ACTION: {aname} keys={list(data.keys())} msg_preview={str(data.get('msg',''))[:300]}")

                    if isinstance(action, dict):
                        name = action.get("name", "")
                        # show_win 事件
                        if "show_win" in name:
                            _on_show_win(data.get("body", {}))
                        # tables 回應
                        elif "tables" in name:
                            msg_data = data.get("msg", {})
                            tables = msg_data.get("tables", [])
                            if isinstance(tables, dict):
                                tables = tables.get("tables", [])
                            if tables:
                                _on_tables_response(tables)
                            else:
                                _log(f"tables 回應但無桌台: keys={list(msg_data.keys())}")
                    elif isinstance(action, str):
                        if "tables" in action:
                            msg_data = data.get("msg", {})
                            tables = msg_data.get("tables", [])
                            if isinstance(tables, dict):
                                tables = tables.get("tables", [])
                            if tables:
                                _on_tables_response(tables)

        except Exception as e:
            _log(f"錯誤: {e}\n{_tb.format_exc()}")

        if _listener_running:
            _log(f"{int(reconnect_delay)} 秒後重新連線...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 1.5, 120)


def _listener_thread_fn():
    """執行緒入口"""
    _log("thread started")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_listener())
    except Exception as e:
        _log(f"執行緒異常退出: {e}\n{_tb.format_exc()}")
    finally:
        loop.close()
        _log("thread ended")


def start_listener():
    """啟動背景監聽執行緒"""
    global _listener_running, _listener_thread
    if _listener_running:
        logger.info("[Listener] 已在運行中")
        return

    _listener_running = True
    _listener_thread = threading.Thread(target=_listener_thread_fn, daemon=True, name="mt-ws-listener")
    _listener_thread.start()
    logger.info("[Listener] 背景監聽已啟動")


def stop_listener():
    """停止監聽"""
    global _listener_running
    _listener_running = False
    logger.info("[Listener] 停止監聽信號已發送")


def is_listener_running():
    """檢查監聽是否在運行"""
    return _listener_running and _listener_thread and _listener_thread.is_alive()


# ── 房間數據查詢（取代 mt_rooms.py 的 Playwright 呼叫）──

TABLE_CATEGORY = {
    "BAG01": "中文廳", "BAG02": "中文廳", "BAG03": "中文廳",
    "BAG05": "中文廳", "BAG06": "中文廳", "BAG07": "中文廳",
    "BAG08": "中文廳", "BAG09": "中文廳", "BAG10": "中文廳",
    "BAG11": "中文廳", "BAG12": "中文廳", "BAG13": "中文廳",
    "BAG03A": "亞洲廳",
    "DTG01": "龍虎", "DTG02": "龍虎", "DTG03": "龍虎",
    "NUG01": "牛牛", "SBG01": "骰寶",
}


def get_room_data(table_id):
    """取得單桌的完整房間資訊"""
    with _lock:
        return dict(_table_info.get(table_id, {}))


def get_rooms_by_category(category):
    """取得指定類別的所有房間（從 Listener 快取）"""
    with _lock:
        rooms = []
        for tid, info in _table_info.items():
            cat = TABLE_CATEGORY.get(tid, "其他")
            if cat == category and info.get("table_name"):
                rooms.append({
                    "table_id": tid,
                    "category": cat,
                    **info,
                })
        return rooms


def get_all_room_data():
    """取得所有桌台的房間資訊"""
    with _lock:
        return [
            {"table_id": tid, "category": TABLE_CATEGORY.get(tid, "其他"), **info}
            for tid, info in _table_info.items()
            if info.get("table_name")
        ]


# ── 桌台 ID 對應顯示名稱 ──
TABLE_DISPLAY_MAP = {
    "BAG01": "百家樂 1", "BAG02": "百家樂 2", "BAG03": "百家樂 3",
    "BAG05": "百家樂 5", "BAG06": "百家樂 6", "BAG07": "百家樂 7",
    "BAG08": "百家樂 8", "BAG09": "百家樂 9", "BAG10": "百家樂 10",
    "BAG11": "百家樂 11", "BAG12": "百家樂 12", "BAG13": "百家樂 13",
    "BAG03A": "百家樂 3A",
    "DTG01": "龍虎 1", "DTG02": "龍虎 2", "DTG03": "龍虎 3",
    "NUG01": "牛牛 1",
    "SBG01": "骰寶 1",
}

# 反向映射：顯示名稱 → 桌台 ID
DISPLAY_TO_TABLE = {v: k for k, v in TABLE_DISPLAY_MAP.items()}


def display_to_table_id(display_name):
    """將顯示名稱轉為桌台 ID"""
    return DISPLAY_TO_TABLE.get(display_name)


def table_id_to_display(table_id):
    """將桌台 ID 轉為顯示名稱"""
    return TABLE_DISPLAY_MAP.get(table_id, table_id)


# ── 測試 ──
if __name__ == "__main__":
    import os
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()
    except FileNotFoundError:
        pass

    start_listener()
    try:
        while True:
            time.sleep(10)
            tables = get_all_tables()
            print(f"\n[{time.strftime('%H:%M:%S')}] 追蹤中: {len(tables)} 桌")
            for tid in sorted(tables):
                hist = get_table_history(tid)
                info = get_table_info(tid)
                last5 = "".join(h[0] for h in hist[-5:]) if hist else "-"
                print(f"  {tid}: {len(hist)}局 最近5局={last5} shoe={info.get('shoe')}")
    except KeyboardInterrupt:
        stop_listener()
        print("\n已停止")
