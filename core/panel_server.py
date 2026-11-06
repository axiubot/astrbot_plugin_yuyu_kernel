# -*- coding: utf-8 -*-
"""御坂面板：本机小服务器，页面和操作都不依赖 AstrBot 的页面桥。"""
import asyncio
import base64
import builtins
import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from astrbot.api import logger

from . import webui
from .paths import RUNTIME

PORT = 6197

_STATE = {"plugin": None, "loop": None}


def _run(coro, timeout=40):
    """把协程丢回主事件循环执行。"""
    loop = _STATE.get("loop")
    if loop is None:
        return None
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout=timeout)
    except Exception as e:
        logger.warning(f"[面板] 动作失败: {e}")
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _redirect(self, to: str):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        plugin = _STATE["plugin"]

        if u.path in ("/", "/dashboard.html"):
            self._dashboard()
            return

        if u.path == "/api/status":
            try:
                data = webui.build_status()
                data["switches"] = webui._switches(plugin)
                self._send(json.dumps({"status": "ok", "data": data}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8", 500)
            return

        if u.path == "/app.js":
            try:
                js = (Path(__file__).resolve().parent.parent / "pages" / "控制台" / "app.js").read_bytes()
                self._send(js, "application/javascript; charset=utf-8")
            except Exception as e:
                self._send(str(e).encode(), "text/plain", 404)
            return

        if u.path == "/plain":
            try:
                data = webui.build_status()
                data["switches"] = webui._switches(plugin)
                self._send(webui.render_html(data, q).encode("utf-8"))
            except Exception as e:
                self._send(f"<pre>{e}</pre>".encode(), "text/html; charset=utf-8", 500)
            return

        if u.path == "/api/act":
            self._api_act()
            return

        if u.path == "/":
            try:
                data = webui.build_status()
                data["switches"] = webui._switches(plugin)
                html = webui.render_html(data, q)
                self._send(html.encode("utf-8"))
            except Exception as e:
                logger.exception("[面板] 渲染失败")
                self._send(f"<h1>渲染失败</h1><pre>{e}</pre>".encode("utf-8"), code=500)
            return

        if u.path == "/act":
            self._act(q)
            return

        if u.path == "/tts":
            text = q.get("text", "唔……御坂、御坂在这儿呢。")[:120]
            try:
                payload = json.dumps({"text": text, "text_lang": "ja",
                                      "media_type": "wav", "emotion": "soft"}).encode()
                tts = RUNTIME.get("tts_proxy", "")
                if not tts:
                    self._send("未配置 TTS".encode("utf-8"), "text/plain; charset=utf-8", 400)
                    return
                req = urllib.request.Request(tts, data=payload,
                                             headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=120) as resp:
                    self._send(resp.read(), "audio/wav")
            except Exception as e:
                self._send(f"合成失败: {e}".encode("utf-8"), "text/plain; charset=utf-8", 500)
            return

        self._send(b"not found", "text/plain", 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/act":
            self._api_act()
            return
        self._send(b"not found", "text/plain", 404)

    def _api_act(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        body["back"] = ""
        q = {k: str(v) for k, v in body.items()}
        self._dispatch(q, json_mode=True)

    def _dashboard(self):
        try:
            html = (Path(__file__).resolve().parent.parent / "pages" / "控制台" / "dashboard.html").read_bytes()
            self._send(html)
        except Exception as e:
            self._send(f"<pre>{e}</pre>".encode(), "text/html; charset=utf-8", 500)

    def _act(self, q: dict):
        self._dispatch(q, json_mode=False)

    def _dispatch(self, q: dict, json_mode: bool = False):
        plugin = getattr(builtins, "__yuyu_plugin", None) or _STATE["plugin"]
        do = q.get("do", "")
        back = q.get("back", "/")
        out = {"msg": ""}
        try:
            if do == "say":
                text = q.get("text", "").strip()[:300]
                if text:
                    from astrbot.core.message.message_event_result import MessageChain
                    from astrbot.api.message_components import Plain
                    _run(plugin.context.send_message(RUNTIME.get("owner_umo", ""), MessageChain([Plain(text=text)])))
            elif do == "backup":
                _run(plugin.tool_backup(None))
            elif do == "digest":
                _run(plugin.tool_digest(None, 7))
            elif do == "reach":
                _run(plugin._offer())
            elif do == "tick":
                plugin._emo_tick()
                webui.write_snapshot(plugin)
            elif do == "reset":
                import time as _t
                f = webui.EX / "emotion.json"
                f.write_text(json.dumps({
                    "dims": {k: 50 for k in ("attach", "curious", "ruminate", "duty", "social",
                                             "fatigue", "intimacy", "stress", "joy")},
                    "expectation": 0.0, "blame": 0.0, "thoughts": [],
                    "last_tick": _t.time(), "state": "calm",
                    "last_inject": 0.0, "last_inject_state": "",
                    "fake_comfort": 0, "last_harsh": 0.0, "last_soft": 0.0,
                }, ensure_ascii=False, indent=1), encoding="utf-8")
                webui.write_snapshot(plugin)
            elif do in ("approve", "reject"):
                _run(plugin.tool_review_style(None, int(q.get("i", 0)), do == "approve"))
            elif do == "toggle":
                key = q.get("key", "")
                cur = webui._switches(plugin)
                val = not bool(cur.get(key))
                conf = webui._load(webui.EX / "extra_conf.json", {})
                conf[key] = val
                (webui.EX / "extra_conf.json").write_text(
                    json.dumps(conf, ensure_ascii=False, indent=1), encoding="utf-8")
                webui.write_snapshot(plugin)
            elif do in ("notes", "notes_list"):
                pkg = webui._notes_page(q.get("offset", 0))
                out["notes"] = pkg
            elif do == "search":
                kw = q.get("q", "")
                hits = []
                try:
                    import sqlite3
                    if not webui.FTS_DB.exists():
                        pass
                    con = sqlite3.connect(webui.FTS_DB)
                    for tx, k in con.execute("SELECT text, kind FROM mem WHERE mem MATCH ? ORDER BY rank LIMIT 10",
                                             (f'"{kw}"',)):
                        hits.append(f"[{k}] {tx[:80]}")
                    con.close()
                except Exception:
                    for r in webui._jsonl(webui.NOTES / "notes.jsonl"):
                        if kw and kw in str(r.get("text", "")):
                            hits.append(str(r.get("text"))[:80])
                out["hits"] = hits[:10]
            elif do == "note_update":
                out["msg"] = webui._note_edit("note_update", q.get("i"), q.get("text"))
                webui.write_snapshot(plugin)
            elif do == "tts":
                text = (q.get("text") or "唔……御坂、御坂在这儿呢。")[:120]
                tts = RUNTIME.get("tts_proxy", "")
                if not tts:
                    out["msg"] = "未配置 TTS 地址"
                else:
                    try:
                        payload = json.dumps({"text": text, "text_lang": "ja",
                                              "media_type": "wav", "emotion": "soft"}).encode()
                        req = urllib.request.Request(tts, data=payload,
                                                     headers={"Content-Type": "application/json"},
                                                     method="POST")
                        with urllib.request.urlopen(req, timeout=120) as resp:
                            raw = resp.read()
                        out["audio"] = "data:audio/wav;base64," + base64.b64encode(raw).decode()
                        out["msg"] = f"合成好了（{len(raw)//1024} KB）"
                    except Exception as e:
                        out["msg"] = f"合成失败：{e}"

            elif do == "forget":
                kw = q.get("q", "")
                f = webui.NOTES / "notes.jsonl"
                try:
                    rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
                except Exception:
                    rows = []
                keep = [r for r in rows if kw and kw not in str(r.get("text", ""))]
                f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in keep) + ("\n" if keep else ""),
                             encoding="utf-8")
                out["msg"] = f"删掉了 {len(rows)-len(keep)} 条"
                webui.write_snapshot(plugin)
            elif do == "note_delete":
                webui._note_edit("note_delete", q.get("i"))
                webui.write_snapshot(plugin)
            elif do == "note_pin":
                webui._note_edit("note_pin", q.get("i"))
                webui.write_snapshot(plugin)
            elif do == "restore":
                webui._restore_backup(plugin, q.get("name", ""))
                webui.write_snapshot(plugin)
        except Exception as e:
            logger.exception(f"[面板] 动作 {do} 失败")
            out["msg"] = f"失败：{e}"
        if json_mode:
            self._send(json.dumps({"status": "ok", "data": out}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self._redirect(back)


def start(plugin, loop, port: int = 6197):
    _STATE["plugin"] = plugin
    _STATE["loop"] = loop
    global PORT
    PORT = int(port or 6197)
    for cand in range(PORT, PORT + 8):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            logger.info(f"[面板] 已启动 http://127.0.0.1:{cand}/")
            return srv
        except OSError:
            continue
        except Exception as e:
            logger.warning(f"[面板] 启动失败: {e}")
            return None
    logger.warning("[面板] 6197-6204 都被占用，面板没起来")
    return None
