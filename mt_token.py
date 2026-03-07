"""
MT真人 Token 自動取得模組
使用 Playwright 自動登入遊戲平台，點擊進入 MT真人，從新視窗 URL 擷取 token。
"""

import asyncio
import os
import time
import re
import logging

logger = logging.getLogger(__name__)

# --- 快取 ---
_cached_token = None
_token_expiry = 0
TOKEN_TTL = 600  # 10 分鐘快取

async def _dismiss_popups(page, stage=""):
    """關閉平台公告彈窗（最新公告 1/3, 2/3, 3/3 等）"""
    logger.info("[%s] 嘗試關閉公告彈窗...", stage)
    dismissed = 0
    for attempt in range(10):  # 最多嘗試 10 次（多頁公告）
        found = False
        # 優先找「確認」按鈕（公告彈窗的主要按鈕）
        for sel in [
            'button:has-text("確認")',
            'text="確認"',
            'button:has-text("确认")',
            'button:has-text("我知道了")',
            'button:has-text("關閉")',
            'button:has-text("close")',
            'button:has-text("×")',
        ]:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.evaluate("el => el.click()")
                    found = True
                    dismissed += 1
                    logger.info("[%s] 已點擊: %s (第%d次)", stage, sel, dismissed)
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        if not found:
            # 嘗試 Escape
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
            # 再檢查一次是否還有遮罩
            try:
                overlay = await page.query_selector('div[class*="fixed"][class*="bg:rgba"]')
                if overlay and await overlay.is_visible():
                    await overlay.evaluate("el => el.click()")
                    await page.wait_for_timeout(1000)
                    continue
            except Exception:
                pass
            break

    logger.info("[%s] 公告彈窗處理完成，共關閉 %d 個", stage, dismissed)


async def _safe_screenshot(page, path):
    """Safe screenshot that won't crash on timeout (Render headless)"""
    try:
        await page.screenshot(path=path, timeout=5000)
    except Exception:
        pass


async def _fetch_mt_token_playwright():
    """用 Playwright 自動登入平台並取得 MT token"""
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/.cache/pw-browsers")
    from playwright.async_api import async_playwright

    platform_url = os.getenv("GR_PLATFORM_URL", "https://seofufan.seogrwin1688.com/")
    username = os.getenv("GR_USERNAME", "")
    password = os.getenv("GR_PASSWORD", "")

    if not username or not password:
        raise RuntimeError("GR_USERNAME / GR_PASSWORD 未設定")

    token = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )

        page = await context.new_page()

        # ── 1. 前往平台首頁 ──
        logger.info("前往平台首頁: %s", platform_url)
        await page.goto(platform_url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        # ── 2. 關閉可能的公告彈窗（登入前） ──
        await _safe_screenshot(page, "debug_01_before_dismiss.png")
        await _dismiss_popups(page, "登入前")

        # ── 3. 登入 ──
        logger.info("嘗試登入 (%s)", username)

        # 找帳號/密碼輸入框 (Vue SPA，用 placeholder 或 type 定位)
        # 嘗試多種選擇器
        account_sel = None
        for sel in [
            'input[placeholder*="帳號"]',
            'input[placeholder*="账号"]',
            'input[placeholder*="account"]',
            'input[placeholder*="Account"]',
            'input[name="account"]',
            'input[type="text"]:first-of-type',
        ]:
            if await page.query_selector(sel):
                account_sel = sel
                break

        pwd_sel = None
        for sel in [
            'input[placeholder*="密碼"]',
            'input[placeholder*="密码"]',
            'input[placeholder*="password"]',
            'input[placeholder*="Password"]',
            'input[name="password"]',
            'input[type="password"]',
        ]:
            if await page.query_selector(sel):
                pwd_sel = sel
                break

        if not account_sel or not pwd_sel:
            # 截圖輔助 debug
            await _safe_screenshot(page, "debug_login_page.png")
            await browser.close()
            raise RuntimeError("找不到帳號或密碼輸入框")

        await page.fill(account_sel, username)
        await page.fill(pwd_sel, password)
        await page.wait_for_timeout(500)

        # 點登入按鈕
        login_btn = None
        for sel in [
            'button:has-text("登入")',
            'button:has-text("登录")',
            'button:has-text("Sign In")',
            'button:has-text("LOGIN")',
            'button[type="submit"]',
        ]:
            if await page.query_selector(sel):
                login_btn = sel
                break

        if not login_btn:
            await _safe_screenshot(page, "debug_login_btn.png")
            await browser.close()
            raise RuntimeError("找不到登入按鈕")

        # 使用 JavaScript 點擊來避免遮罩阻擋
        btn_el = await page.query_selector(login_btn)
        if btn_el:
            await btn_el.evaluate("el => el.click()")
        else:
            await page.click(login_btn)
        logger.info("已點擊登入按鈕，等待登入完成...")
        await page.wait_for_timeout(5000)
        await _safe_screenshot(page, "debug_03_after_login.png")

        # ── 4. 關閉登入後的公告彈窗（最新公告 1/3, 2/3, 3/3） ──
        await _dismiss_popups(page, "登入後")

        # ── 5. 設置網路攔截 & 新視窗監聽 ──
        token_future = asyncio.get_event_loop().create_future()

        # 攔截 API 回應中含 token 的 URL（平台呼叫遊戲啟動 API 時）
        async def on_response(response):
            try:
                url = response.url
                # 攔截遊戲啟動 API (通常回傳含 token 的 game URL)
                if "game" in url.lower() or "launch" in url.lower() or "login" in url.lower():
                    if response.status == 200:
                        try:
                            body = await response.json()
                            body_str = str(body)
                            m = re.search(r'token=([a-fA-F0-9]{32})', body_str)
                            if m and not token_future.done():
                                logger.info("從 API 回應攔截到 token: %s", url)
                                token_future.set_result(m.group(1))
                        except Exception:
                            try:
                                body_text = await response.text()
                                m = re.search(r'token=([a-fA-F0-9]{32})', body_text)
                                if m and not token_future.done():
                                    logger.info("從 API 文字回應攔截到 token: %s", url)
                                    token_future.set_result(m.group(1))
                            except Exception:
                                pass
            except Exception:
                pass

        page.on("response", on_response)

        # 監聽新視窗
        def on_new_page(new_page):
            async def _extract():
                try:
                    await new_page.wait_for_load_state("domcontentloaded", timeout=15000)
                    url = new_page.url
                    logger.info("新視窗 URL: %s", url)
                    m = re.search(r'[?&]token=([a-fA-F0-9]{32})', url)
                    if m and not token_future.done():
                        token_future.set_result(m.group(1))
                except Exception as e:
                    logger.warning("新視窗處理失敗: %s", e)
            asyncio.ensure_future(_extract())

        context.on("page", on_new_page)

        # ── 6. 點擊「真人視訊」導航頁籤 ──
        logger.info("嘗試點擊「真人視訊」導航...")
        # 用 page.locator 精確定位導航欄的「真人視訊」
        try:
            # 導航欄中的真人視訊連結
            nav_live = page.locator('a:has-text("真人視訊"), [role="tab"]:has-text("真人視訊")')
            if await nav_live.count() > 0:
                await nav_live.first.click(timeout=5000)
                logger.info("已透過 locator 點擊「真人視訊」")
            else:
                # 備用：用 JS 找精確文字
                clicked = await page.evaluate('''() => {
                    const links = document.querySelectorAll('a, div[class*="nav"], span');
                    for (const el of links) {
                        if (el.textContent.trim() === '真人視訊' || el.textContent.trim() === '真人视讯') {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }''')
                if clicked:
                    logger.info("已透過 JS 精確匹配點擊「真人視訊」")
                else:
                    logger.warning("找不到「真人視訊」導航")
        except Exception as e:
            logger.warning("點擊真人視訊失敗: %s", e)

        await page.wait_for_timeout(3000)
        await _safe_screenshot(page, "debug_05_after_live.png")

        # ── 7. 點擊「MT真人」遊戲卡片 ──
        logger.info("嘗試點擊「MT真人」遊戲...")
        # 先滾動頁面讓遊戲卡片可見
        await page.evaluate("window.scrollBy(0, 300)")
        await page.wait_for_timeout(1000)

        mt_clicked = False
        # 方法1：用 JS 精確找包含 "MT" 文字的可點擊元素
        mt_clicked = await page.evaluate('''() => {
            // 找所有含 "MT" 的元素，優先找按鈕/連結/圖片
            const candidates = document.querySelectorAll('div, span, a, button, img');
            let best = null;
            for (const el of candidates) {
                const text = el.textContent || el.alt || '';
                if (text.includes('MT真人') || text.includes('MT 真人')) {
                    // 優先找較小的元素（更精確的匹配）
                    if (!best || el.textContent.length < best.textContent.length) {
                        best = el;
                    }
                }
            }
            if (best) {
                best.click();
                return true;
            }
            // 備用：找 img alt 含 MT 的
            const imgs = document.querySelectorAll('img');
            for (const img of imgs) {
                if ((img.alt || '').toUpperCase().includes('MT')) {
                    img.parentElement.click();
                    return true;
                }
            }
            return false;
        }''')

        if mt_clicked:
            logger.info("已透過 JS 點擊「MT真人」")
        else:
            await _safe_screenshot(page, "debug_06_mt_btn.png")
            logger.warning("JS 精確匹配未找到 MT真人，嘗試其他方法...")
            # 方法2：用 Playwright locator
            try:
                mt_loc = page.locator('text="MT真人"')
                if await mt_loc.count() > 0:
                    await mt_loc.first.click(timeout=5000)
                    mt_clicked = True
                    logger.info("已透過 locator 點擊「MT真人」")
            except Exception:
                pass

        if not mt_clicked:
            await _safe_screenshot(page, "debug_07_no_mt.png")
            await browser.close()
            raise RuntimeError("找不到 MT真人 按鈕")

        await page.wait_for_timeout(3000)
        await _safe_screenshot(page, "debug_08_after_mt_click.png")

        # ── 8. 等待 token（從新視窗或 API 回應） ──
        try:
            token = await asyncio.wait_for(token_future, timeout=25)
            logger.info("成功取得 MT Token: %s...", token[:8])
        except (asyncio.TimeoutError, TimeoutError):
            # 檢查所有已開的頁面
            all_pages = context.pages
            for p2 in all_pages:
                url = p2.url
                logger.info("檢查頁面 URL: %s", url[:100])
                m = re.search(r'[?&]token=([a-fA-F0-9]{32})', url)
                if m:
                    token = m.group(1)
                    break

            if not token:
                try:
                    await page.screenshot(path="debug_no_token.png", timeout=5000)
                except Exception:
                    pass  # 截圖失敗不影響錯誤回報
                await browser.close()
                raise RuntimeError("無法取得 MT Token（超時）")

        await browser.close()

    return token


async def get_mt_token(force_refresh=False):
    """取得 MT Token（帶快取）"""
    global _cached_token, _token_expiry

    if not force_refresh and _cached_token and time.time() < _token_expiry:
        logger.info("使用快取 MT Token: %s...", _cached_token[:8])
        return _cached_token

    logger.info("重新取得 MT Token...")
    token = await _fetch_mt_token_playwright()
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
    # 讀取 .env
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
    print(f"MT URL: https://gsa.ofalive99.net/?token={token}&lang=zhtw")
