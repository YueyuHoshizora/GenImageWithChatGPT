#!/usr/bin/env python3
"""純文字輸入的繪圖工具：透過官方 Codex CLI 的內建 image generation，使用 ChatGPT 訂閱（OAuth）出圖。

登入由 `codex login`（Sign in with ChatGPT）處理，本工具不碰任何 token。

用法：
    python3 genimage.py                  # 互動模式，輸入文字描述即出圖
    python3 genimage.py "一隻灰色的貓"    # 單次出圖
    python3 genimage.py --login          # 執行 codex login
    python3 genimage.py --logout         # 執行 codex logout
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "output"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
EXEC_TIMEOUT = 600


class ToolError(Exception):
    pass


def codex_bin() -> str:
    path = shutil.which("codex")
    if not path:
        raise ToolError(
            "找不到 codex 指令。請先安裝 Codex CLI（例如 `brew install --cask codex` 或 "
            "`npm install -g @openai/codex`），再執行 `codex login`。"
        )
    return path


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def is_logged_in() -> bool:
    r = subprocess.run([codex_bin(), "login", "status"], capture_output=True, stdin=subprocess.DEVNULL)
    return r.returncode == 0


def codex_login() -> None:
    # 互動式：由 Codex 開瀏覽器完成 Sign in with ChatGPT
    if subprocess.run([codex_bin(), "login"]).returncode != 0:
        raise ToolError("codex login 失敗。")


def codex_logout() -> None:
    if subprocess.run([codex_bin(), "logout"]).returncode != 0:
        raise ToolError("codex logout 失敗。")


def generate(prompt: str) -> list[Path]:
    """呼叫 codex exec 出圖，回傳 Codex 存放的圖片路徑。"""
    # 讓 Codex 原樣使用提示詞，否則它會自行改寫、加料
    request = f"$imagegen Use the following prompt exactly as written, without rewriting or adding details:\n{prompt}"
    with tempfile.TemporaryDirectory(prefix="genimage-") as workdir:
        try:
            r = subprocess.run(
                [
                    codex_bin(), "exec", "--json", "--skip-git-repo-check", "--ephemeral",
                    "--enable", "image_generation", "--sandbox", "read-only",
                    "-C", workdir, request,
                ],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=EXEC_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"codex 超過 {EXEC_TIMEOUT} 秒沒有完成。")

    thread_id = None
    failure = None
    last_message = ""
    for line in r.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        etype = event.get("type")
        item = event.get("item") or {}
        if etype == "thread.started":
            thread_id = event.get("thread_id")
        elif etype == "turn.failed":
            failure = (event.get("error") or {}).get("message") or "turn failed"
        elif etype == "error":
            failure = event.get("message") or "error"
        elif etype == "item.completed" and item.get("type") == "agent_message":
            last_message = item.get("text", "")  # item.type == "error" 多為設定警告，不算失敗

    if failure:
        raise ToolError(f"Codex 回報失敗：{failure}")
    if r.returncode != 0:
        raise ToolError(f"codex exec 結束碼 {r.returncode}：{(r.stderr or '').strip()[:500]}")
    if not thread_id:
        raise ToolError("codex 沒有回傳 thread_id。")

    img_dir = codex_home() / "generated_images" / thread_id
    images = sorted(p for p in img_dir.glob("*") if p.suffix.lower() in IMAGE_SUFFIXES) if img_dir.is_dir() else []
    if not images:
        hint = f"\nCodex 回覆：{last_message.strip()}" if last_message.strip() else ""
        raise ToolError(f"Codex 沒有產生圖片（Free 方案不支援內建出圖，或被安全機制拒絕）。{hint}")
    return images


def save_images(images: list[Path], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    saved = []
    for src in images:
        ext = src.suffix.lower()
        dest, n = out_dir / f"{stamp}{ext}", 1
        while dest.exists():
            dest, n = out_dir / f"{stamp}-{n}{ext}", n + 1
        shutil.copyfile(src, dest)  # 保留 Codex 自己的那份，不動它的資料
        saved.append(dest)
    return saved


PREVIEW_COLS = 48  # 預覽寬度（字元欄數）


def _preview_mode() -> str:
    """回傳 iterm / kitty / chafa / off。GENIMAGE_PREVIEW 可手動覆寫。"""
    forced = os.environ.get("GENIMAGE_PREVIEW", "").lower()
    if forced:
        return forced
    if not sys.stdout.isatty() or os.environ.get("TMUX"):  # tmux 需 passthrough，不自動啟用
        return "off"
    term, prog = os.environ.get("TERM", ""), os.environ.get("TERM_PROGRAM", "")
    if prog in {"iTerm.app", "WezTerm"} or os.environ.get("LC_TERMINAL") == "iTerm2":
        return "iterm"
    if prog == "ghostty" or os.environ.get("KITTY_WINDOW_ID") or "kitty" in term:
        return "kitty"
    return "chafa" if shutil.which("chafa") else "off"


def preview(path: Path) -> None:
    """在終端機內直接顯示圖片；不支援時靜默略過（檔案已存檔）。"""
    mode = _preview_mode()
    if mode == "iterm":
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        name = base64.b64encode(path.name.encode()).decode("ascii")
        sys.stdout.write(
            f"\033]1337;File=name={name};inline=1;width={PREVIEW_COLS};preserveAspectRatio=1:{data}\a\n"
        )
    elif mode == "kitty":
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        chunks = [data[i:i + 4096] for i in range(0, len(data), 4096)]
        for i, chunk in enumerate(chunks):
            more = 1 if i < len(chunks) - 1 else 0
            ctrl = f"a=T,f=100,c={PREVIEW_COLS},m={more}" if i == 0 else f"m={more}"
            sys.stdout.write(f"\033_G{ctrl};{chunk}\033\\")
        sys.stdout.write("\n")
    elif mode == "chafa":
        subprocess.run(["chafa", f"--size={PREVIEW_COLS}", str(path)], check=False)
    sys.stdout.flush()


def draw(prompt: str, out_dir: Path) -> None:
    if not is_logged_in():
        print("尚未登入 ChatGPT，開始登入…")
        codex_login()
    print("繪圖中…（約數十秒）", flush=True)
    for p in save_images(generate(prompt), out_dir):
        print(f"已儲存：{p}")
        preview(p)


def main() -> int:
    ap = argparse.ArgumentParser(description="純文字輸入繪圖工具（Codex 內建 image generation，ChatGPT 訂閱登入）")
    ap.add_argument("prompt", nargs="*", help="圖片描述；省略則進入互動模式")
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT_DIR, help="圖片輸出資料夾（預設 ./output）")
    ap.add_argument("--login", action="store_true", help="執行 codex login 後離開")
    ap.add_argument("--logout", action="store_true", help="執行 codex logout 後離開")
    args = ap.parse_args()

    try:
        if args.logout:
            codex_logout()
            return 0
        if args.login:
            codex_login()
            return 0
        if args.prompt:
            draw(" ".join(args.prompt), args.out)
            return 0

        print("輸入圖片描述後按 Enter，空行或 Ctrl-D / 輸入 exit 離開。")
        while True:
            try:
                prompt = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not prompt or prompt.lower() in {"exit", "quit"}:
                break
            try:
                draw(prompt, args.out)
            except ToolError as e:
                print(f"✗ {e}", file=sys.stderr)
        return 0
    except ToolError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
