"""独立 Web 面板：可配置监听地址/端口，账号密码登录，IP 白名单，会话令牌（1.0.0-A6）。

安全边界（实现约束）：
- 默认仅监听 127.0.0.1；IP 白名单默认仅回环，命中即拒绝；
- 密码只存 PBKDF2 哈希（不存明文）；会话令牌只存哈希、HttpOnly、TTL 可配；
- 登录失败按 IP 限流并锁定；登录/退出/失败均写审计（actor=panel:<user>）；
- 非回环监听且未显式开启 allow_insecure_http 时，拒绝明文登录（提示走 HTTPS 反代/隧道）；
- 默认以本机管理员模式运行；可通过 allow_write_actions 显式切换只读。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlsplit

from ..web_console_compat import (
    StandaloneQueryParams,
    StandaloneRequest,
    StandaloneResponse,
    StandaloneUploadFile,
    standalone_json_bytes,
    standalone_request_context,
)

CREDENTIALS_FILE = "remote_panel.json"
_DEFAULT_ALLOWLIST = ("127.0.0.1", "::1")
_DEFAULT_TRUSTED_PROXIES = ("127.0.0.1", "::1")
_PBKDF2_ITERATIONS = 200_000
_LOGIN_MAX_FAILS = 5
_LOGIN_LOCK_SECONDS = 600
_SESSION_DEFAULT_TTL = 12 * 3600
_COOKIE_NAME = "tavern_panel"
_COOKIE_SAME_SITE = "Strict"
_CONSOLE_ROUTE = "/#/plugin-page/astrbot_plugin_tavern/console"
_CONSOLE_API_PREFIX = "/api/console/"
_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_UPLOAD_BYTES = 512 * 1024 * 1024


# ── 密码与凭据 ─────────────────────────────────────────────
def hash_password(password: str, iterations: int = _PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", str(password).encode("utf-8"), salt, iterations
    )
    return (
        f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, hash_hex = str(stored or "").split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def _normalize_allowlist(items: Any) -> list[str]:
    if isinstance(items, str):
        items = [items]
    result: list[str] = []
    for item in items or ():
        text = str(item or "").strip()
        if not text:
            continue
        try:
            ipaddress.ip_network(text, strict=False)
            result.append(text)
        except ValueError:
            continue
    return result or list(_DEFAULT_ALLOWLIST)


def ip_allowed(ip: str, allowlist: Any) -> bool:
    text = str(ip or "").strip()
    if not text:
        return False
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    networks = _normalize_allowlist(allowlist)
    for entry in networks:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if address in network:
            return True
    return False


def _panel_cookie(
    token: str,
    *,
    secure: bool,
    max_age: int | None = None,
) -> str:
    """Build the panel session cookie with a fixed security attribute set.

    SameSite is always Strict; ``Secure`` follows the effective scheme and
    ``HttpOnly`` is always set so browser script can never read the token.
    """
    parts = [f"{_COOKIE_NAME}={token}", "HttpOnly", f"SameSite={_COOKIE_SAME_SITE}", "Path=/"]
    if secure:
        parts.append("Secure")
    if max_age is not None and max_age >= 0:
        parts.append(f"Max-Age={int(max_age)}")
    return "; ".join(parts)


def forwarded_proto(
    client_ip: str,
    header_value: str,
    trusted_cidrs: Any,
) -> str:
    """Return the forwarded scheme only when the direct peer is trusted.

    ``X-Forwarded-Proto`` is never honored for untrusted peers, so an
    arbitrary client cannot force HTTPS cookie attributes.
    """
    if not trusted_cidrs:
        return ""
    if not ip_allowed(client_ip, trusted_cidrs):
        return ""
    first = str(header_value or "").strip().split(",", 1)[0].strip().lower()
    return first if first in ("http", "https") else ""


def normalize_scheme(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in ("http", "https") else "http"


def default_credentials(username: str = "admin") -> dict[str, Any]:
    return {
        "username": str(username or "admin"),
        "password_hash": "",
        "ip_allowlist": list(_DEFAULT_ALLOWLIST),
        "session_ttl_seconds": _SESSION_DEFAULT_TTL,
        "login_rate_limit": _LOGIN_MAX_FAILS,
        "allow_write_actions": True,
        "sessions_revoked_at": "",
    }


def load_credentials(data_dir: Path) -> dict[str, Any]:
    path = Path(data_dir) / CREDENTIALS_FILE
    if not path.is_file():
        return default_credentials()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_credentials()
    if not isinstance(value, dict):
        return default_credentials()
    merged = default_credentials()
    for key in ("username", "password_hash", "ip_allowlist",
                "session_ttl_seconds", "login_rate_limit",
                "allow_write_actions", "sessions_revoked_at"):
        if key in value:
            merged[key] = value[key]
    return merged


def save_credentials(data_dir: Path, credentials: Mapping[str, Any]) -> None:
    path = Path(data_dir) / CREDENTIALS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        dict(credentials), ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    fd, temp = tempfile.mkstemp(prefix=".remote-panel-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


# ── 会话与限流 ─────────────────────────────────────────────
def _now() -> float:
    return time.monotonic()


def _epoch() -> float:
    return time.time()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class _RateLimiter:
    def __init__(self) -> None:
        self._values: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def allowed(self, ip: str, limit: int) -> bool:
        limit = max(1, int(limit or _LOGIN_MAX_FAILS))
        with self._lock:
            fails, lock_until = self._values.get(ip, (0, 0.0))
            if lock_until and _now() < lock_until:
                return False
            if fails >= limit:
                self._values[ip] = (0, _now() + _LOGIN_LOCK_SECONDS)
                return False
            return True

    def record_failure(self, ip: str) -> None:
        with self._lock:
            fails, lock_until = self._values.get(ip, (0, 0.0))
            self._values[ip] = (fails + 1, lock_until)

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._values.pop(ip, None)


# ── HTTP 服务 ──────────────────────────────────────────────
_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>321开团控制台 · 登录</title>
<style>
:root{color-scheme:light;--paper:#f3f6f8;--card:#fff;--ink:#14202c;--muted:#6c7d8c;--line:#d6e0e7;--accent:#b66b16;--accent-soft:#f7ecdf;--danger:#bd3e34}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 78% 18%,#fff4e8 0,transparent 34%),linear-gradient(145deg,#eef4f7,#f8fafb);color:var(--ink);font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif}
.shell{width:min(100%,460px)}
.brand{display:flex;gap:14px;align-items:center;margin:0 0 22px}
.mark{width:58px;height:58px;display:grid;place-items:center;border:1px solid #e7cda8;border-radius:18px;background:var(--accent-soft);color:var(--accent);font-size:28px}
.eyebrow{color:var(--accent);font-size:13px;font-weight:800;letter-spacing:.14em}
h1{font-size:28px;margin:3px 0 0}
.card{background:rgba(255,255,255,.94);border:1px solid var(--line);border-radius:24px;padding:28px;box-shadow:0 24px 70px rgba(31,53,71,.13)}
.card h2{font-size:22px;margin:0 0 8px}
.lead{color:var(--muted);font-size:14px;line-height:1.7;margin:0 0 20px}
label{display:block;font-size:13px;font-weight:700;margin:14px 0 7px}
input{width:100%;padding:13px 14px;border:1px solid var(--line);border-radius:12px;background:#f8fafb;color:var(--ink);font-size:15px;outline:none}
input:focus{border-color:#c98a40;box-shadow:0 0 0 3px rgba(182,107,22,.12)}
button{width:100%;margin-top:20px;padding:13px;border:0;border-radius:12px;background:var(--accent);color:#fff;font-size:15px;font-weight:800;cursor:pointer}
button:disabled{opacity:.55;cursor:wait}
.msg{min-height:22px;margin-top:12px;color:var(--danger);font-size:13px;line-height:1.6}
.note{margin-top:16px;color:var(--muted);font-size:12px;line-height:1.6}
</style>
</head>
<body>
<main class="shell">
  <div class="brand">
    <div class="mark">♜</div>
    <div><div class="eyebrow">AI TAVERN</div><h1>321开团控制台</h1></div>
  </div>
  <section class="card">
    <h2>进入独立控制台</h2>
    <p class="lead">登录后将直接打开由 8766 端口托管的完整 WebUI，不会跳转到 AstrBot 管理后台。</p>
    <form id="login-form">
      <label for="username">账号</label>
      <input id="username" name="username" autocomplete="username" required />
      <label for="password">密码</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required />
      <button id="login-btn" type="submit">登录控制台</button>
      <div class="msg" id="login-msg" role="alert"></div>
    </form>
    <div class="note">会话 Cookie 仅用于本面板，并启用 HttpOnly 与 SameSite=Strict。</div>
  </section>
</main>
<script>
const form=document.querySelector("#login-form");
const button=document.querySelector("#login-btn");
const message=document.querySelector("#login-msg");
form.addEventListener("submit",async(event)=>{
  event.preventDefault();button.disabled=true;message.textContent="";
  try{
    const response=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:form.username.value,password:form.password.value})});
    const data=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(data.error||`登录失败（HTTP ${response.status}）`);
    const next=new URLSearchParams(location.search).get("next");
    location.replace(next&&next.startsWith("/")&&!next.startsWith("//")?next:"/");
  }catch(error){message.textContent=error.message||"登录失败，请检查账号和密码";button.disabled=false;}
});
</script>
</body>
</html>
"""


_PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>321开团 · 独立面板</title>
<style>
:root{--bg:#12141a;--card:#1b1e27;--line:#2b3040;--text:#e7e9ee;--dim:#9aa2b1;--accent:#e3a857;--danger:#e06c5a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;min-height:100vh}
.wrap{max-width:960px;margin:0 auto;padding:28px 18px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
.card h2{font-size:14px;margin:0 0 12px}
label{display:block;font-size:13px;color:var(--dim);margin:10px 0 4px}
input{width:100%;padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:#14171f;color:var(--text);font-size:14px}
button{width:100%;margin-top:14px;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#1a1206;font-weight:700;font-size:14px;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}
.msg{font-size:13px;margin-top:10px;min-height:18px}
.msg.err{color:var(--danger)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600}
.empty{color:var(--dim);font-size:13px;padding:8px 0}
.hidden{display:none}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
a{color:var(--accent)}
</style>
</head>
<body>
<div class="wrap">
  <h1>321开团 · 独立面板</h1>
  <p class="sub">只读运行总览 · 321开团独立面板</p>
  <div class="card" id="login-card">
    <h2>登录</h2>
    <label for="username">账号</label>
    <input id="username" autocomplete="username" />
    <label for="password">密码</label>
    <input id="password" type="password" autocomplete="current-password" />
    <button id="login-btn">登录</button>
    <div class="msg" id="login-msg"></div>
  </div>
  <div class="card hidden" id="overview-card">
    <div class="top"><h2>运行总览</h2><a href="#" id="logout">退出登录</a></div>
    <div id="overview-body" class="empty">加载中…</div>
  </div>
</div>
<script>
const $=(s)=>document.querySelector(s);
async function api(path,opts={}){
  const resp=await fetch(path,{headers:{"Content-Type":"application/json"},...opts});
  if(resp.status===401){showLogin();throw new Error("登录已过期");}
  if(resp.status===403){throw new Error("IP 不在白名单");}
  const data=await resp.json().catch(()=>({}));
  if(!resp.ok) throw new Error(data.error||("请求失败 HTTP "+resp.status));
  return data;
}
function showLogin(){$("#login-card").classList.remove("hidden");$("#overview-card").classList.add("hidden");}
function showOverview(){ $("#login-card").classList.add("hidden");$("#overview-card").classList.remove("hidden");loadOverview();}
async function loadOverview(){
  try{
    const d=await api("/api/overview");
    const sessions=(d.sessions||[]).map(s=>`<tr><td>${escapeHtml(s.name||"—")}</td><td>${escapeHtml(s.state||"—")}</td><td>${escapeHtml(s.world_name||"—")}</td></tr>`).join("");
    $("#overview-body").innerHTML=
      `<table><thead><tr><th>副本</th><th>状态</th><th>世界</th></tr></thead><tbody>${sessions||'<tr><td colspan="3">无副本</td></tr>'}</tbody></table>`+
      `<p style="margin-top:12px">插件 ${escapeHtml(d.plugin_version||"—")} · 数据库 Schema ${escapeHtml(String(d.schema_version??"—"))} · 世界 ${Number(d.world_count||0)} · 副本 ${Number(d.session_count||0)}</p>`;
  }catch(e){$("#overview-body").textContent=e.message;}
}
function escapeHtml(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));}
$("#login-btn").addEventListener("click",async()=>{
  const btn=$("#login-btn");btn.disabled=true;$("#login-msg").textContent="";
  try{
    const d=await api("/api/login",{method:"POST",body:JSON.stringify({username:$("#username").value,password:$("#password").value})});
    showOverview();
  }catch(e){$("#login-msg").textContent=e.message;btn.disabled=false;}
});
$("#logout").addEventListener("click",async(e)=>{e.preventDefault();try{await api("/api/logout",{method:"POST"});}catch(_){}showLogin();});
(async()=>{try{await api("/api/overview");showOverview();}catch(_){}})();
</script>
</body>
</html>
"""




class AssetsMixin:
    def _serve_console_api(self, route_path: str) -> None:
        session = self._session_entry()
        if session is None:
            self._send_json({"error": "未登录"}, status=401)
            return
        matched = self._match_console_route(route_path)
        if matched is None:
            self._send_json({"error": "控制台接口不存在"}, status=404)
            return
        route, route_params = matched
        allowed = tuple(route.get("methods") or ())
        if self.command not in allowed:
            self._send_json({"error": "接口不支持该请求方法"}, status=405)
            return
        if self.command == "POST" and not bool(
            self.panel.credentials.get("allow_write_actions")
        ):
            self._send_json(
                {
                    "error": "独立面板当前为只读模式",
                    "recovery": "请由管理员开启独立面板写操作后重试。",
                },
                status=403,
            )
            return

        payload: dict[str, Any] = {}
        uploads: dict[str, StandaloneUploadFile] = {}
        content_type = (self.headers.get("Content-Type") or "").lower()
        if self.command == "POST":
            if content_type.startswith("multipart/form-data"):
                payload, uploads = self._read_multipart()
            elif content_type.startswith("application/json"):
                payload = self._read_json()

        request_value = StandaloneRequest(
            username=str(session.get("username") or "admin"),
            method=self.command,
            query=self._query_params(),
            payload=payload,
            uploads=uploads,
            headers={str(key): str(value) for key, value in self.headers.items()},
        )
        try:
            with standalone_request_context(request_value):
                result = self._run_async(
                    route["handler"](**route_params),
                    timeout=None if route_path == "events" else 180.0,
                )
            self._send_console_response(result)
        finally:
            request_value.close()
    def _handle(self) -> None:
        path = urlsplit(self.path).path
        # 网页建卡命名空间：自带一次性令牌认证，玩家 IP 不在管理员白名单
        # 内属预期行为，因此必须在管理员白名单之前拦截分流。
        if path == "/cw" or path.startswith("/cw/"):
            self._handle_card_wizard(path)
            return
        if not ip_allowed(self._client_ip(), self.panel.credentials.get("ip_allowlist")):
            self._send_json({"error": "IP 不在白名单"}, status=403)
            return
        try:
            if self.command == "GET":
                if path == "/":
                    if not self._session_ok():
                        self._send_redirect("/login?next=/")
                    else:
                        self._serve_static("/index.html")
                elif path == "/login":
                    if self._session_ok():
                        self._send_redirect("/")
                    else:
                        self._send_html(_LOGIN_HTML)
                elif path == "/legacy":
                    self._send_html(_PANEL_HTML)
                elif path == "/api/context":
                    session = self._session_entry()
                    if session is None:
                        self._send_json({"error": "未登录"}, status=401)
                    else:
                        self._send_json(
                            {
                                "locale": "zh-CN",
                                "principalRef": (
                                    "remote-panel:"
                                    + str(session.get("username") or "admin")
                                ),
                                "authenticated": True,
                                "role": "admin",
                                "roles": ["admin", "author", "host"],
                                "capabilities": {
                                    "admin": True,
                                    "author": True,
                                    "host": True,
                                },
                                "standalone": True,
                            }
                        )
                elif path == "/api/overview":
                    self._api_overview()
                elif path == "/api/health":
                    self._send_json({"ok": True})
                elif path.startswith(_CONSOLE_API_PREFIX):
                    self._serve_console_api(
                        path[len(_CONSOLE_API_PREFIX):].strip("/")
                    )
                elif self._session_ok():
                    self._serve_static(path)
                else:
                    self._send_redirect("/login?next=/")
            elif self.command == "POST":
                if not self._same_origin_ok():
                    self._send_json(
                        {"error": "cross-site request rejected: Origin does not match panel host"},
                        status=403,
                    )
                    return
                if path == "/api/login":
                    self._api_login()
                elif path == "/api/logout":
                    self._api_logout()
                elif path.startswith(_CONSOLE_API_PREFIX):
                    self._serve_console_api(
                        path[len(_CONSOLE_API_PREFIX):].strip("/")
                    )
                else:
                    self._send_json({"error": "未找到"}, status=404)
            else:
                self._send_json({"error": "不支持的方法"}, status=405)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception:
            self.panel.logger.exception("321开团独立面板处理异常")
            try:
                self._send_json({"error": "服务器内部错误"}, status=500)
            except Exception:
                pass
    def _api_login(self) -> None:
        ip = self._client_ip()
        creds = self.panel.credentials
        username = str(creds.get("username") or "")
        stored_hash = str(creds.get("password_hash") or "")
        limit = int(creds.get("login_rate_limit") or _LOGIN_MAX_FAILS)
        if not stored_hash:
            self._send_json({"error": "尚未设置面板密码，请在 AstrBot 控制台设置"}, status=403)
            return
        # 非回环 + 非 HTTPS + 未显式允许明文 → 拒绝
        scheme = self._effective_scheme()
        if (
            not ip_allowed(ip, _DEFAULT_ALLOWLIST)
            and scheme != "https"
            and not self.panel.allow_insecure_http
        ):
            self._send_json(
                {"error": "非本机访问需要 HTTPS 反代/隧道；如确需明文请在配置中开启 allow_insecure_http"},
                status=403,
            )
            return
        if not self.panel.rate_limiter.allowed(ip, limit):
            self._send_json({"error": "登录尝试过多，请稍后再试"}, status=429)
            return
        payload = self._read_json()
        given_user = str(payload.get("username") or "")
        given_pass = str(payload.get("password") or "")
        if (
            not username
            or not hmac.compare_digest(given_user.encode("utf-8"), username.encode("utf-8"))
            or not verify_password(given_pass, stored_hash)
        ):
            self.panel.rate_limiter.record_failure(ip)
            self._audit("", f"panel:{given_user or '?'}", "panel.login_failed", ip, {})
            self._send_json({"error": "账号或密码错误"}, status=401)
            return
        self.panel.rate_limiter.record_success(ip)
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        ttl = max(60, int(creds.get("session_ttl_seconds") or _SESSION_DEFAULT_TTL))
        # 登录轮换：同一账号的旧会话立即失效，只保留本次会话。
        with self.panel.session_lock:
            for old_digest in [
                key
                for key, entry in self.panel.sessions.items()
                if entry.get("username") == username
            ]:
                self.panel.sessions.pop(old_digest, None)
            self.panel.sessions[digest] = {
                "issued_at": _epoch(),
                "expires_at": _epoch() + ttl,
                "username": username,
            }
        self._audit("", f"panel:{username}", "panel.login", ip, {})
        cookie = _panel_cookie(
            token,
            secure=self.panel.secure_cookie or self._effective_scheme() == "https",
            max_age=ttl,
        )
        self._send_json({"ok": True}, headers={"Set-Cookie": cookie})

