"""
MT真人 即時牌路追蹤模組
長駐 Playwright 頁面，監聽 WebSocket show_win 事件，
自動累積每桌牌路歷史，供 LINE Bot 直接查詢。
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
    """主監聽迴圈：用 fetch_mt_token_with_session() 取得 token+browser → 直接在同一 browser 監聽 WS"""
    global _listener_running
    _log("_run_listener 開始執行")

    try:
        import os as _os
        _os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/.cache/pw-browsers")
        _log(f"PLAYWRIGHT_BROWSERS_PATH={_os.environ.get('PLAYWRIGHT_BROWSERS_PATH', 'NOT SET')}")
        from mt_token import fetch_mt_token_with_session
        _log("mt_token import OK")
    except Exception as e:
        _log(f"import 失敗: {e}\n{_tb.format_exc()}")
        return

    reconnect_delay = 10

    while _listener_running:
        pw_instance = None
        browser = None
        try:
            # ---- Step1: 登入+取token（返回同一個 browser session）----
            _log("Step1: 登入平台+取得 MT token...")
            pw_instance, browser, context, token = await fetch_mt_token_with_session()
            _log(f"Step1 OK: token={token[:16]}...")

            # ---- Step2: 在現有 context 中找到 MT 遊戲頁面 ----
            _log(f"Step2: 尋找 MT 遊戲頁面（共 {len(context.pages)} 個分頁）...")

            ws_connected = asyncio.Event()
            ws_closed = asyncio.Event()

            def _bind_ws(target_page):
                def on_ws(ws):
                    _log(f"WS 偵測到: {ws.url[:80]}")
                    if "/ws" not in ws.url:
                        return
                    _log(f"✅ 目標 WS 已連線: {ws.url[:80]}")
                    ws_connected.set()

                    def on_frame(payload):
                        try:
                            data = json.loads(payload)
                            action = data.get("action", "")
                            if isinstance(action, dict):
                                name = action.get("name", "")
                                if "show_win" in name:
                                    _on_show_win(data.get("body", {}))
                            elif isinstance(action, str) and "tables" in action:
                                msg = data.get("msg", {})
                                tables = msg.get("tables", [])
                                if isinstance(tables, dict):
                                    tables = tables.get("tables", [])
                                if tables:
                                    _on_tables_response(tables)
                        except Exception:
                            pass

                    def on_close():
                        _log("WS 已斷開")
                        ws_closed.set()

                    ws.on("framereceived", on_frame)
                    ws.on("close", on_close)
                target_page.on("websocket", on_ws)

            # 綁定 WS 監聽到所有現有頁面
            mt_page = None
            for pg in context.pages:
                _log(f"  分頁: {pg.url[:60]}")
                _bind_ws(pg)
                if "ofalive" in pg.url or "token=" in pg.url:
                    mt_page = pg

            # 監聽未來新分頁
            def on_new_page(new_page):
                _log(f"  新分頁: {new_page.url[:60]}")
                _bind_ws(new_page)
            context.on("page", on_new_page)

            # ---- Step3: 等待 WS ----
            _log("Step3: 等待 WS 連線...")
            try:
                await asyncio.wait_for(ws_connected.wait(), timeout=30)
            except asyncio.TimeoutError:
                if mt_page:
                    _log(f"Step3a: MT頁面存在({mt_page.url[:60]})但WS未建立，再等30s...")
                else:
                    # 手動 goto MT
                    _log("Step3a: 沒找到MT頁面，手動開啟...")
                    mt_url = f"https://gsa.ofalive99.net/?token={token}&lang=zhtw"
                    mt_page = await context.new_page()
                    _bind_ws(mt_page)
                    await mt_page.goto(mt_url, wait_until="domcontentloaded", timeout=60000)
                    _log(f"Step3a: goto 完成, URL={mt_page.url[:60]}")
                await asyncio.sleep(15)
                if not ws_connected.is_set():
                    try:
                        await asyncio.wait_for(ws_connected.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        pages_info = [pg.url[:50] for pg in context.pages]
                        _log(f"WS 超時，所有頁面: {pages_info}")
                        await browser.close()
                        await pw_instance.stop()
                        continue

            _log("✅ WS 已連線，開始監聽即時開牌")
            reconnect_delay = 10

            # 找到 MT 頁面用於 keep-alive
            if not mt_page:
                for pg in context.pages:
                    if "ofalive" in pg.url or "token=" in pg.url:
                        mt_page = pg
                        break
            if not mt_page:
                mt_page = context.pages[-1] if context.pages else None

            # ---- 保持運行直到 WS 斷開 ----
            while _listener_running and not ws_closed.is_set():
                try:
                    await asyncio.wait_for(ws_closed.wait(), timeout=300)
                except asyncio.TimeoutError:
                    if mt_page:
                        try:
                            await mt_page.evaluate("1+1")
                        except Exception:
                            _log("頁面已失效，重新連線")
                            break

            await browser.close()
            await pw_instance.stop()

        except Exception as e:
            _log(f"錯誤: {e}\n{_tb.format_exc()}")
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if pw_instance:
                try:
                    await pw_instance.stop()
                except Exception:
                    pass

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
