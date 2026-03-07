import json
import requests
from flask import Flask, request, jsonify, abort
import os
import uuid
from datetime import datetime, timedelta, timezone
import traceback
import random
import threading
import hmac
import hashlib
import base64
from collections import Counter

app = Flask(__name__)

print("[BOOT] sv94.py 模組載入中...", flush=True)

# --- 基礎配置 ---
LINE_ACCESS_TOKEN = os.environ.get("LINE_ACCESS_TOKEN", "Y6KHkjxZnW9I0pbDV6ogI3A0/+USC4q2+bnnTgBrG9A/WT7Hm8dpLGmviC4jNM3mk186VYBkyAag7wFqYMXE92fJXSvUm/xFCmjOdDm0rPZ0+dnnBNMYR7Kpj5xmsBslD4e+BlFjOTfXrlILdXdRTAdB04t89/1O/w1cDnyilFU=")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "107a3917516a9c8efc23c3229aaefc71")
FIXED_RTP = 96.89

# 管理員 UID
ADMIN_UIDS = ["Ub9a0ddfd2b9fd49e3500fa08e2fbbbe7", "U543d02a7d79565a14d475bff5b357f05"]

USER_DATA_FILE = "user_data.json"
TIME_CARDS_FILE = "time_cards.json"

# 允許的序號期限
VALID_DURATIONS = {"10M": "10分鐘", "1H": "1小時", "2D": "2天", "7D": "7天", "12D": "12天", "30D": "30天"}

# --- 全局變數初始化 ---
baccarat_history_dict = {}
chat_modes = {}
user_access_data = {}
time_cards_data = {"active_cards": {}, "used_cards": {}}
profit_tracker = {}  # uid -> {bankroll, unit, bet_side, history[], total_profit, rounds}

user_data_lock = threading.RLock()
time_cards_data_lock = threading.RLock()

# --- 資料存取 ---
def load_data(f, default_val=None):
    if os.path.exists(f):
        try:
            with open(f, 'r', encoding='utf-8') as file:
                return json.load(file)
        except:
            pass
    return default_val if default_val is not None else {}

def save_data(f, d):
    try:
        with open(f, 'w', encoding='utf-8') as file:
            json.dump(d, file, ensure_ascii=False, indent=4)
    except:
        pass

# 模組載入時讀取資料
user_access_data = load_data(USER_DATA_FILE)
time_cards_data = load_data(TIME_CARDS_FILE, {"active_cards": {}, "used_cards": {}})

# --- 房間清單 ---
MT_ROOMS = [f"百家樂 {i}" if i != 4 else "百家樂 3A" for i in range(1, 14)]
DG_ROOMS = ([f"A0{i}" for i in range(1, 6) if i != 4] + [f"C0{i}" for i in range(1, 7) if i != 4] + [f"D0{i}" for i in range(1, 9) if i != 4])

# --- 安全驗證 ---
def verify_signature(body, signature):
    hash = hmac.new(LINE_CHANNEL_SECRET.encode('utf-8'), body.encode('utf-8'), hashlib.sha256).digest()
    return base64.b64encode(hash).decode('utf-8') == signature

# ==================== 核心邏輯：電子預測 ====================
def calculate_slot_logic(total_bet, score_rate):
    expected_return = total_bet * (FIXED_RTP / 100.0)
    actual_gain = total_bet * (score_rate / 100.0)
    bonus_space = expected_return - actual_gain
    if score_rate >= FIXED_RTP:
        if score_rate > 110:
            level, color = "⚠️ 高位震盪", "#9B59B6"
            desc = f"機台今日表現({score_rate}%)遠超預期，正處於極端吐分波段，隨時可能反轉，建議謹慎操作。"
        else:
            level, color = "🌟 熱機中", "#E67E22"
            desc = "機台數據飽和但動能強勁，目前屬於「連續爆分」波段，建議小量跟進觀察。"
    else:
        if bonus_space >= 500000:
            level, color, desc = "🔥 極致推薦", "#FF4444", "機台積累大量預算，目前處於大回補窗口，爆發力極強！"
        elif bonus_space > 0:
            level, color, desc = "✅ 推薦", "#2ECC71", "機台狀態正向，仍有補償空間，穩定操作。"
        else:
            level, color, desc = "☁️ 觀望", "#7F8C8D", "數據趨於平衡，建議更換房間或等待下一個週期。"
    return {"space": bonus_space, "level": level, "color": color, "desc": desc}

# ==================== 核心邏輯：百家預測 (強化版) ====================
# --- 8副牌基礎常量 ---
DECKS = 8
TOTAL_CARDS = DECKS * 52  # 416張
# 標準百家樂機率 (8副牌理論值)
BASE_BANKER_PROB = 0.4586
BASE_PLAYER_PROB = 0.4462
BASE_TIE_PROB = 0.0952
# 賠率設定
BANKER_PAYOUT = 0.95   # 莊贏賠率 (扣5%佣金)
PLAYER_PAYOUT = 1.0    # 閒贏賠率
TIE_PAYOUT = 8.0       # 和贏賠率

def _perm(n, r):
    """計算 P(n, r) = n * (n-1) * ... * (n-r+1)"""
    result = 1
    for i in range(r):
        result *= (n - i)
    return result

def _calculate_accuracy_index(round_num):
    """根據局數計算精準度指標 (0~100%)，局數越多越準確"""
    def exp_approx(x):
        return 1 - x + (x**2)/2 - (x**3)/6 + (x**4)/24
    if round_num < 3:
        decay = exp_approx(round_num / 3)
        accuracy = (1 - decay) * 18
    elif round_num < 6:
        decay = exp_approx((round_num - 3) / 3)
        accuracy = 18 + (1 - decay) * 12
    elif round_num < 10:
        decay = exp_approx((round_num - 6) / 4)
        accuracy = 30 + (1 - decay) * 20
    elif round_num < 30:
        decay = exp_approx((round_num - 10) / 20)
        accuracy = 50 + (1 - decay) * 45
    else:
        accuracy = 95
    return min(100, max(0, round(accuracy, 2)))

def _estimate_shoe_state(history):
    """根據歷史紀錄估算牌靴消耗狀態"""
    pure = [h for h in history if h in ("莊", "閒")]
    tie_count = history.count("和")
    total_hands = len(pure) + tie_count
    # 平均每手用 4.94 張牌
    avg_cards_per_hand = 4.94
    cards_used = total_hands * avg_cards_per_hand
    remaining = max(TOTAL_CARDS - cards_used, 52)
    shoe_progress = cards_used / TOTAL_CARDS  # 0~1 牌靴進度
    return remaining, shoe_progress, total_hands

def _compute_dynamic_probability(history):
    """根據歷史動態調整莊/閒/和機率"""
    pure = [h for h in history if h in ("莊", "閒")]
    tie_count = history.count("和")
    total_hands = len(pure) + tie_count
    if total_hands == 0:
        return BASE_BANKER_PROB, BASE_PLAYER_PROB, BASE_TIE_PROB
    b_count = pure.count("莊")
    p_count = pure.count("閒")
    remaining, shoe_progress, _ = _estimate_shoe_state(history)
    # 貝葉斯校正：將觀測頻率與理論值加權混合
    # 局數越多，觀測值權重越高
    obs_weight = min(total_hands / 60, 0.7)  # 最多觀測佔70%
    theory_weight = 1 - obs_weight
    obs_b = b_count / max(len(pure), 1)
    obs_p = p_count / max(len(pure), 1)
    obs_t = tie_count / max(total_hands, 1)
    adj_b = theory_weight * BASE_BANKER_PROB + obs_weight * obs_b
    adj_p = theory_weight * BASE_PLAYER_PROB + obs_weight * obs_p
    adj_t = theory_weight * BASE_TIE_PROB + obs_weight * obs_t
    # 牌靴深度校正：越深入牌靴，偏差越顯著
    depth_factor = 1 + shoe_progress * 0.15
    if obs_b > BASE_BANKER_PROB:
        adj_b *= depth_factor
    if obs_p > BASE_PLAYER_PROB:
        adj_p *= depth_factor
    # 正規化
    total_prob = adj_b + adj_p + adj_t
    return adj_b / total_prob, adj_p / total_prob, adj_t / total_prob

def _compute_ev(prob_b, prob_p, prob_t):
    """計算各下注選項的期望值 (EV)"""
    # 莊 EV = P(莊贏)*0.95 - P(閒贏)*1 - P(和)*0 (和退注)
    ev_banker = prob_b * BANKER_PAYOUT - prob_p * 1.0
    # 閒 EV = P(閒贏)*1 - P(莊贏)*1 - P(和)*0
    ev_player = prob_p * PLAYER_PAYOUT - prob_b * 1.0
    # 和 EV = P(和)*8 - P(非和)*1
    ev_tie = prob_t * TIE_PAYOUT - (1 - prob_t) * 1.0
    return ev_banker, ev_player, ev_tie

def _detect_patterns(pure):
    """大路牌型偵測 (強化版)"""
    patterns = []
    if len(pure) < 2:
        return patterns, None, None
    # Build streaks (大路列)
    streaks = []
    cur = [pure[0]]
    for h in pure[1:]:
        if h == cur[0]:
            cur.append(h)
        else:
            streaks.append(cur)
            cur = [h]
    streaks.append(cur)
    last_streak = streaks[-1]
    last_val = last_streak[0]
    last_len = len(last_streak)
    opp_val = "閒" if last_val == "莊" else "莊"
    suggest = None
    confidence = 60

    # ===== 長莊 / 長閒 (連續4個或以上) =====
    if last_len >= 6:
        patterns.append(f"超級長龍：連續{last_len}{last_val}，強勢延續")
        suggest = last_val
        confidence = 82
    elif last_len >= 4:
        patterns.append(f"長{last_val}：連續{last_len}{last_val}，龍尾延續中")
        suggest = last_val
        confidence = 78
    elif last_len >= 3:
        patterns.append(f"長{last_val}：連{last_len}{last_val}，龍尾延續中")
        suggest = last_val
        confidence = 72

    # ===== 大路單跳 (莊閒梅花間竹) =====
    if len(streaks) >= 6:
        r6 = streaks[-6:]
        if all(len(s) == 1 for s in r6):
            patterns.append(f"大路單跳：莊閒交替×6，預測跳至{opp_val}")
            suggest = opp_val
            confidence = 74
    elif len(streaks) >= 4:
        r4 = streaks[-4:]
        if all(len(s) == 1 for s in r4):
            patterns.append(f"大路單跳：莊閒交替出現，預測跳至{opp_val}")
            suggest = opp_val
            confidence = 70

    # ===== 雙跳 (BBPPBBPP) =====
    if len(streaks) >= 4:
        r4 = streaks[-4:]
        if all(len(s) == 2 for s in r4):
            if last_len == 2:
                patterns.append(f"雙跳路：近期雙雙交替，預測跳至{opp_val}")
                suggest = opp_val
                confidence = 72
            elif last_len == 1:
                patterns.append(f"雙跳路：預測{last_val}再開一局")
                suggest = last_val
                confidence = 68

    # ===== 一莊兩閒 / 兩莊一閒 =====
    if len(streaks) >= 4:
        r4 = streaks[-4:]
        lens4 = [len(s) for s in r4]
        vals4 = [s[0] for s in r4]
        if lens4 == [1, 2, 1, 2] and vals4[0] == vals4[2] and vals4[1] == vals4[3]:
            a, b = vals4[0], vals4[1]
            patterns.append(f"一{a}兩{b}：規律重複中")
            if last_len == 2 and last_val == b:
                suggest = a
                confidence = 70
            elif last_len == 1 and last_val == a:
                suggest = b
                confidence = 68
        elif lens4 == [2, 1, 2, 1] and vals4[0] == vals4[2] and vals4[1] == vals4[3]:
            a, b = vals4[0], vals4[1]
            patterns.append(f"兩{a}一{b}：規律重複中")
            if last_len == 1 and last_val == b:
                suggest = a
                confidence = 70
            elif last_len == 2 and last_val == a:
                suggest = b
                confidence = 68

    # ===== 逢莊跳 / 逢閒跳 =====
    if len(streaks) >= 6:
        r6 = streaks[-6:]
        b_streaks = [s for s in r6 if s[0] == "莊"]
        p_streaks = [s for s in r6 if s[0] == "閒"]
        if b_streaks and all(len(s) == 1 for s in b_streaks) and len(b_streaks) >= 2:
            patterns.append("逢莊跳：莊每次只出1個就轉閒")
            if last_val == "莊" and last_len == 1:
                suggest = "閒"
                confidence = 72
        if p_streaks and all(len(s) == 1 for s in p_streaks) and len(p_streaks) >= 2:
            patterns.append("逢閒跳：閒每次只出1個就轉莊")
            if last_val == "閒" and last_len == 1:
                suggest = "莊"
                confidence = 72

    # ===== 逢莊連 / 逢閒連 =====
    if len(streaks) >= 5:
        r5 = streaks[-5:]
        vals5 = [s[0] for s in r5]
        lens5 = [len(s) for s in r5]
        if vals5[0] == "莊" and vals5[2] == "莊" and vals5[4] == "莊":
            if all(lens5[i] >= 2 for i in [0, 2, 4]) and all(lens5[i] >= 1 for i in [1, 3]):
                patterns.append("逢莊連：莊每次出現都連續2個以上")
                if last_val == "莊" and last_len >= 1:
                    suggest = "莊"
                    confidence = 73
        if vals5[0] == "閒" and vals5[2] == "閒" and vals5[4] == "閒":
            if all(lens5[i] >= 2 for i in [0, 2, 4]) and all(lens5[i] >= 1 for i in [1, 3]):
                patterns.append("逢閒連：閒每次出現都連續2個以上")
                if last_val == "閒" and last_len >= 1:
                    suggest = "閒"
                    confidence = 73

    # ===== 排排連 =====
    if len(streaks) >= 4:
        r4 = streaks[-4:]
        if all(len(s) >= 2 for s in r4):
            patterns.append("排排連：最近4列都連續2個以上")
            if last_len >= 2:
                suggest = last_val
                confidence = 70

    # ===== 長度遞增 (1,2,3... 或 2,3,4...) =====
    if len(streaks) >= 3:
        r3 = streaks[-3:]
        lens3 = [len(s) for s in r3]
        if lens3[0] < lens3[1] < lens3[2]:
            patterns.append(f"遞增路：長度{lens3[0]}→{lens3[1]}→{lens3[2]}，趨勢加強")
            suggest = last_val
            confidence = max(confidence, 71)
        elif lens3[0] > lens3[1] > lens3[2] and lens3[2] == 1:
            patterns.append(f"遞減路：長度{lens3[0]}→{lens3[1]}→{lens3[2]}，趨勢衰退")
            suggest = opp_val
            confidence = max(confidence, 68)

    # ===== 鏡像路 (ABBA pattern) =====
    if len(streaks) >= 4:
        r4 = streaks[-4:]
        lens4 = [len(s) for s in r4]
        if lens4[0] == lens4[3] and lens4[1] == lens4[2]:
            patterns.append(f"鏡像路：長度{lens4[0]}-{lens4[1]}-{lens4[2]}-{lens4[3]}對稱")
            confidence = max(confidence, 69)

    return patterns, suggest, confidence

def _analyze_derived(road, name):
    """分析衍生路趨勢"""
    if not road or len(road) < 3:
        return None
    r_count = road.count("R")
    b_count = road.count("B")
    total = len(road)
    r_pct = round(r_count / total * 100)
    last3 = road[-3:]
    last5 = road[-min(5, len(road)):]
    r5 = last5.count("R")
    # 加強：看最近5筆趨勢
    if all(x == "R" for x in last3):
        return f"{name}：紅{r_pct}%（近期全紅=規律強）"
    elif all(x == "B" for x in last3):
        return f"{name}：藍{100-r_pct}%（近期全藍=無規律）"
    elif r5 >= 4:
        return f"{name}：紅{r_pct}%（近5筆紅{r5}個=趨勢穩定）"
    elif r5 <= 1:
        return f"{name}：藍{100-r_pct}%（近5筆藍{5-r5}個=趨勢混亂）"
    return f"{name}：紅{r_pct}%/藍{100-r_pct}%"

def _derived_vote(road):
    """衍生路投票：+1=跟趨勢 -1=反轉"""
    if not road or len(road) < 2:
        return 0
    last3 = road[-min(3, len(road)):]
    r = last3.count("R")
    b = last3.count("B")
    if r > b:
        return 1
    elif b > r:
        return -1
    return 0

def baccarat_ai_logic(history_list, big_eye=None, small_r=None, cockroach=None, total_counts=None):
    """強化版百家AI邏輯：結合機率模型 + 牌路分析 + 衍生路 + 期望值計算"""
    pure_history = [h for h in history_list if h in ["莊", "閒"]]
    if not pure_history:
        return {"下注": "等待數據", "勝率": 50, "建議注碼": "觀察", "模式": "數據不足",
                "理由": "數據不足，等待更多開牌紀錄", "精準度": 0}
    # 使用累計總數（若有）來計算精準度和統計
    if total_counts:
        b_count = total_counts.get("莊", 0)
        p_count = total_counts.get("閒", 0)
        t_count = total_counts.get("和", 0)
        total = b_count + p_count
        total_hands = b_count + p_count + t_count
    else:
        counts = Counter(pure_history)
        b_count = counts.get("莊", 0)
        p_count = counts.get("閒", 0)
        total = b_count + p_count
        total_hands = len(history_list)
    b_pct = round(b_count / total * 100) if total else 50
    p_pct = 100 - b_pct

    # --- (1) 機率模型：動態機率 + EV ---
    prob_b, prob_p, prob_t = _compute_dynamic_probability(history_list)
    ev_b, ev_p, ev_t = _compute_ev(prob_b, prob_p, prob_t)
    remaining_cards, shoe_progress, _ = _estimate_shoe_state(history_list)

    # --- (2) 精準度指標 ---
    accuracy = _calculate_accuracy_index(total_hands)

    # --- (3) 大路牌型偵測 ---
    patterns, suggest, confidence = _detect_patterns(pure_history)

    # --- (4) 衍生路分析 ---
    derived_reasons = []
    derived_score = 0
    for road, name in [(big_eye, "大眼仔"), (small_r, "小路"), (cockroach, "蟑螂路")]:
        info = _analyze_derived(road, name)
        if info:
            derived_reasons.append(info)
        derived_score += _derived_vote(road)

    # --- (5) 當前連莊/連閒 ---
    streak = 1
    for i in range(len(pure_history) - 2, -1, -1):
        if pure_history[i] == pure_history[-1]:
            streak += 1
        else:
            break

    # --- (6) 綜合決策：多維度加權 ---
    score_banker = 0
    score_player = 0
    decision_factors = []

    # 維度A：EV (期望值) → 權重 30%
    if ev_b > ev_p:
        score_banker += 30
        decision_factors.append(f"期望值莊{ev_b:+.4f} > 閒{ev_p:+.4f}")
    else:
        score_player += 30
        decision_factors.append(f"期望值閒{ev_p:+.4f} > 莊{ev_b:+.4f}")

    # 維度B：動態機率 → 權重 25%
    if prob_b > prob_p:
        score_banker += 25
    else:
        score_player += 25

    # 維度C：大路牌型 → 權重 25%
    if suggest == "莊":
        score_banker += 25
    elif suggest == "閒":
        score_player += 25
    else:
        # 無明確牌型建議，微偏莊 (理論優勢)
        score_banker += 13
        score_player += 12

    # 維度D：衍生路 → 權重 20%
    if derived_score >= 2:
        # 規律強，跟隨當前趨勢
        if pure_history[-1] == "莊":
            score_banker += 20
        else:
            score_player += 20
    elif derived_score <= -2:
        # 無規律，反轉
        if pure_history[-1] == "莊":
            score_player += 20
        else:
            score_banker += 20
    else:
        score_banker += 10
        score_player += 10

    # 最終預測
    if score_banker > score_player:
        final_prediction = "莊"
        conf = min(55 + int((score_banker - score_player) * 0.6) + int(accuracy * 0.15), 92)
    elif score_player > score_banker:
        final_prediction = "閒"
        conf = min(55 + int((score_player - score_banker) * 0.6) + int(accuracy * 0.15), 92)
    else:
        final_prediction = "莊"  # 平局偏莊
        conf = 58

    # 牌型置信度加成
    if suggest and confidence > 70:
        conf = max(conf, confidence)

    # --- (7) 模式 & 注碼建議 (1~5級，保守策略) ---
    best_ev = max(ev_b, ev_p)
    score_diff = abs(score_banker - score_player)

    # 基礎注碼：由信心度映射 (conf 55~92 → 注碼 1~3)
    bet_units = max(1, min(3, round((conf - 55) / 15) + 1))

    # 加分因子（保守）
    if streak >= 5 and best_ev > 0:
        bet_units += 2
    elif streak >= 4 and best_ev > 0:
        bet_units += 1
    elif streak >= 3:
        bet_units += 1
    if patterns and confidence and confidence >= 72 and best_ev > 0:
        bet_units += 1

    # 減分因子
    if best_ev < -0.005:
        bet_units -= 1
    if score_diff <= 5:
        bet_units -= 1
    if total_hands < 5:
        bet_units = min(bet_units, 2)

    # 限制範圍 1~5
    bet_units = max(1, min(5, bet_units))

    bet = f"{bet_units}單位"

    # 模式判定
    if bet_units >= 4:
        mode = "🔥 強勢跟進"
    elif bet_units == 3:
        mode = "✅ 穩健跟進"
    elif bet_units == 2:
        mode = "📈 輕注試探"
    else:
        mode = "☁️ 觀望為主"

    # --- (8) 組建理由文字 ---
    reasons = []
    # 機率統計
    reasons.append(f"📊 機率：莊{prob_b*100:.1f}% / 閒{prob_p*100:.1f}% / 和{prob_t*100:.1f}%")
    reasons.append(f"💰 期望值：莊{ev_b:+.4f} / 閒{ev_p:+.4f}")
    reasons.append(f"📈 精準度：{accuracy}% (已分析{total_hands}局)")
    reasons.append(f"🃏 牌靴進度：{shoe_progress*100:.0f}% (約剩{remaining_cards:.0f}張)")
    # 歷史統計
    reasons.append(f"📋 歷史：莊{b_pct}%({b_count}局) / 閒{p_pct}%({p_count}局)")
    if streak >= 2:
        reasons.append(f"🔗 連{streak}{pure_history[-1]}")
    # 牌型
    for p in patterns:
        reasons.append(f"🎯 {p}")
    # 衍生路
    for d in derived_reasons:
        reasons.append(f"🔍 {d}")
    if not patterns and not derived_reasons:
        reasons.append("⏳ 暫無明顯好路，依機率模型推薦")

    reason_text = "📊 AI分析報告：\n" + "\n".join(f"• {r}" for r in reasons)
    return {"下注": final_prediction, "勝率": conf, "建議注碼": bet, "模式": mode,
            "理由": reason_text, "精準度": accuracy}

# ==================== 五路算法 ====================
def compute_big_road(history, max_rows=6):
    pure = [h for h in history if h in ("莊", "閒")]
    if not pure:
        return {}, 0
    grid = {}
    r, c = 0, 0
    vert_col = 0
    tailing = False
    grid[(r, c)] = pure[0]
    max_col = 0
    for h in pure[1:]:
        prev = grid[(r, c)]
        if h == prev:
            if not tailing and r + 1 < max_rows and (r + 1, c) not in grid:
                r += 1
            else:
                tailing = True
                c += 1
                while (r, c) in grid:
                    c += 1
        else:
            tailing = False
            new_c = vert_col + 1
            r = 0
            while (r, new_c) in grid:
                new_c += 1
            c = new_c
            vert_col = c
        grid[(r, c)] = h
        if c > max_col:
            max_col = c
    return grid, max_col + 1

def compute_big_road_cols(history):
    cols = []
    pure = [h for h in history if h in ("莊", "閒")]
    if not pure:
        return cols
    current_col = [pure[0]]
    for h in pure[1:]:
        if h == current_col[0]:
            current_col.append(h)
        else:
            cols.append(list(current_col))
            current_col = [h]
    cols.append(list(current_col))
    return cols

# ==================== 衍生路算法 ====================
def _derived_road(big_road_cols, gap):
    # gap=1: 大眼仔路, gap=2: 小路, gap=3: 蟑螂路
    # 大眼仔: start col2 row2 (1-idx), fallback col3 row1
    # 小路:   start col3 row2 (1-idx), fallback col4 row1
    # 蟑螂路: start col4 row2 (1-idx), fallback col5 row1
    # 和局不計入 (big_road_cols already excludes 和)
    results = []
    n = len(big_road_cols)
    # Determine starting point (convert 1-indexed to 0-indexed)
    primary_col = gap      # col (gap+1) in 1-idx = col gap in 0-idx
    primary_row = 1        # row 2 in 1-idx = row 1 in 0-idx
    fallback_col = gap + 1 # col (gap+2) in 1-idx = col (gap+1) in 0-idx
    fallback_row = 0       # row 1 in 1-idx = row 0 in 0-idx
    if primary_col < n and len(big_road_cols[primary_col]) >= 2:
        start_ci, start_ri = primary_col, primary_row
    elif fallback_col < n:
        start_ci, start_ri = fallback_col, fallback_row
    else:
        return results
    # Iterate through big road positions from start point
    started = False
    for ci in range(n):
        for ri in range(len(big_road_cols[ci])):
            if not started:
                if ci == start_ci and ri == start_ri:
                    started = True
                else:
                    continue
            # Judgment
            if ri == 0:
                # 齊整: compare col(ci-1) length vs col(ci-1-gap) length
                prev_ci = ci - 1
                compare_ci = ci - 1 - gap
                if prev_ci < 0 or compare_ci < 0:
                    continue
                results.append("R" if len(big_road_cols[prev_ci]) == len(big_road_cols[compare_ci]) else "B")
            else:
                # 直落: move gap left, compare current row with row above
                # (ci-gap, ri) exists? AND (ci-gap, ri-1) exists?
                # Same state (both exist or both don't) = Red, different = Blue
                ref_ci = ci - gap
                if ref_ci < 0:
                    continue
                ref_len = len(big_road_cols[ref_ci])
                cur_exists = ri < ref_len
                above_exists = (ri - 1) < ref_len
                results.append("R" if cur_exists == above_exists else "B")
    return results

def _derived_to_cols(flat):
    if not flat:
        return []
    cols = [[flat[0]]]
    for h in flat[1:]:
        if h == cols[-1][0]:
            cols[-1].append(h)
        else:
            cols.append([h])
    return cols


def compute_derived_roads(big_road_cols):
    return (
        _derived_road(big_road_cols, 1),
        _derived_road(big_road_cols, 2),
        _derived_road(big_road_cols, 3)
    )

# ==================== UI 組件：五路渲染 ====================
CM = {"莊": "#E74C3C", "閒": "#2E86C1", "和": "#27AE60"}
LM = {"莊": "莊", "閒": "閒", "和": "和"}
DM = {"R": "#E74C3C", "B": "#2E86C1"}

def _circle(bg, txt, sz="18px"):
    return {
        "type": "box", "layout": "vertical", "cornerRadius": "50px",
        "width": sz, "height": sz, "backgroundColor": bg,
        "contents": [{"type": "text", "text": txt, "size": "xxs", "color": "#ffffff", "align": "center", "gravity": "center"}],
        "justifyContent": "center", "alignItems": "center"
    }

def _dot(bg, sz="8px"):
    return {
        "type": "box", "layout": "vertical", "cornerRadius": "50px",
        "width": sz, "height": sz, "backgroundColor": bg,
        "contents": [{"type": "filler"}]
    }

def _hollow(border_color, sz="8px"):
    return {
        "type": "box", "layout": "vertical", "cornerRadius": "50px",
        "width": sz, "height": sz, "backgroundColor": "#ffffff",
        "borderColor": border_color, "borderWidth": "2px",
        "contents": [{"type": "filler"}]
    }

def _slash(color, sz="10px"):
    return {
        "type": "box", "layout": "vertical",
        "width": sz, "height": sz,
        "contents": [{"type": "text", "text": "/", "size": "xxs", "color": color, "align": "center", "gravity": "center"}],
        "justifyContent": "center", "alignItems": "center"
    }

def _empty(sz="18px"):
    return {"type": "box", "layout": "vertical", "width": sz, "height": sz, "contents": [{"type": "filler"}]}

def _grid(cols_data, rows, cell_fn, esz="18px"):
    if not cols_data:
        return {"type": "box", "layout": "horizontal", "contents": [{"type": "filler"}], "height": "20px"}
    ui = []
    for col in cols_data:
        cells = []
        for r in range(rows):
            if r < len(col):
                cells.append(cell_fn(col[r]))
            else:
                cells.append(_empty(esz))
        ui.append({"type": "box", "layout": "vertical", "contents": cells, "spacing": "xs", "flex": 0, "width": esz, "alignItems": "center"})
    ui.append({"type": "filler"})
    return {
        "type": "box", "layout": "horizontal", "contents": ui, "spacing": "xs",
        "paddingAll": "xs", "backgroundColor": "#F8F9FA", "cornerRadius": "md"
    }

def _section(title, widget):
    return {
        "type": "box", "layout": "vertical", "margin": "xs", "contents": [
            {"type": "text", "text": title, "size": "xxs", "color": "#888888", "weight": "bold"},
            widget
        ]
    }

def build_bead_road(history, max_cols=15):
    # 6行N列. 每列由上至下填6顆，填滿往右換下一列
    # 超過max_cols列後，以列為單位丟掉最舊的列
    nrows = 6
    sz = "18px"
    # 每6筆一列（由上至下）
    cols = [history[i:i + nrows] for i in range(0, len(history), nrows)]
    # 取最後max_cols列（以列為單位縮減，保持對齊）
    if len(cols) > max_cols:
        cols = cols[-max_cols:]
    # 帶文字圓圈 (紅=莊, 藍=閒, 綠=和)
    return _section("珠盤路", _grid(cols, nrows, lambda x: _circle(CM.get(x, "#27AE60"), LM.get(x, "和"), sz), sz))

def build_big_road_ui(grid_data, max_display=80):
    grid, num_cols = grid_data
    if not grid:
        return _section("大路", {"type": "box", "layout": "horizontal", "contents": [{"type": "filler"}], "height": "20px"})
    display_cols = min(num_cols, max_display)
    # Show LAST display_cols columns (most recent data)
    start_col = max(num_cols - display_cols, 0)
    # Auto-size: smaller dots when more columns
    if display_cols <= 15:
        sz = "12px"
    elif display_cols <= 25:
        sz = "8px"
    elif display_cols <= 40:
        sz = "6px"
    else:
        sz = "4px"
    ui = []
    for c in range(start_col, num_cols):
        has_any = any(grid.get((r, c)) for r in range(6))
        if not has_any:
            continue
        cells = []
        for r in range(6):
            val = grid.get((r, c))
            if val:
                cells.append(_dot(CM.get(val, "#999999"), sz))
            else:
                cells.append(_empty(sz))
        ui.append({"type": "box", "layout": "vertical", "contents": cells, "spacing": "none", "flex": 0, "width": sz, "alignItems": "center"})
    ui.append({"type": "filler"})
    grid_box = {
        "type": "box", "layout": "horizontal", "contents": ui, "spacing": "none",
        "paddingAll": "xs", "backgroundColor": "#F8F9FA", "cornerRadius": "md"
    }
    return _section("大路", grid_box)

def build_derived_road_ui(title, flat, style="hollow", max_display=40, use_dot=False):
    if not flat:
        return _section(title, {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": "數據不足", "size": "xxs", "color": "#cccccc"}
        ]})
    cols = _derived_to_cols(flat)
    if not cols:
        return _section(title, {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": "數據不足", "size": "xxs", "color": "#cccccc"}
        ]})
    display_cols = cols[-max_display:] if len(cols) > max_display else cols
    max_rows = min(max(len(c) for c in display_cols), 12)
    sz = "5px"
    if use_dot:
        cell_fn = lambda x: _dot(DM.get(x, "#999999"), sz)
    elif style == "hollow":
        cell_fn = lambda x: _hollow(DM.get(x, "#999999"), sz)
    elif style == "dot":
        cell_fn = lambda x: _dot(DM.get(x, "#999999"), sz)
    else:
        cell_fn = lambda x: _slash(DM.get(x, "#999999"), sz)
    # Truncate columns to max_rows
    truncated = [c[:max_rows] for c in display_cols]
    return _section(title, _grid(truncated, max_rows, cell_fn, sz))

# ==================== Flex 構建 ====================
def build_analysis_flex(room, history, total_counts=None, profit_info=None, _out_res=None):
    big_road_grid = compute_big_road(history)
    big_road_cols = compute_big_road_cols(history)
    big_eye, small_r, cockroach = compute_derived_roads(big_road_cols)
    res = baccarat_ai_logic(history, big_eye, small_r, cockroach, total_counts=total_counts)
    if _out_res is not None:
        _out_res.update(res)
    reason_text = res.get("理由", "")
    if total_counts:
        tb = total_counts.get('莊', 0)
        tp = total_counts.get('閒', 0)
        tt = total_counts.get('和', 0)
        stats = f"莊:{tb}  閒:{tp}  和:{tt}  總:{tb+tp+tt}"
    else:
        c = Counter(history)
        stats = f"莊:{c.get('莊', 0)}  閒:{c.get('閒', 0)}  和:{c.get('和', 0)}  總:{sum(c.values())}"
    bet_text = res.get('建議開倉', res.get('建議注碼', '1單位'))
    accuracy = res.get('精準度', 0)
    pred = [
        {"type": "text", "text": f"🎯 預測：{res['下注']}", "weight": "bold", "size": "xl", "color": "#D35400", "align": "center"},
        {"type": "text", "text": f"信心：{res['勝率']}%  |  注碼：{bet_text}", "size": "sm", "align": "center", "color": "#1E8449"},
        {"type": "text", "text": f"🧠 AI精準度：{accuracy}%  |  {stats}", "size": "xxs", "color": "#666666", "align": "center", "margin": "xs"}
    ]
    if profit_info:
        pi = profit_info
        profit_color = "#1E8449" if pi["total_profit"] >= 0 else "#C0392B"
        round_color = "#1E8449" if pi.get("round_profit", 0) >= 0 else "#C0392B"
        profit_lines = []
        if "round_text" in pi:
            profit_lines.append({"type": "text", "text": pi["round_text"], "size": "xs", "color": round_color, "align": "center", "wrap": True})
        profit_lines.append({"type": "text", "text": f"💰 累計損益：{pi['total_profit']:+,.0f}  |  勝率：{(pi['wins']/max(pi['wins']+pi['losses'],1)*100):.0f}% ({pi['wins']}W{pi['losses']}L)  |  共{pi['rounds']}局", "size": "xxs", "color": profit_color, "align": "center", "wrap": True})
        pred.append({"type": "separator", "margin": "xs", "color": "#DDDDDD"})
        pred.extend(profit_lines)
    if reason_text:
        pred.append({"type": "text", "text": reason_text, "size": "xxs", "color": "#888888", "align": "start", "wrap": True, "margin": "xs"})
    hdr = {
        "type": "box", "layout": "vertical", "backgroundColor": "#1A5276", "paddingAll": "sm",
        "contents": [{"type": "text", "text": "新紀元百家 AI 分析", "color": "#ffffff", "weight": "bold", "size": "md", "align": "center"}]
    }
    footer_btns = [
        {"type": "button", "action": {"type": "message", "label": "清除", "text": f"清除數據:{room}"}, "style": "secondary", "height": "sm"},
        {"type": "button", "action": {"type": "message", "label": "返回", "text": "返回主選單"}, "style": "primary", "color": "#1A5276", "height": "sm"}
    ]
    if profit_info:
        footer_btns.insert(1, {"type": "button", "action": {"type": "message", "label": "結算", "text": "結算"}, "style": "secondary", "height": "sm"})
    footer = {
        "type": "box", "layout": "horizontal", "spacing": "sm",
        "contents": footer_btns
    }
    pred_box = {"type": "box", "layout": "vertical", "margin": "xs", "backgroundColor": "#FDF2E9", "paddingAll": "sm", "cornerRadius": "md", "contents": pred}
    info_line = {"type": "text", "text": f"房號：{room} | 模式：{res['模式']}", "size": "xxs", "color": "#888888"}
    # Progressive size reduction: reduce bead + big road columns until under 29KB
    attempts = [
        (15, 80),  # full
        (15, 50),
        (15, 30),
        (10, 30),
        (8, 25),
        (6, 20),
    ]
    for bead_cols, br_cols in attempts:
        bubble1 = {
            "type": "bubble", "size": "giga",
            "header": hdr,
            "body": {
                "type": "box", "layout": "vertical", "spacing": "none", "paddingAll": "xs",
                "contents": [info_line, build_bead_road(history, bead_cols), build_big_road_ui(big_road_grid, br_cols), pred_box]
            },
            "footer": footer
        }
        b1_size = len(json.dumps(bubble1, ensure_ascii=False))
        print(f"[DEBUG] bubble1={b1_size} (bead={bead_cols}, big_road={br_cols})")
        if b1_size < 29000:
            break
    return {"type": "flex", "altText": "AI分析報告", "contents": bubble1}

def build_slot_flex(room, res):
    return {
        "type": "flex", "altText": "電子預測報告",
        "contents": {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": "#2C3E50", "contents": [
                {"type": "text", "text": "電子數據分析系統", "color": "#ffffff", "weight": "bold", "size": "md", "align": "center"}
            ]},
            "body": {"type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": f"機台房號：{room} | RTP: {FIXED_RTP}%", "size": "xxs", "color": "#888888", "margin": "sm"},
                {"type": "box", "layout": "vertical", "margin": "lg", "backgroundColor": "#F4F6F7", "paddingAll": "md", "cornerRadius": "md", "contents": [
                    {"type": "text", "text": res['level'], "weight": "bold", "size": "lg", "color": res['color'], "align": "center"},
                    {"type": "text", "text": res['desc'], "size": "xs", "wrap": True, "align": "center", "margin": "xs", "color": "#333333"}
                ]}
            ]},
            "footer": {"type": "box", "layout": "vertical", "contents": [
                {"type": "button", "action": {"type": "message", "label": "返回主選單", "text": "返回主選單"}, "style": "primary", "color": "#2C3E50"}
            ]}
        }
    }

# ==================== LINE 回覆 ====================
def line_reply(reply_token, payload):
    MENU_QUICK_REPLY = {"items": [
        {"type": "action", "action": {"type": "message", "label": "計算獲利", "text": "計算獲利"}},
        {"type": "action", "action": {"type": "message", "label": "百家預測", "text": "百家預測"}},
        {"type": "action", "action": {"type": "message", "label": "電子預測", "text": "電子預測"}},
        {"type": "action", "action": {"type": "message", "label": "儲值", "text": "儲值"}}
    ]}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
    if isinstance(payload, list):
        msgs = payload
    elif isinstance(payload, dict):
        msgs = [payload]
    else:
        msgs = [{"type": "text", "text": str(payload)}]
    if msgs:
        last = msgs[-1]
        if "quickReply" not in last:
            last["quickReply"] = MENU_QUICK_REPLY
    resp = requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, json={"replyToken": reply_token, "messages": msgs})
    if resp.status_code != 200:
        print(f"[LINE API ERROR] {resp.status_code}: {resp.text[:300]}")
    else:
        print(f"[LINE API OK] sent {len(msgs)} msg(s)")

def sys_bubble(text, quick_reply_items=None):
    bubble = {
        "type": "flex", "altText": text[:40],
        "contents": {
            "type": "bubble", "size": "kilo",
            "body": {
                "type": "box", "layout": "vertical",
                "backgroundColor": "#F7F9FA",
                "borderColor": "#D5D8DC", "borderWidth": "1px", "cornerRadius": "lg",
                "paddingAll": "lg",
                "contents": [{"type": "text", "text": text, "wrap": True, "size": "sm", "color": "#2C3E50", "align": "center"}]
            }
        }
    }
    if quick_reply_items:
        bubble["quickReply"] = {"items": quick_reply_items}
    return bubble

def text_with_back(text):
    return sys_bubble(text, [{"type": "action", "action": {"type": "message", "label": "↩ 返回主選單", "text": "返回主選單"}}])

# ==================== 輔助功能 ====================
def send_main_menu(tk):
    line_reply(tk, sys_bubble("--- 新紀元 AI 系統 ---", [
        {"type": "action", "action": {"type": "message", "label": "計算獲利", "text": "計算獲利"}},
        {"type": "action", "action": {"type": "message", "label": "百家預測", "text": "百家預測"}},
        {"type": "action", "action": {"type": "message", "label": "電子預測", "text": "電子預測"}},
        {"type": "action", "action": {"type": "message", "label": "儲值", "text": "儲值"}}
    ]))

def get_access_status(uid):
    if uid in ADMIN_UIDS:
        return "active", "永久"
    user = user_access_data.get(uid)
    if not user:
        return "none", ""
    expiry = datetime.fromisoformat(user["expiry_date"].replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)
    if now < expiry:
        diff = expiry - now
        return "active", f"{diff.days}天 {diff.seconds // 3600}時"
    return "expired", ""

def use_time_card(uid, code):
    with time_cards_data_lock:
        active = time_cards_data.get("active_cards", {})
        if code not in active:
            return False, "❌ 序號無效"
        dur_str = active[code]["duration"]
        val = int(''.join(filter(str.isdigit, dur_str)))
        now = datetime.now(timezone.utc)
        current_expiry = datetime.fromisoformat(
            user_access_data.get(uid, {"expiry_date": now.isoformat()})["expiry_date"].replace('Z', '+00:00')
        )
        base_time = max(now, current_expiry)
        if 'M' in dur_str:
            delta = timedelta(minutes=val)
        elif 'H' in dur_str:
            delta = timedelta(hours=val)
        else:
            delta = timedelta(days=val)
        new_expiry = (base_time + delta).isoformat().replace("+00:00", "Z")
        user_access_data[uid] = {"expiry_date": new_expiry}
        time_cards_data.setdefault("used_cards", {})[code] = active.pop(code)
        save_data(USER_DATA_FILE, user_access_data)
        save_data(TIME_CARDS_FILE, time_cards_data)
        return True, f"✅ 儲值成功！有效期至：\n{new_expiry[:16]}"

# ==================== Webhook 入口 ====================
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    if not verify_signature(body, signature):
        abort(400)

    data = request.json
    for event in data.get("events", []):
        # 處理 follow 事件 (新用戶加入)
        if event["type"] == "follow":
            uid = event["source"]["userId"]
            tk = event["replyToken"]
            print(f"[FOLLOW] new user: {uid[-6:]}")
            send_main_menu(tk)
            continue
        if event["type"] != "message" or "text" not in event["message"]:
            continue
        uid = event["source"]["userId"]
        tk = event["replyToken"]
        msg = event["message"]["text"].strip()
        print(f"[RECV] uid={uid[-6:]}, msg={msg}, mode={chat_modes.get(uid)}")

        # 1. 基礎指令
        if msg.upper() in ["UID", "查詢ID", "我的ID"]:
            line_reply(tk, sys_bubble(f"📋 您的 UID：\n{uid}"))
            continue

        if uid in ADMIN_UIDS and msg.startswith("產生序號"):
            try:
                _, duration, count = msg.split()
                dur_key = duration.upper()
                if dur_key not in VALID_DURATIONS:
                    valid_list = "\n".join([f"  {k} = {v}" for k, v in VALID_DURATIONS.items()])
                    line_reply(tk, sys_bubble(f"⚠️ 無效期限【{duration}】\n\n可用期限：\n{valid_list}\n\n格式：產生序號 [期限] [數量]"))
                    continue
                codes = []
                with time_cards_data_lock:
                    for _ in range(int(count)):
                        code = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=10))
                        time_cards_data["active_cards"][code] = {"duration": dur_key, "created_at": datetime.now(timezone.utc).isoformat()}
                        codes.append(code)
                    save_data(TIME_CARDS_FILE, time_cards_data)
                line_reply(tk, [
                    sys_bubble(f"✅ 已產生 {count} 組【{VALID_DURATIONS[dur_key]}】序號："),
                    {"type": "text", "text": "\n".join(codes)}
                ])
            except:
                line_reply(tk, sys_bubble("⚠️ 格式錯誤：產生序號 [期限] [數量]\n\n可用：10M / 1H / 2D / 7D / 12D / 30D"))
            continue

        if msg == "返回主選單":
            chat_modes.pop(uid, None)
            baccarat_history_dict.pop(uid, None)
            profit_tracker.pop(uid, None)
            send_main_menu(tk)
            continue

        if "清除數據" in msg and (":" in msg or "：" in msg):
            room = msg.replace("：", ":").split(":")[-1].strip()
            if uid in baccarat_history_dict and room in baccarat_history_dict[uid]:
                baccarat_history_dict[uid][room] = []
                baccarat_history_dict[uid].pop(f"{room}_total", None)
            clear_msg = f"✅ {room} 牌路已清除"
            if uid in profit_tracker:
                clear_msg += "\n\n💰 獲利計算仍持續中\n請繼續輸入開牌結果"
            line_reply(tk, text_with_back(clear_msg))
            continue

        # 2. 狀態機與功能入口
        mode = chat_modes.get(uid)
        status, left = get_access_status(uid)

        # --- 電子預測 ---
        if msg == "電子預測":
            if status == "active":
                chat_modes[uid] = "slot_choose_game"
                line_reply(tk, sys_bubble("🎰 請選擇電子遊戲：", [
                    {"type": "action", "action": {"type": "message", "label": "賽特1", "text": "選遊戲:賽特1"}},
                    {"type": "action", "action": {"type": "message", "label": "賽特2", "text": "選遊戲:賽特2"}}
                ]))
            else:
                line_reply(tk, sys_bubble("❌ 權限不足，請先儲值。"))
            continue

        elif mode == "slot_choose_game" and msg.startswith("選遊戲:"):
            game_name = msg.split(":")[-1]
            chat_modes[uid] = {"state": "slot_choose_room", "game": game_name}
            line_reply(tk, text_with_back(f"✅ 已選 {game_name}\n請輸入房號 (1~3000)：\n例如：888"))
            continue

        elif isinstance(mode, dict) and mode.get("state") == "slot_choose_room":
            chat_modes[uid] = {"state": "slot_input_bet", "game": mode["game"], "room": msg}
            line_reply(tk, text_with_back(f"✅ 已鎖定：{mode['game']} 房號 {msg}\n\n第一步：請輸入【今日總下注額】"))
            continue

        elif isinstance(mode, dict) and mode.get("state") == "slot_input_bet":
            try:
                bet = float(msg)
                chat_modes[uid] = {"state": "slot_input_rate", "game": mode["game"], "room": mode["room"], "total_bet": bet}
                line_reply(tk, text_with_back(f"💰 總下注額已設定：{bet:,.0f}\n\n第二步：請輸入【今日得分率】\n(例如：48)"))
            except:
                line_reply(tk, sys_bubble("⚠️ 格式錯誤，請輸入純數字下注額。"))
            continue

        elif isinstance(mode, dict) and mode.get("state") == "slot_input_rate":
            try:
                rate = float(msg)
                total_bet = mode["total_bet"]
                room_display = f"{mode['game']} 房號:{mode['room']}"
                res = calculate_slot_logic(total_bet, rate)
                line_reply(tk, build_slot_flex(room_display, res))
                chat_modes[uid] = {"state": "slot_input_bet", "game": mode["game"], "room": mode["room"]}
            except:
                line_reply(tk, sys_bubble("⚠️ 格式錯誤，請輸入純數字得分率。"))
            continue

        # --- 計算獲利 ---
        if msg == "計算獲利":
            if status == "active":
                chat_modes[uid] = {"state": "profit_input_unit"}
                line_reply(tk, text_with_back("💰 計算獲利模式\n\n請輸入您的【1單位金額】：\n(例如：100)\n\n設定後請進入百家預測，系統會自動根據AI建議注碼幫您計算每局損益"))
            else:
                line_reply(tk, sys_bubble("❌ 權限不足，請先儲值。"))
            continue

        elif isinstance(mode, dict) and mode.get("state") == "profit_input_unit":
            try:
                unit = float(msg)
                if unit <= 0:
                    raise ValueError
                profit_tracker[uid] = {
                    "unit": unit, "total_profit": 0, "rounds": 0,
                    "wins": 0, "losses": 0, "last_prediction": None
                }
                chat_modes.pop(uid, None)
                line_reply(tk, sys_bubble(
                    f"✅ 獲利計算已啟動\n\n"
                    f"🎯 1單位金額：{unit:,.0f}\n\n"
                    f"請選擇遊戲館開始遊戲\n"
                    f"每局開牌後系統會自動計算損益\n\n"
                    f"輸入【結算】可查看完整報表\n"
                    f"輸入【關閉獲利】停止計算",
                    [
                        {"type": "action", "action": {"type": "message", "label": "百家預測", "text": "百家預測"}},
                        {"type": "action", "action": {"type": "message", "label": "電子預測", "text": "電子預測"}},
                        {"type": "action", "action": {"type": "message", "label": "↩ 返回主選單", "text": "返回主選單"}}
                    ]
                ))
            except:
                line_reply(tk, sys_bubble("⚠️ 請輸入正確的數字金額"))
            continue

        if msg == "結算" and uid in profit_tracker:
            pt = profit_tracker[uid]
            rpt = (
                f"📊 獲利結算報表\n"
                f"{'='*20}\n"
                f"🎯 單位金額：{pt['unit']:,.0f}\n"
                f"{'='*20}\n"
                f"📈 總損益：{pt['total_profit']:+,.0f}\n"
                f"{'='*20}\n"
                f"🎮 總局數：{pt['rounds']}\n"
                f"✅ 贏：{pt['wins']}局\n"
                f"❌ 輸：{pt['losses']}局\n"
                f"➖ 和：{pt['rounds'] - pt['wins'] - pt['losses']}局\n"
                f"📊 勝率：{(pt['wins']/max(pt['wins']+pt['losses'],1)*100):.1f}%\n"
            )
            profit_tracker.pop(uid, None)
            line_reply(tk, sys_bubble(rpt))
            continue

        if msg == "關閉獲利" and uid in profit_tracker:
            profit_tracker.pop(uid, None)
            line_reply(tk, sys_bubble("✅ 獲利計算已關閉"))
            continue

        # --- 百家預測 ---
        if msg == "百家預測":
            if status == "active":
                chat_modes[uid] = "choose_provider"
                BASE_URL = "https://bc-line-kmh9.onrender.com"
                provider_flex = {
                    "type": "flex", "altText": "請選擇平台",
                    "contents": {
                        "type": "bubble", "size": "mega",
                        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1A5276", "paddingAll": "md", "contents": [
                            {"type": "text", "text": "🎲 請選擇遊戲平台", "color": "#ffffff", "weight": "bold", "size": "lg", "align": "center"},
                            {"type": "text", "text": f"🔑 授權剩餘：{left}", "color": "#AED6F1", "size": "xs", "align": "center", "margin": "xs"}
                        ]},
                        "body": {"type": "box", "layout": "horizontal", "spacing": "lg", "paddingAll": "lg", "contents": [
                            {"type": "box", "layout": "vertical", "flex": 1, "cornerRadius": "lg", "backgroundColor": "#F8F9FA", "paddingAll": "md", "contents": [
                                {"type": "image", "url": f"{BASE_URL}/static/MT.jpg", "size": "full", "aspectRatio": "1:1", "aspectMode": "cover"},
                                {"type": "text", "text": "MT真人", "weight": "bold", "size": "md", "align": "center", "margin": "sm", "color": "#2C3E50"},
                            ], "action": {"type": "message", "label": "MT真人", "text": "平台:MT"}},
                            {"type": "box", "layout": "vertical", "flex": 1, "cornerRadius": "lg", "backgroundColor": "#F8F9FA", "paddingAll": "md", "contents": [
                                {"type": "image", "url": f"{BASE_URL}/static/DG.jpg", "size": "full", "aspectRatio": "1:1", "aspectMode": "cover"},
                                {"type": "text", "text": "DG真人", "weight": "bold", "size": "md", "align": "center", "margin": "sm", "color": "#2C3E50"},
                            ], "action": {"type": "message", "label": "DG真人", "text": "平台:DG"}}
                        ]},
                        "footer": {"type": "box", "layout": "vertical", "contents": [
                            {"type": "button", "action": {"type": "message", "label": "↩ 返回主選單", "text": "返回主選單"}, "style": "primary", "color": "#1A5276", "height": "sm"}
                        ]}
                    }
                }
                line_reply(tk, provider_flex)
            else:
                line_reply(tk, sys_bubble("❌ 權限已過期或未開通。"))
            continue

        elif mode == "choose_provider" and msg.startswith("平台:"):
            p_name = "MT真人" if "MT" in msg else "DG真人"
            if "MT" in msg:
                chat_modes[uid] = {"state": "mt_choose_category", "p": p_name}
                BASE_URL = "https://bc-line-kmh9.onrender.com"
                cat_flex = {
                    "type": "flex", "altText": "MT真人 - 選擇遊戲廳",
                    "contents": {
                        "type": "bubble", "size": "mega",
                        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1A5276", "paddingAll": "md", "contents": [
                            {"type": "box", "layout": "horizontal", "contents": [
                                {"type": "image", "url": f"{BASE_URL}/static/MT.jpg", "size": "xxs", "aspectRatio": "1:1", "aspectMode": "cover", "flex": 0},
                                {"type": "box", "layout": "vertical", "flex": 4, "paddingStart": "md", "contents": [
                                    {"type": "text", "text": "MT真人", "color": "#ffffff", "weight": "bold", "size": "lg"},
                                    {"type": "text", "text": "請選擇遊戲廳", "color": "#AED6F1", "size": "xs"}
                                ]}
                            ]}
                        ]},
                        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "lg", "contents": [
                            {"type": "button", "action": {"type": "message", "label": "🎲 百家樂 - 亞洲廳", "text": "MT廳:亞洲廳"}, "style": "primary", "color": "#2E86C1", "height": "sm"},
                            {"type": "button", "action": {"type": "message", "label": "🎲 百家樂 - 國際廳（敬請期待）", "text": "MT廳:國際廳"}, "style": "secondary", "height": "sm"},
                            {"type": "button", "action": {"type": "message", "label": "↩ 返回主選單", "text": "返回主選單"}, "style": "secondary", "height": "sm"}
                        ]}
                    }
                }
                line_reply(tk, cat_flex)
            else:
                chat_modes[uid] = {"state": "choose_room", "p": p_name}
                line_reply(tk, text_with_back(f"✅ 已選擇 {p_name}\n\n請直接輸入房號：\n(例如：A01、C03、D05、RB01、S06)"))
            continue

        elif isinstance(mode, dict) and mode.get("state") == "mt_choose_category" and msg.startswith("MT廳:"):
            category = msg.replace("MT廳:", "")
            chat_modes[uid] = {"state": "choose_room", "p": "MT真人"}

            # 顏色設定
            color_map = {"亞洲廳": "#2E86C1", "國際廳": "#1A5276"}
            header_color = color_map.get(category, "#2E86C1")
            emoji = "🎲"

            # ── 靜態房間列表 ──
            if category == "亞洲廳":
                row1 = [{"type": "button", "action": {"type": "message", "label": f"百家樂{i}", "text": f"百家樂{i}"}, "style": "primary", "color": "#2E86C1", "height": "sm"} for i in range(1, 6)]
                row2 = [{"type": "button", "action": {"type": "message", "label": f"百家樂{i}", "text": f"百家樂{i}"}, "style": "primary", "color": "#2E86C1", "height": "sm"} for i in range(6, 14)]
                body_items = row1 + row2 + [
                    {"type": "button", "action": {"type": "message", "label": "百家樂3A", "text": "百家樂3A"}, "style": "secondary", "height": "sm"},
                    {"type": "button", "action": {"type": "message", "label": "↩ 返回主選單", "text": "返回主選單"}, "style": "secondary", "height": "sm"}
                ]
            elif category == "國際廳":
                body_items = [
                    {"type": "text", "text": "🚧 國際廳即將開放，敬請期待！", "size": "sm", "color": "#888888", "align": "center", "wrap": True},
                    {"type": "button", "action": {"type": "message", "label": "↩ 返回主選單", "text": "返回主選單"}, "style": "secondary", "height": "sm"}
                ]
            else:
                body_items = [{"type": "button", "action": {"type": "message", "label": "↩ 返回主選單", "text": "返回主選單"}, "style": "secondary", "height": "sm"}]
            room_flex = {
                "type": "flex", "altText": f"MT{category} - 選擇房間",
                "contents": {
                    "type": "bubble", "size": "mega",
                    "header": {"type": "box", "layout": "vertical", "backgroundColor": header_color, "paddingAll": "sm", "contents": [
                        {"type": "text", "text": f"{emoji} MT真人 - {category}", "color": "#ffffff", "weight": "bold", "size": "md", "align": "center"}
                    ]},
                    "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "md", "contents": body_items}
                }
            }
            line_reply(tk, room_flex)
            continue

        elif isinstance(mode, dict) and mode.get("state") == "choose_room":
            room_name = msg.replace("房號:", "").strip()
            if mode.get("p") == "MT真人":
                # Normalize: add space after 百家樂 if missing
                rn = room_name
                if rn.startswith("百家樂") and len(rn) > 3 and rn[3] != " ":
                    rn = "百家樂 " + rn[3:]
                room_name = rn
                mt_valid = [f"百家樂 {i}" for i in range(1, 14)] + ["百家樂 3A"]
                if room_name not in mt_valid:
                    line_reply(tk, text_with_back("⚠️ MT真人房號格式錯誤\n\n百家樂：百家樂1~百家樂13、百家樂3A"))
                    continue

                # ── MT真人：手動輸入模式 ──
                chat_modes[uid] = {"state": "predicting", "room": room_name}
                line_reply(tk, text_with_back(f"✅ 已選擇 {room_name}\n\n請輸入開牌結果：\n1(閒) 2(莊) 3(和)"))
                continue
            # DG 或其他平台 → 手動模式
            chat_modes[uid] = {"state": "predicting", "room": room_name}
            line_reply(tk, [
                sys_bubble(f"🔗 連線中... {room_name}"),
                text_with_back(f"✅ 已成功連線 {room_name}\n\n請輸入開牌結果：\n1(閒) 2(莊) 3(和)")
            ])
            continue

        elif isinstance(mode, dict) and mode.get("state") == "predicting":
            room = mode["room"]
            history = baccarat_history_dict.setdefault(uid, {}).setdefault(room, [])
            code_map = {"1": "閒", "2": "莊", "3": "和"}
            new_data = [code_map[c] for c in msg if c in code_map]
            print(f"[DEBUG] predicting: msg={msg}, new_data={new_data}, history_len={len(history)}")
            if new_data:
                # --- 獲利計算：用上一輪AI預測 vs 本輪實際結果 ---
                profit_info = None
                pt = profit_tracker.get(uid)
                if pt:
                    for actual in new_data:
                        last_pred = pt.get("last_prediction")
                        if last_pred:
                            bet_side = last_pred["下注"]
                            bet_text = last_pred.get("建議注碼", "1單位")
                            # 動態解析注碼數字 (支援 1~10單位)
                            try:
                                multiplier = float(''.join(c for c in bet_text.split("單位")[0] if c.isdigit() or c == '.'))
                            except:
                                multiplier = 1.0
                            bet_amount = pt["unit"] * multiplier
                            pt["rounds"] += 1
                            if actual == "和":
                                profit = 0
                                round_text = f"第{pt['rounds']}局：AI下{bet_side} {bet_amount:,.0f} → 開{actual} ➖ 和局(退注)"
                            elif actual == bet_side:
                                if bet_side == "莊":
                                    profit = bet_amount * BANKER_PAYOUT
                                else:
                                    profit = bet_amount * PLAYER_PAYOUT
                                pt["wins"] += 1
                                round_text = f"第{pt['rounds']}局：AI下{bet_side} {bet_amount:,.0f} → 開{actual} ✅ +{profit:,.0f}"
                            else:
                                profit = -bet_amount
                                pt["losses"] += 1
                                round_text = f"第{pt['rounds']}局：AI下{bet_side} {bet_amount:,.0f} → 開{actual} ❌ {profit:,.0f}"
                            pt["total_profit"] += profit
                            pt["round_text"] = round_text
                            pt["round_profit"] = profit

                history.extend(new_data)
                # Track total count before trimming
                total_key = f"{room}_total"
                room_totals = baccarat_history_dict[uid].setdefault(total_key, {"莊": 0, "閒": 0, "和": 0})
                for d in new_data:
                    if d in room_totals:
                        room_totals[d] += 1
                if len(history) > 90:
                    history = history[-90:]
                baccarat_history_dict[uid][room] = history

                # Build profit_info for display
                if pt:
                    profit_info = {
                        "total_profit": pt["total_profit"],
                        "rounds": pt["rounds"],
                        "wins": pt["wins"],
                        "losses": pt["losses"],
                        "round_profit": pt.get("round_profit", 0)
                    }
                    if "round_text" in pt:
                        profit_info["round_text"] = pt["round_text"]

                try:
                    ai_out = {} if pt else None
                    flex_msg = build_analysis_flex(room, history, room_totals, profit_info, _out_res=ai_out)
                    print(f"[DEBUG] flex built OK, size={len(json.dumps(flex_msg, ensure_ascii=False))}")
                    line_reply(tk, flex_msg)
                    # Store current AI prediction for next round's profit calculation
                    if pt and ai_out:
                        pt["last_prediction"] = ai_out
                except Exception as e:
                    print(f"[DEBUG] build_analysis_flex ERROR: {e}")
                    traceback.print_exc()
                    line_reply(tk, sys_bubble(f"⚠️ 分析錯誤：{str(e)[:100]}"))
            else:
                line_reply(tk, sys_bubble("⚠️ 請輸入 1, 2 或 3"))
            continue

        # 儲值入口
        if msg == "儲值":
            chat_modes[uid] = "input_card"
            line_reply(tk, sys_bubble("請輸入 10 位儲值序號："))
            continue

        elif mode == "input_card":
            success, result_msg = use_time_card(uid, msg.upper())
            chat_modes.pop(uid, None)
            line_reply(tk, sys_bubble(result_msg))
            continue

        # 持久選單出口
        send_main_menu(tk)

    return jsonify({"status": "ok"})

@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "sv94-bot"
    })

# gunicorn 啟動時也需要載入資料
user_access_data = load_data(USER_DATA_FILE)
time_cards_data = load_data(TIME_CARDS_FILE, {"active_cards": {}, "used_cards": {}})

if __name__ == "__main__":
    print("=== SV94 Bot 啟動成功 (port 5001) ===")
    app.run(host="0.0.0.0", port=5001)
