# -*- coding: utf-8 -*-
"""
京都府警 運転免許学科試験 予約空き状況モニター

毎回ブラウザを新規に起動し、
  同意 -> 学科試験 -> 原付以外 -> 運転免許試験場 -> 予約者情報入力(あなた本人の情報) -> カレンダー
まで自動で進んで、config.json の target_dates で指定した日付の空き状況を確認する。
「満席 -> 空きあり」に変わったときだけ Discord に通知する(毎回通知すると鬱陶しいため)。

このスクリプトは実際の予約は一切行わない(カレンダーで空き状況を見るだけで止める)。
入力する氏名・生年月日・本人確認番号は config.json に書いた本人のもの(偽情報は使わない)。
サイトへの負荷を避けるため、ループ実行の間隔は最短5分。
"""
import json
import logging
import argparse
import random
import time
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "monitor.log"
LOCK_PATH = BASE_DIR / "monitor.lock"
DEBUG_SHOT_PATH = BASE_DIR / "last_error.png"

TOP_URL = "https://unmen-yoyaku.police.pref.kyoto.lg.jp/menkyo-yoyaku/"
SITE_HOST = urlparse(TOP_URL).netloc
BLOCK_STATUSES = (403, 429)  # アクセス拒否 / アクセス過多。検出したら即停止する


class BlockedError(Exception):
    """サイトから 403 / 429 が返った(アクセス制限の可能性)。監視を止める合図"""

logger = logging.getLogger("menkyo_monitor")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_file_handler = logging.FileHandler(str(LOG_PATH), encoding="utf-8")
_file_handler.setFormatter(_fmt)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_fmt)
logger.addHandler(_console_handler)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def notify_discord(webhook_url, message):
    if not webhook_url or "ここに" in webhook_url:
        logger.warning("Discord Webhook URLが未設定のため通知をスキップしました。")
        return
    try:
        resp = requests.post(webhook_url, json={"content": message}, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Discord通知の送信に失敗しました: {e}")


def click_text_exact(page, text, timeout=10000):
    """表示されている文字列と完全一致する要素をクリックする"""
    # <title> など非表示要素が先に当たらないよう、表示中の要素だけを対象にし、
    # リンク/ボタンがあればそれを優先する
    clickable = page.locator(
        f"xpath=//a[normalize-space(.)='{text}'] | //button[normalize-space(.)='{text}']"
    ).filter(visible=True)
    anything = page.locator(f"xpath=//*[normalize-space(text())='{text}']").filter(visible=True)
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for loc in (clickable, anything):
            if loc.count() > 0:
                loc.first.click()
                return
        page.wait_for_timeout(200)
    raise PlaywrightTimeoutError(f"「{text}」が表示されませんでした")


def goto_calendar(page, config):
    """規約同意〜予約者情報入力〜カレンダー到達までを自動で行う"""
    page.goto(TOP_URL, wait_until="networkidle")

    page.locator("input[type=checkbox]").first.check()
    click_text_exact(page, "利用を開始する")

    click_text_exact(page, "予約登録")
    click_text_exact(page, "学科試験")
    click_text_exact(page, "原付以外")
    click_text_exact(page, "運転免許試験場")

    # 予約者情報入力(config.json の本人情報。本予約まで進まない)
    page.locator("input[type=text]").first.fill(config["kana_name"])
    page.locator("input[type=tel]").first.fill(str(config["confirm_number"]))

    selects = page.locator("select")
    selects.nth(0).select_option(value=str(config["birth_year"]))
    selects.nth(1).select_option(value=f"{int(config['birth_month']):02d}")
    selects.nth(2).select_option(value=f"{int(config['birth_day']):02d}")

    click_text_exact(page, "予約希望日時")
    page.wait_for_selector("text=受付を希望する日付を選択してください", timeout=15000)


def read_month_header(page):
    """カレンダー見出しの「2026年10月」を (年, 月) で返す。「2026年10月5日」のような日付文は除外"""
    body = page.inner_text("body")
    m = re.search(r"(\d{4})年\s*(\d{1,2})月(?!\s*\d)", body)
    if not m:
        raise RuntimeError("カレンダーの年月表示が見つかりません")
    return int(m.group(1)), int(m.group(2))


def navigate_to_month(page, target_year, target_month, max_steps=36):
    for _ in range(max_steps):
        cur_year, cur_month = read_month_header(page)
        if (cur_year, cur_month) == (target_year, target_month):
            return True
        # ボタンは全角の「＞」「＜」で、th.next / th.prev。表示中のものだけを対象にする
        css = "th.next" if (cur_year, cur_month) < (target_year, target_month) else "th.prev"
        nav = page.locator(css).filter(visible=True).first
        if nav.count() == 0:
            return False
        nav.click()
        page.wait_for_timeout(300)
    return False


def click_day_cell(page, day):
    """同じ数字が前後月のグレー表示と重複するため、文字色が一番濃いセルを選んでクリックする"""
    day_str = str(day)
    cells = page.locator(f"xpath=(//td|//button)[normalize-space(.)='{day_str}' or .//*[normalize-space(text())='{day_str}']]")
    count = cells.count()
    if count == 0:
        return False

    best_index = None
    best_luminance = None
    for i in range(count):
        cell = cells.nth(i)
        try:
            color = cell.evaluate("el => getComputedStyle(el).color")
        except Exception:
            continue
        m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", color)
        if not m:
            continue
        r, g, b = (int(x) for x in m.groups())
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        if best_luminance is None or luminance < best_luminance:
            best_luminance = luminance
            best_index = i

    if best_index is None:
        best_index = 0

    cells.nth(best_index).click()
    return True


def check_date(page, date_str):
    try:
        return _check_date(page, date_str)
    except Exception as e:
        logger.error(f"{date_str}: 取得中に例外: {e}")
        return "error", None


def _check_date(page, date_str):
    """
    戻り値: (status, slots)
    status: "available" | "full" | "closed" | "error"
    slots: [(時間帯, 残り人数), ...] (closed/errorの場合はNone)
    """
    target = datetime.strptime(date_str, "%Y-%m-%d")

    if not navigate_to_month(page, target.year, target.month):
        logger.error(f"{date_str}: 対象月への移動に失敗しました")
        return "error", None

    if not click_day_cell(page, target.day):
        logger.error(f"{date_str}: 日付セルが見つかりませんでした")
        return "error", None

    # 直前の日付の表示を誤読しないよう、本文が変化するまで待つ(最大5秒)
    try:
        page.wait_for_function(
            "([d]) => { const t = document.body.innerText; "
            "return t.includes('該当日の予約はできません') || t.includes('受付時間'); }",
            arg=[date_str], timeout=5000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(700)
    body_text = page.inner_text("body")

    if "該当日の予約はできません" in body_text:
        return "closed", None

    slots = re.findall(r"受付時間：(\d{2}:\d{2}-\d{2}:\d{2})\s*残り：(\d+)名", body_text)
    if not slots:
        logger.error(f"{date_str}: 時間帯の情報が見つかりませんでした(画面構成が変わった可能性)")
        return "error", None

    slots = [(t, int(n)) for t, n in slots]
    if any(n > 0 for _, n in slots):
        return "available", slots
    return "full", slots


def run_once(config, state):
    all_ok = True
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=config.get("headless", True))
        page = browser.new_page()

        # 予約サイト自身(同じドメイン)への通信で 403/429 が返ったら記録する
        blocked = []
        page.on("response", lambda r: blocked.append((r.status, r.url))
                if r.status in BLOCK_STATUSES and urlparse(r.url).netloc == SITE_HOST else None)

        def raise_if_blocked():
            if blocked:
                browser.close()
                raise BlockedError(f"HTTP {blocked[0][0]} を検出: {blocked[0][1]}")

        try:
            goto_calendar(page, config)
        except PlaywrightTimeoutError:
            raise_if_blocked()  # タイムアウトの原因が制限だった場合はこちらを優先
            logger.error("予約者情報入力〜カレンダー到達でタイムアウトしました。サイトの構造が変わったかもしれません。")
            try:
                page.screenshot(path=str(DEBUG_SHOT_PATH))
                logger.error(f"失敗時点のスクリーンショットを保存しました: {DEBUG_SHOT_PATH}")
            except Exception:
                pass
            browser.close()
            return state, False
        raise_if_blocked()

        for date_str in config["target_dates"]:
            status, slots = check_date(page, date_str)
            raise_if_blocked()
            prev_status = state.get(date_str, {}).get("status")
            logger.info(f"{date_str}: {status} {slots}")

            if status == "error":
                all_ok = False
                try:
                    page.screenshot(path=str(DEBUG_SHOT_PATH))
                except Exception:
                    pass
                continue

            if status == "available" and prev_status != "available":
                lines = [f"・{t}　残り{n}名" for t, n in slots if n > 0]
                message = (
                    f"京都府運転免許試験場 学科試験 {date_str} に空きが出ました！\n"
                    + "\n".join(lines)
                    + f"\n今すぐ予約してください:\n{TOP_URL}"
                )
                notify_discord(config["discord_webhook_url"], message)
                logger.info(f"Discordに通知を送信しました: {date_str}")

            state[date_str] = {
                "status": status,
                "slots": slots,
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            }

        browser.close()
    return state, all_ok


def run_cycle(config):
    state = load_state()
    new_state, ok = run_once(config, state)

    if ok:
        new_state["_fail_count"] = 0
    else:
        new_state["_fail_count"] = state.get("_fail_count", 0) + 1
        if new_state["_fail_count"] == 3:
            notify_discord(
                config["discord_webhook_url"],
                "警告: 京都府学科試験 監視ツールが3回連続で空き状況を取得できていません。"
                "サイトの画面構成が変わった可能性があるので確認してください。"
                f"(last_error.png を参照: {DEBUG_SHOT_PATH})",
            )
    save_state(new_state)


def handle_blocked(config, err):
    """403/429 を検出したときの後始末: ログと Discord 警告(監視の停止は呼び出し側で行う)"""
    logger.error(f"アクセス制限の可能性があるため監視を停止します: {err}")
    notify_discord(
        config["discord_webhook_url"],
        "【停止】京都府学科試験 監視ツールがアクセス制限(HTTP 403/429)を検出したため停止しました。\n"
        f"{err}\n"
        "しばらく(数時間〜1日)空けてから、手動で再開してください。連続で再開するとさらに制限される恐れがあります。",
    )


def acquire_lock():
    """二重起動すると負荷が倍になるため、ロックファイルで防ぐ(古いロックは3時間で無効)"""
    if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime < 3 * 3600:
        logger.error("すでに別の monitor.py が動いています(monitor.lock)。二重起動は負荷が増えるため終了します。")
        sys.exit(1)
    LOCK_PATH.write_text(str(time.time()))


def release_lock():
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, metavar="MIN",
                        help="MIN分おきに繰り返す(最短5分・推奨10分。ゆらぎ付き)。省略すると1回だけ実行")
    args = parser.parse_args()
    config = load_config()

    acquire_lock()
    try:
        if not args.loop:
            try:
                run_cycle(config)
            except BlockedError as e:
                handle_blocked(config, e)
                sys.exit(3)  # 呼び出し側(GitHub Actions など)が「制限された」と判別するための終了コード
            return

        base = max(5, args.loop)
        quiet = config.get("quiet_hours", [0, 6])  # この時間帯(開始時, 終了時)は巡回しない
        logger.info(f"ループ開始: 約{base}分おき / 停止時間帯 {quiet[0]}時-{quiet[1]}時。止めるには Ctrl+C")
        while True:
            hour = datetime.now().hour
            if quiet[0] <= hour < quiet[1]:
                time.sleep(600)
                LOCK_PATH.touch()
                continue
            try:
                run_cycle(config)
            except BlockedError as e:
                handle_blocked(config, e)
                break  # ループを抜けて終了(ロックも finally で解除される)
            except Exception as e:
                logger.error(f"巡回中に予期せぬエラー: {e}")
            fails = load_state().get("_fail_count", 0)
            # 連続失敗中(メンテ・障害・画面変更)は間隔を倍々にして最大60分まで空ける
            interval = base if fails < 3 else min(60, base * 2 ** (fails - 2))
            wait = interval * 60 + random.randint(0, 60)
            while wait > 0:  # 長い待機中もロックを更新
                time.sleep(min(wait, 600))
                wait -= 600
                LOCK_PATH.touch()
    except KeyboardInterrupt:
        logger.info("停止しました")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
