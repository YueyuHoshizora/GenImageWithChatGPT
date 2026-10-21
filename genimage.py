#!/usr/bin/env python3
"""純文字輸入的繪圖工具：OpenAI gpt-image-2.5-flare + Sign in with ChatGPT (OAuth)。

用法：
    python genimage.py                  # 互動模式，輸入文字描述即出圖
    python genimage.py "一隻灰色的貓"    # 單次出圖
    python genimage.py --login          # 強制重新登入
    python genimage.py --logout         # 撤銷並清除 token
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
import requests

MODEL = "gpt-image-2.5-flare"
IMAGES_URL = "https://api.openai.com/v1/images/generations"
RESOURCE = "https://api.openai.com/v1"
ISSUER = "https://auth.openai.com"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"

AGENT_NAME = "GenImage"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPE = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"

CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
LOGIN_TIMEOUT = 300
REFRESH_MARGIN = 120  # access token 剩餘秒數低於此值就先 refresh
AUTH_HTTP_TIMEOUT = 30
IMAGE_HTTP_TIMEOUT = 300

# refresh 回這些錯誤碼代表 refresh token 已不可用，需要重新授權
UNUSABLE_REFRESH_CODES = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}

HOME = Path(os.environ.get("GENIMAGE_HOME") or Path.home() / ".config" / "genimage")
STATE_FILE = HOME / "credentials.json"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "output"


class ToolError(Exception):
    pass


class TokenError(ToolError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ───────────────────────── 本機憑證儲存 ─────────────────────────

def _write_private(path: Path, text: str) -> None:
    """原子寫入，僅限本人讀寫（0600）。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except json.JSONDecodeError as e:
        raise ToolError(f"{STATE_FILE} 損毀（{e}），請刪除後重新登入。")
    # 每個 host 固定一個 ext_agent_host_id，須在第一次登入前就存在
    state.setdefault("ext_agent_host_id", f"urn:uuid:{uuid.uuid4()}")
    return state


def save_state(state: dict) -> None:
    _write_private(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))


def _clear_tokens(account: dict) -> None:
    """登出／token 失效：清掉 token，保留 client 與帳號對應供下次登入使用。"""
    for key in ("id_token", "access_token", "refresh_token", "token_type", "expires_in", "saved_at", "scopes"):
        account.pop(key, None)


# ───────────────────────── OAuth ─────────────────────────

@lru_cache(maxsize=1)
def discovery() -> dict:
    try:
        r = requests.get(DISCOVERY_URL, timeout=AUTH_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        raise ToolError(f"無法取得 OpenAI OIDC 設定：{e}")


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)  # 86 字元，落在 RFC 7636 的 43–128
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path != CALLBACK_PATH:
            self.send_error(404)
            return
        self.server.result = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = "<!doctype html><meta charset=utf-8><body>已收到回應，可以關閉此分頁並回到終端機。".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 不把含 code 的 URL 印出來
        pass


def _bind_listener() -> HTTPServer:
    # 文件範例用 1455；之後的登入只允許 port 不同，其餘（127.0.0.1、路徑）必須一致
    try:
        server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    except OSError:
        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.result = None
    return server


def _wait_callback(server: HTTPServer) -> dict:
    deadline = time.monotonic() + LOGIN_TIMEOUT
    try:
        while server.result is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolError("等待瀏覽器登入逾時。")
            server.timeout = min(1.0, remaining)
            server.handle_request()
        return server.result
    finally:
        server.server_close()


def _token_request(data: dict) -> dict:
    try:
        r = requests.post(discovery()["token_endpoint"], data=data, timeout=AUTH_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise ToolError(f"Token endpoint 連線失敗：{e}")
    if not r.ok:
        try:
            body = r.json()
        except ValueError:
            body = {}
        code = body.get("error") if isinstance(body.get("error"), str) else ""
        desc = body.get("error_description") or r.text[:300]
        raise TokenError(code, f"Token endpoint 錯誤 {r.status_code}：{code} {desc}".strip())
    return r.json()


def _validate_id_token(id_token: str, client_id: str, nonce: str | None) -> dict:
    """依官方文件驗證 ID token：JWKS 簽章、issuer、audience、exp、nonce。"""
    try:
        r = requests.get(discovery()["jwks_uri"], timeout=AUTH_HTTP_TIMEOUT)
        r.raise_for_status()
        keyset = jwt.PyJWKSet.from_dict(r.json())
        kid = jwt.get_unverified_header(id_token).get("kid")
        key = next((k for k in keyset.keys if k.key_id == kid), None)
        if key is None:
            raise ToolError("ID token 的簽章金鑰不在 OpenAI JWKS 內。")
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=ISSUER,
            leeway=60,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except (jwt.PyJWTError, requests.RequestException, ValueError) as e:
        raise ToolError(f"ID token 驗證失敗：{e}")
    if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ToolError("ID token 的 nonce 不符。")
    return claims


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _apply_tokens(account: dict, tok: dict) -> None:
    """access / refresh / expiry / scopes 要一起換掉（refresh token 會輪替）。"""
    scopes = tok.get("scope", "").split() or account.get("scopes", [])
    if PLAN_SCOPE not in scopes:
        raise ToolError(
            f"此授權沒有 {PLAN_SCOPE}（ChatGPT 方案用量）。請重新執行 --login 並在同意畫面允許。"
        )
    account.update(
        access_token=tok["access_token"],
        refresh_token=tok.get("refresh_token") or account.get("refresh_token"),
        token_type=tok.get("token_type", "Bearer"),
        expires_in=int(tok.get("expires_in", 3600)),
        scopes=scopes,
        saved_at=_now_iso(),
    )


def authorize(state: dict, account: dict | None) -> dict:
    """瀏覽器授權（PKCE）。account 為已註冊過的帳號則沿用其 client_id，否則動態註冊。"""
    endpoints = discovery()
    verifier, challenge = _pkce()
    state_param = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)

    server = _bind_listener()
    redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
    registered = bool(account and account.get("client_id"))
    client_id = account["client_id"] if registered else DYNAMIC_CLIENT_ID

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "resource": RESOURCE,
        "state": state_param,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "ext_agent_host_id": state["ext_agent_host_id"],
    }
    if registered:
        if account.get("email"):
            params["login_hint"] = account["email"]
    else:
        params["agent_name_hint"] = AGENT_NAME
    # id_token_hint 只送給授權端點，不印在畫面上
    full_params = dict(params)
    if registered and account.get("id_token"):
        full_params["id_token_hint"] = account["id_token"]

    base = endpoints["authorization_endpoint"]
    print("開啟瀏覽器進行 ChatGPT 登入…")
    if not webbrowser.open(f"{base}?{urlencode(full_params)}"):
        print(f"無法自動開啟瀏覽器，請手動開啟：\n{base}?{urlencode(params)}")

    cb = _wait_callback(server)

    if not hmac.compare_digest(cb.get("state", ""), state_param):
        raise ToolError("回呼的 state 不符，已中止。")
    if cb.get("error"):
        raise ToolError(f"授權失敗：{cb['error']} {cb.get('error_description', '')}".strip())
    code = cb.get("code")
    if not code:
        raise ToolError("回呼中沒有 authorization code。")

    issued = cb.get("client_id")
    if registered:
        if issued and issued != client_id:
            raise ToolError("回呼的 client_id 與已註冊的不同，已拒絕。")
    else:
        if not issued or issued == DYNAMIC_CLIENT_ID:
            raise ToolError("動態註冊未回傳 client_id，註冊未完成。")
        client_id = issued

    tok = _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": RESOURCE,
        }
    )
    if not tok.get("id_token") or not tok.get("access_token"):
        raise ToolError("Token 回應缺少 id_token 或 access_token。")
    claims = _validate_id_token(tok["id_token"], client_id, nonce)
    if registered and account.get("subject") and claims["sub"] != account["subject"]:
        raise ToolError("登入的 ChatGPT 帳號與先前註冊的不同，已拒絕。")

    new_account = {
        "email": claims.get("email") or (account or {}).get("email"),
        "issuer": ISSUER,
        "subject": claims["sub"],
        "client_id": client_id,
        "id_token": tok["id_token"],
    }
    scope_source = dict(tok)
    scope_source.setdefault("scope", cb.get("scope", ""))
    _apply_tokens(new_account, scope_source)
    return new_account


def _expires_at(account: dict) -> float:
    saved = datetime.fromisoformat(account["saved_at"]).timestamp()
    return saved + int(account.get("expires_in", 0))


def refresh(state: dict) -> None:
    account = state["account"]
    try:
        tok = _token_request(
            {
                "grant_type": "refresh_token",
                "client_id": account["client_id"],
                "refresh_token": account["refresh_token"],
                "resource": RESOURCE,
            }
        )
    except TokenError as e:
        if e.code in UNUSABLE_REFRESH_CODES:
            _clear_tokens(account)
            save_state(state)
            raise ToolError("登入已失效，請執行 --login 重新登入。")
        raise
    if tok.get("id_token"):
        _validate_id_token(tok["id_token"], account["client_id"], None)
        account["id_token"] = tok["id_token"]
    _apply_tokens(account, tok)
    save_state(state)


def ensure_access_token() -> str:
    """回傳有效的 access token；沒登入就登入，快過期就 refresh。"""
    state = load_state()
    account = state.get("account")
    if not account or not account.get("access_token") or not account.get("refresh_token"):
        state["account"] = authorize(state, account)
        save_state(state)
        print(f"已登入：{state['account'].get('email') or state['account']['subject']}")
    elif _expires_at(account) - time.time() < REFRESH_MARGIN:
        refresh(state)
    return state["account"]["access_token"]


def login() -> None:
    state = load_state()
    state["account"] = authorize(state, state.get("account"))
    save_state(state)
    acc = state["account"]
    print(f"登入成功：{acc.get('email') or acc['subject']}（client_id: {acc['client_id']}）")


def logout() -> None:
    state = load_state()
    account = state.get("account")
    if not account or not account.get("access_token"):
        print("目前沒有登入。")
        return
    revoked = False
    if account.get("refresh_token"):
        try:
            r = requests.post(
                discovery()["revocation_endpoint"],
                data={
                    "token": account["refresh_token"],
                    "token_type_hint": "refresh_token",
                    "client_id": account["client_id"],
                },
                timeout=AUTH_HTTP_TIMEOUT,
            )
            revoked = r.status_code == 200
        except (requests.RequestException, ToolError):
            pass
    _clear_tokens(account)
    save_state(state)
    if revoked:
        print("已登出並撤銷 refresh token。")
    else:
        print("已清除本機 token，但未能確認遠端撤銷；可到 ChatGPT Settings 中斷此 app 的連線。")


# ───────────────────────── 圖像生成 ─────────────────────────

def _api_error(r: requests.Response) -> str:
    code = ""
    try:
        body = r.json()
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        msg, code = err.get("message") or "", err.get("code") or ""
    elif isinstance(body, dict) and body.get("detail"):
        msg = str(body["detail"])  # 直連路由的 admission 錯誤沒有標準 error 物件
    else:
        msg = r.text[:300]
    text = f"API 錯誤 {r.status_code}：{msg}"
    if code:
        text += f"（code: {code}）"
    if r.headers.get("x-request-id"):
        text += f"\nrequest id: {r.headers['x-request-id']}"
    if str(code).startswith(("subscription_sharing_", "chatpass_v2_")) or r.status_code in (401, 403):
        text += (
            "\n提示：OpenAI 官方文件中，ChatGPT 方案 OAuth token 目前只開放 POST /v1/responses，"
            "且不支援 image generation；此 token 可能無法用於 Images API。"
        )
    return text


def generate(access_token: str, prompt: str, model: str) -> list[bytes]:
    try:
        r = requests.post(
            IMAGES_URL,
            json={"model": model, "prompt": prompt},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=IMAGE_HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        raise ToolError(f"連線失敗：{e}")
    if not r.ok:
        raise ToolError(_api_error(r))
    data = r.json().get("data") or []
    images = [base64.b64decode(d["b64_json"]) for d in data if d.get("b64_json")]
    if not images:
        raise ToolError("回應中沒有圖片資料。")
    return images


def save_images(images: list[bytes], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    paths = []
    for img in images:
        path, n = out_dir / f"{stamp}.png", 1
        while path.exists():
            path, n = out_dir / f"{stamp}-{n}.png", n + 1
        path.write_bytes(img)
        paths.append(path)
    return paths


def draw(prompt: str, model: str, out_dir: Path) -> None:
    token = ensure_access_token()
    print("繪圖中…", flush=True)
    for p in save_images(generate(token, prompt, model), out_dir):
        print(f"已儲存：{p}")


def main() -> int:
    ap = argparse.ArgumentParser(description="純文字輸入繪圖工具（OpenAI 圖像模型，Sign in with ChatGPT 登入）")
    ap.add_argument("prompt", nargs="*", help="圖片描述；省略則進入互動模式")
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT_DIR, help="圖片輸出資料夾（預設 ./output）")
    ap.add_argument("-m", "--model", default=MODEL, help=f"模型 ID（預設 {MODEL}）")
    ap.add_argument("--login", action="store_true", help="強制重新登入後離開")
    ap.add_argument("--logout", action="store_true", help="撤銷並清除 token 後離開")
    args = ap.parse_args()

    try:
        if args.logout:
            logout()
            return 0
        if args.login:
            login()
            return 0
        if args.prompt:
            draw(" ".join(args.prompt), args.model, args.out)
            return 0

        print(f"模型：{args.model}　輸入圖片描述後按 Enter，空行或 Ctrl-D / 輸入 exit 離開。")
        while True:
            try:
                prompt = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not prompt or prompt.lower() in {"exit", "quit"}:
                break
            try:
                draw(prompt, args.model, args.out)
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
