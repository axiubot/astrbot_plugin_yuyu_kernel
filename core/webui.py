# -*- coding: utf-8 -*-
"""御坂内核 WebUI：把她现在的状态做成一个页面。"""
import json
import time
import urllib.parse
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import json_response, request

PLUGIN = "astrbot_plugin_yuyu_inner"
from .paths import NOTES, EX, SHARED, DATA_ROOT, RUNTIME

ACT_SECRET = None   # 表单操作用的本地口令（首次生成后固定）

LABEL = {"attach": "依恋", "curious": "好奇", "ruminate": "反刍", "duty": "责任",
         "social": "社交", "fatigue": "疲惫", "intimacy": "亲密",
         "stress": "压力", "joy": "喜悦"}


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _persona_preview() -> str:
    try:
        import sqlite3
        con = sqlite3.connect(str(DATA_ROOT / "data_v4.db"))
        row = con.execute("select system_prompt from personas where persona_id='default' limit 1").fetchone()
        con.close()
        return (row[0] if row and row[0] else "")[:1200]
    except Exception:
        return ""


def _restore_backup(plugin, name: str) -> str:
    """从一个 zip 恢复（只覆盖 note/extra/shared 三类文件）。"""
    import zipfile
    from .paths import BACKUP_DIR as bdir
    if not name:
        return "没指定备份"
    z = bdir / name
    if not z.exists():
        return "找不到这个备份"
    roots = {
        "notes": NOTES,
        "extra": EX,
        "shared": SHARED,
    }
    n = 0
    try:
        with zipfile.ZipFile(z) as zf:
            for info in zf.infolist():
                parts = info.filename.split("/", 1)
                if len(parts) != 2 or parts[0] not in roots:
                    continue
                target = roots[parts[0]] / parts[1]
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                n += 1
        return f"恢复了 {n} 个文件"
    except Exception as e:
        return f"恢复失败：{str(e)[:60]}"


def _switches(plugin):
    conf = getattr(plugin, "conf", {}) or {}
    try:
        extra = _load(EX / "extra_conf.json", {})
    except Exception:
        extra = {}
    return {
        "proactive": bool(extra.get("proactive", True)),
        "emotion": bool(extra.get("emotion", True)),
        "autobackup": bool(extra.get("autobackup", False)),
        "enabled": bool(conf.get("enabled", True)),
    }


def _kinds(notes):
    out = {}
    for r in notes:
        k = str(r.get("kind") or "note")
        out[k] = out.get(k, 0) + 1
    return out


def _day_start() -> float:
    import time as _t
    lt = _t.localtime()
    return _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _count_today() -> int:
    """今天的对话条数（从 AstrBot 数据库里数）。"""
    import sqlite3
    try:
        con = sqlite3.connect(str(DATA_ROOT / "data_v4.db"))
        row = con.execute("select content from conversations where user_id=? order by updated_at desc limit 1",
                          (RUNTIME.get("owner_umo", ""),)).fetchone()
        con.close()
        if not row or not row[0]:
            return 0
        msgs = json.loads(row[0])
        return sum(1 for m in msgs if float(m.get("ts") or 0) >= _day_start())
    except Exception:
        return 0


def _notes_page(offset=0, size: int = 40):
    rows = _jsonl(NOTES / "notes.jsonl")
    total = len(rows)
    off = max(0, int(offset or 0))
    chunk = rows[::-1][off:off + size]
    out = []
    for i, r in enumerate(chunk):
        out.append({"idx": total - 1 - (off + i),
                    "ts": r.get("ts"), "text": str(r.get("text"))[:200],
                    "kind": r.get("kind", ""), "status": r.get("status", "")})
    return {"total": total, "items": out}


def _note_edit(action: str, idx, text=None) -> str:
    f = NOTES / "notes.jsonl"
    try:
        rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    except Exception:
        return "读不到记忆"
    try:
        i = int(idx)
    except Exception:
        return "序号不对"
    if not (0 <= i < len(rows)):
        return "序号越界"
    if action == "note_delete":
        rows.pop(i)
        msg = "删掉了"
    elif action == "note_update":
        t = str(text or "").strip()
        if not t:
            return "内容为空"
        rows[i]["text"] = t[:300]
        msg = "改好了"
    else:  # note_pin
        rows[i]["status"] = "open" if rows[i].get("status") != "open" else "done"
        msg = "置顶了" if rows[i]["status"] == "open" else "取消置顶了"
    try:
        f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
                     encoding="utf-8")
    except Exception as e:
        return f"写回失败：{e}"
    return msg


def _weekly(weeks: int = 8):
    """近 N 周每周的新增记忆条数。"""
    import time as _t
    now = _t.time()
    buckets = [0] * weeks
    for r in _jsonl(NOTES / "notes.jsonl"):
        age_days = (now - float(r.get("ts") or 0)) / 86400
        idx = int(age_days // 7)
        if 0 <= idx < weeks:
            buckets[weeks - 1 - idx] += 1
    return buckets


def _recent_chat(n: int = 24):
    import sqlite3
    try:
        con = sqlite3.connect(str(DATA_ROOT / "data_v4.db"))
        row = con.execute("select content from conversations where user_id=? order by updated_at desc limit 1",
                          (RUNTIME.get("owner_umo", ""),)).fetchone()
        con.close()
        if not row or not row[0]:
            return []
        msgs = json.loads(row[0])[-n:]
        out = []
        for m in msgs:
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
            txt = str(c or "")
            import re as _re
            txt = _re.sub(r"\[\[[^\]]*\]\]", "", txt)
            txt = _re.sub(r"<system_reminder>.*?</system_reminder>", "", txt, flags=_re.S).strip()
            if not txt:
                continue
            out.append({"role": "她" if m.get("role") != "user" else "鱼片", "text": txt[:160]})
        return out
    except Exception:
        return []


def _backups():
    from .paths import BACKUP_DIR as d
    try:
        files = sorted(d.glob("yuyu-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)[:6]
        return [{"name": f.name, "kb": round(f.stat().st_size / 1024, 1),
                 "ts": int(f.stat().st_mtime)} for f in files]
    except Exception:
        return []


def _graph_edges():
    g = _load(EX / "graph.json", {}) or {}
    out = []
    for sub, rels in list(g.items())[:60]:
        for rel, objs in (rels or {}).items():
            for ob in (objs or [])[:4]:
                out.append([str(sub), str(rel), str(ob)])
    return out


def _daily(days: int = 7):
    import time as _t
    base = _day_start()
    buckets = [0] * days
    for r in _jsonl(NOTES / "notes.jsonl"):
        ts = float(r.get("ts") or 0)
        idx = int((base - ts) // 86400)
        if 0 <= idx < days:
            buckets[days - 1 - idx] += 1
    return buckets


def _jsonl(p: Path):
    try:
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    except Exception:
        return []


def build_status() -> dict:
    """把面板要的数据一次性算好（接口和快照共用）。"""
    notes = _jsonl(NOTES / "notes.jsonl")
    emo = _load(EX / "emotion.json", {})
    dims = emo.get("dims") or {}
    promises = [r for r in _load(EX / "promises.json", []) if r.get("status") == "open"]
    styles = [x for x in _load(EX / "style_queue.json", []) if x.get("status") == "pending"]
    return {
        "counts": {
            "notes": len(notes),
            "scars": len(_load(NOTES / "scars.json", [])),
            "habits": len(_load(NOTES / "habits.json", [])),
            "promises": len(promises),
            "people": len(_load(EX / "people.json", {})),
            "worldbook": len(_load(EX / "worldbook.json", [])),
            "graph": len(_load(EX / "graph.json", {})),
        },
        "emotion": {
            "state": emo.get("state", "calm"),
            "dims": {LABEL.get(k, k): v for k, v in dims.items()},
            "expectation": emo.get("expectation", 0),
            "blame": emo.get("blame", 0),
            "thoughts": [x.get("type") for x in (emo.get("thoughts") or [])],
        },
        "promises": [{"due": r.get("due"), "to": r.get("to"), "text": str(r.get("text"))[:80]}
                     for r in sorted(promises, key=lambda r: float(r.get("due") or 0))[:8]],
        "pending_style": [str(x.get("text"))[:80] for x in styles[-5:]],
        "people": [{"name": (v.get("name") or k),
                    "bits": "、".join(f"{lab}：{v.get(k)}" for k, lab in
                                      (("call", "称呼"), ("tone", "语气"), ("avoid", "禁忌"),
                                       ("relation", "关系"), ("note", "备注")) if v.get(k))[:120]}
                   for k, v in list(_load(EX / "people.json", {}).items())[:12]],
        "recent_notes": [{"ts": r.get("ts"), "text": str(r.get("text"))[:90],
                          "kind": r.get("kind", ""), "status": r.get("status", "")}
                         for r in notes[-40:]][::-1],
        "history": _load(EX / "emotion_history.json", [])[-96:],
        "chat": _recent_chat(24),
        "diaries": [{"date": d.get("date"), "text": str(d.get("text"))[:400]}
                    for d in (_load(SHARED / "diaries.json", []) or [])[-3:]],
        "dreams": [{"date": d.get("date"), "text": str(d.get("text"))[:400]}
                   for d in (_load(SHARED / "dreams.json", []) or [])[-3:]],
        "backups": _backups(),
        "edges": _graph_edges()[:40],
        "daily": _daily(7),
        "weekly": _weekly(8),
        "kinds": _kinds(notes),
        "notes_all": [{"idx": i, "ts": r.get("ts"), "text": str(r.get("text"))[:160],
                       "kind": r.get("kind", ""), "status": r.get("status", "")}
                      for i, r in list(enumerate(notes))[::-1][:60]],
        "switches": _switches(None),
        "persona": _persona_preview(),
        "today": {"notes": sum(1 for r in notes if float(r.get("ts") or 0) >= _day_start()),
                  "talks": _count_today()},
        "now": __import__("time").strftime("%Y-%m-%d %H:%M"),
    }


def _h(v) -> str:
    """HTML 转义。"""
    import html as _html
    try:
        return _html.escape(str(v if v is not None else ""))
    except Exception:
        return ""


def _svg_radar(dims: dict, size: int = 360) -> str:
    import math
    keys = list(dims.keys())
    if not keys:
        return ""
    n = len(keys)
    cx = cy = size / 2
    R = size / 2 - 34
    def pt(i, r):
        a = -math.pi / 2 + i * 2 * math.pi / n
        return cx + math.cos(a) * r, cy + math.sin(a) * r
    parts = []
    for ring in (0.25, 0.5, 0.75, 1.0):
        pts = " ".join(f"{pt(i, R*ring)[0]:.1f},{pt(i, R*ring)[1]:.1f}" for i in range(n))
        parts.append(f'<polygon points="{pts}" fill="none" stroke="rgba(128,140,170,.22)"/>')
    for i in range(n):
        x, y = pt(i, R)
        parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="rgba(128,140,170,.14)"/>')
    pts = []
    for i, k in enumerate(keys):
        v = max(0, min(100, float(dims[k]))) / 100
        x, y = pt(i, R * v)
        pts.append(f"{x:.1f},{y:.1f}")
    parts.append(f'<polygon points="{" ".join(pts)}" fill="rgba(255,95,126,.35)" stroke="#ff5f7e" stroke-width="1.6"/>')
    for i, k in enumerate(keys):
        x, y = pt(i, R + 17)
        parts.append(f'<text x="{x:.1f}" y="{y:.1f}" fill="#8791a6" font-size="11" text-anchor="middle">{k}</text>')
    return f'<svg viewBox="0 0 {size} {size}" width="100%" height="auto">{"".join(parts)}</svg>'


def _svg_bars(arr, labels, height: int = 150, color: str = "#ff5f7e") -> str:
    if not arr:
        return ""
    n = len(arr)
    W, H, x0, y0 = 440, height, 12, 8
    mx = max(arr) or 1
    bw = (W - x0 * 2) / n * 0.56
    parts = []
    for i, v in enumerate(arr):
        h = (H - 30) * (v / mx)
        x = x0 + (W - x0 * 2) * (i + 0.5) / n - bw / 2
        y = y0 + (H - 30) - h
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(h,1):.1f}" rx="3" fill="{color}" opacity=".85"/>')
        parts.append(f'<text x="{x+bw/2:.1f}" y="{y-4:.1f}" fill="#8791a6" font-size="10" text-anchor="middle">{v}</text>')
        parts.append(f'<text x="{x+bw/2:.1f}" y="{H-10}" fill="#8791a6" font-size="9" text-anchor="middle">{labels[i]}</text>')
    return f'<svg viewBox="0 0 {W} {H}" width="100%" height="auto">{"".join(parts)}</svg>'


def _svg_line(hist, w: int = 440, h: int = 170) -> str:
    if not hist or len(hist) < 2:
        return '<div class="empty">数据还不够，跑一会儿就有了</div>'
    W, H, x0, y0 = w, h, 12, 10
    def path(key):
        pts = []
        for i, row in enumerate(hist):
            v = max(0, min(100, float((row.get("dims") or {}).get(key, 50))))
            x = x0 + (W - 2 * x0) * i / (len(hist) - 1)
            y = y0 + (H - 30) * (1 - v / 100)
            pts.append(f"{x:.1f},{y:.1f}")
        return " ".join(pts)
    parts = []
    for key, color in (("joy", "#3ddc97"), ("stress", "#ff5f7e"), ("attach", "#7aa2ff")):
        parts.append(f'<polyline points="{path(key)}" fill="none" stroke="{color}" stroke-width="1.6"/>')
    for i, (key, color) in enumerate((("joy", "#3ddc97"), ("stress", "#ff5f7e"), ("attach", "#7aa2ff"))):
        parts.append(f'<rect x="{x0+i*92}" y="{H-16}" width="8" height="8" fill="{color}"/>')
        parts.append(f'<text x="{x0+i*92+12}" y="{H-8}" fill="#8791a6" font-size="11">{key}</text>')
    return f'<svg viewBox="0 0 {W} {H}" width="100%" height="auto">{"".join(parts)}</svg>'


def _svg_donut(kinds: dict, w: int = 320, h: int = 220) -> str:
    import math
    entries = sorted((kinds or {}).items(), key=lambda x: -x[1])[:6]
    if not entries:
        return '<div class="empty">还没有记忆</div>'
    total = sum(v for _, v in entries) or 1
    cx, cy, R, r = w / 2 - 56, h / 2, 72, 46
    colors = ["#ff5f7e", "#ffa26b", "#7aa2ff", "#3ddc97", "#c084fc", "#f472b6"]
    parts = []
    a = -math.pi / 2
    for i, (_, v) in enumerate(entries):
        ang = v / total * math.pi * 2
        a2 = a + ang
        large = 1 if ang > math.pi else 0
        x1, y1 = cx + R * math.cos(a), cy + R * math.sin(a)
        x2, y2 = cx + R * math.cos(a2), cy + R * math.sin(a2)
        x3, y3 = cx + r * math.cos(a2), cy + r * math.sin(a2)
        x4, y4 = cx + r * math.cos(a), cy + r * math.sin(a)
        parts.append(f'<path d="M{x1:.1f},{y1:.1f} A{R},{R} 0 {large} 1 {x2:.1f},{y2:.1f} '
                     f'L{x3:.1f},{y3:.1f} A{r},{r} 0 {large} 0 {x4:.1f},{y4:.1f} Z" fill="{colors[i%6]}"/>')
        a = a2
    for i, (k, v) in enumerate(entries):
        y = 34 + i * 24
        parts.append(f'<rect x="{cx+R+18}" y="{y-8}" width="9" height="9" fill="{colors[i%6]}"/>')
        parts.append(f'<text x="{cx+R+33}" y="{y}" fill="#8791a6" font-size="11">{_h(k)} {v}</text>')
    return f'<svg viewBox="0 0 {w} {h}" width="100%" height="auto">{"".join(parts)}</svg>'


def _svg_graph(edges, w: int = 620, h: int = 420) -> str:
    import math
    if not edges:
        return '<div class="empty">还没有关系数据（用 link_fact 教她几次就有了）</div>'
    nodes = []
    for a, _, b in edges:
        for n in (a, b):
            if n not in nodes:
                nodes.append(n)
    nodes = nodes[:26]
    cx, cy, R = w / 2, h / 2, min(w, h) / 2 - 46
    pos = {}
    for i, n in enumerate(nodes):
        a = -math.pi / 2 + i * 2 * math.pi / len(nodes)
        pos[n] = (cx + math.cos(a) * R, cy + math.sin(a) * R)
    parts = []
    for a, rel, b in edges:
        if a in pos and b in pos:
            parts.append(f'<line x1="{pos[a][0]:.1f}" y1="{pos[a][1]:.1f}" x2="{pos[b][0]:.1f}" y2="{pos[b][1]:.1f}" '
                         f'stroke="rgba(255,95,126,.22)"/>')
    for n in nodes:
        x, y = pos[n]
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="rgba(255,95,126,.85)"/>')
        parts.append(f'<text x="{x:.1f}" y="{y-10:.1f}" fill="#8791a6" font-size="11" text-anchor="middle">{_h(n[:10])}</text>')
    return f'<svg viewBox="0 0 {w} {h}" width="100%" height="auto">{"".join(parts)}</svg>'


def render_html(d: dict, q: dict = None) -> str:
    """整页由服务端画好；按钮是普通表单/链接，不需要 JS。"""
    import time as _t
    q = q or {}
    c = d.get("counts") or {}
    e = d.get("emotion") or {}
    t = d.get("today") or {}
    sw = d.get("switches") or {}
    st_map = {"calm": "平静", "hurt": "有点委屈", "afraid": "有点怕", "closed": "不想说话"}
    state = st_map.get(e.get("state"), e.get("state") or "?")

    radar = _svg_radar(e.get("dims") or {})
    days = ["6天前", "5天前", "4天前", "3天前", "2天前", "昨天", "今天"]
    daily = d.get("daily") or []
    week = _svg_bars(daily[-7:], days[-len(daily):] if daily else days)
    growth = _svg_bars(d.get("weekly") or [], [("本周" if i == 7 else f"{7-i}周前") for i in range(8)], 150, "#7aa2ff")
    spark = _svg_line(d.get("history") or [])
    donut = _svg_donut(d.get("kinds") or {})
    graph = _svg_graph(d.get("edges") or [])

    def fmt(ts):
        try:
            return _t.strftime("%m-%d %H:%M", _t.localtime(float(ts or 0)))
        except Exception:
            return ""

    cards = [("记忆", c.get("notes"), "条"), ("疤", c.get("scars"), "处"), ("习惯", c.get("habits"), "个"),
             ("承诺", c.get("promises"), "件"), ("人物", c.get("people"), "人"),
             ("世界书", c.get("worldbook"), "张"), ("关系网", c.get("graph"), "点")]
    cards_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v or 0}<span class="u">{u}</span></div></div>'
        for k, v, u in cards)

    dims_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v sm">{v}</div>'
        f'<div class="track"><div class="fill" style="width:{max(0,min(100,int(v)))}%"></div></div></div>'
        for k, v in (e.get("dims") or {}).items())

    thoughts = "".join(f'<span class="chip">{_h(x)}</span>' for x in (e.get("thoughts") or [])) or '<span class="chip d">现在什么都不想</span>'

    switch_html = "".join(
        f'<a class="chip {"g" if sw.get(k2) else "d"}" href="/act?do=toggle&key={k2}&back=/#overview">{label} {"开" if sw.get(k2) else "关"}</a>'
        for label, k2 in (("主动说话", "proactive"), ("情绪核心", "emotion"), ("自动备份", "autobackup")))

    notes_rows = d.get("notes_all") or d.get("recent_notes") or []
    notes_html = "".join(
        f'<div class="it"><div class="t">#{n.get("idx","")} {fmt(n.get("ts"))}</div>'
        f'<div class="x">{_h(n.get("text"))}</div>'
        f'<a class="mini" href="/act?do=note_pin&i={n.get("idx","")}&back=/#memory">置顶</a>'
        f'<a class="mini ghost" href="/act?do=note_delete&i={n.get("idx","")}&back=/#memory">删</a></div>'
        for n in notes_rows) or '<div class="empty">还没记什么</div>'

    chat_html = "".join(
        f'<div class="it"><div class="t">{_h(r.get("role"))}</div><div class="x">{_h(r.get("text"))}</div></div>'
        for r in (d.get("chat") or [])) or '<div class="empty">还没有对话</div>'

    promise_html = "".join(
        f'<div class="it"><div class="t">{fmt(p.get("due"))}</div>'
        f'<div class="x">跟{_h(p.get("to"))}说：{_h(p.get("text"))}</div></div>'
        for p in (d.get("promises") or [])) or '<div class="empty">没有待办</div>'

    people_html = "".join(
        f'<div class="it"><div class="t">{_h(x.get("name"))}</div><div class="x">{_h(x.get("bits"))}</div></div>'
        for x in (d.get("people") or [])) or '<div class="empty">还没认识谁</div>'

    diary_html = ""
    for title, key, color in (("日记", "diaries", "#ffb3c2"), ("梦", "dreams", "#9db4ff")):
        rows = d.get(key) or []
        if rows:
            diary_html += (f'<div class="panel" style="margin-bottom:12px"><h3 style="color:{color}">{title}</h3>' +
                           "".join(f'<div class="it"><div class="t">{_h(r.get("date"))}</div>'
                                   f'<div class="x">{_h(r.get("text"))}</div></div>' for r in rows) + "</div>")
    diary_html = diary_html or '<div class="panel"><div class="empty">她还没写</div></div>'

    style_html = "".join(
        f'<div class="it"><div class="x">{_h(x)}</div>'
        f'<a class="mini" href="/act?do=approve&i={i}&back=/#style">采纳</a>'
        f'<a class="mini ghost" href="/act?do=reject&i={i}&back=/#style">不要</a></div>'
        for i, x in enumerate(d.get("pending_style") or [])) or '<div class="empty">没有待批</div>'

    backups_html = "".join(
        f'<div class="it"><div class="t">{fmt(b.get("ts"))}</div>'
        f'<div class="x">{_h(b.get("name"))} · {b.get("kb")} KB</div>'
        f'<a class="mini ghost" href="/act?do=restore&name={urllib.parse.quote(str(b.get("name")))}&back=/#system">恢复</a></div>'
        for b in (d.get("backups") or [])) or '<div class="empty">还没有备份</div>'

    kw = str(q.get("q") or "").strip()
    search_result = ""
    if kw:
        hits = []
        try:
            import sqlite3
            from . import webui as _self
            if not _self.FTS_DB.exists():
                pass
            con = sqlite3.connect(_self.FTS_DB)
            cur = con.execute("SELECT text, kind FROM mem WHERE mem MATCH ? ORDER BY rank LIMIT 10", (f'"{kw}"',))
            hits = cur.fetchall()
            con.close()
        except Exception:
            hits = []
        search_result = ('<div class="panel" style="margin-top:12px"><h3>搜索结果</h3>' +
                         ("".join(f'<div class="it"><div class="t">{_h(k)}</div><div class="x">{_h(tx)}</div></div>'
                                  for tx, k in hits) or '<div class="empty">没搜到</div>') + "</div>")

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>御坂 · 控制台</title>
<style>
:root{{--bg:#080a0f;--panel:#12161f;--line:#1f2533;--txt:#e9edf6;--dim:#8791a6}}
*{{box-sizing:border-box}}
body{{margin:0;background:radial-gradient(1000px 520px at 8% -12%,#1d1224,#080a0f 62%);color:var(--txt);
 font:14px/1.6 system-ui,"Segoe UI","Microsoft YaHei",sans-serif;display:flex;min-height:100vh}}
aside{{width:186px;flex:none;border-right:1px solid var(--line);padding:18px 12px;background:rgba(10,13,19,.85);
 position:sticky;top:0;height:100vh;overflow:auto}}
.brand{{font-size:16px;font-weight:680;margin:2px 6px}}.brand small{{display:block;color:var(--dim);font-size:11.5px;margin-top:3px}}
nav{{margin-top:16px}}
nav a{{display:flex;gap:9px;padding:8px 11px;border-radius:10px;color:var(--dim);text-decoration:none;font-size:13.5px;margin-bottom:2px}}
nav a:hover{{background:var(--panel);color:var(--txt)}}
main{{flex:1;padding:22px 26px;min-width:0}}
h1{{font-size:21px;margin:0 0 4px}}.sub{{color:var(--dim);font-size:12.5px;margin-bottom:14px}}
section{{margin-bottom:28px;padding-top:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:11px}}
.card{{background:linear-gradient(180deg,#161b25,#12161f);border:1px solid var(--line);border-radius:14px;padding:13px 14px}}
.card .k{{color:var(--dim);font-size:12.5px}}.card .v{{font-size:24px;font-weight:680;margin-top:4px}}
.card .v.sm{{font-size:19px}}.card .u{{font-size:12px;color:var(--dim);margin-left:3px}}
.track{{height:5px;background:rgba(128,140,170,.18);border-radius:3px;margin-top:8px;overflow:hidden}}
.fill{{height:100%;background:linear-gradient(90deg,#ff5f7e,#ffa26b)}}
.row2{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}.row2>*{{min-width:0}}
@media (max-width:900px){{.row2{{grid-template-columns:1fr}}}}
.panel{{background:#12161f;border:1px solid var(--line);border-radius:16px;padding:14px 16px}}
.panel h3{{margin:0 0 10px;font-size:13px;color:var(--dim)}}
.chips{{display:flex;gap:7px;flex-wrap:wrap}}
.chip{{padding:5px 11px;border-radius:999px;font-size:12px;text-decoration:none;
 background:rgba(255,95,126,.1);border:1px solid rgba(255,95,126,.26);color:#ff9db0}}
.chip.g{{background:rgba(61,220,151,.1);border-color:rgba(61,220,151,.3);color:#7ee6b8}}
.chip.d{{background:transparent;border-color:var(--line);color:var(--dim)}}
.it{{display:flex;gap:8px;align-items:flex-start;padding:9px 0;border-bottom:1px solid #1a202c;font-size:13px}}
.it:last-child{{border-bottom:0}}
.it .t{{color:var(--dim);font-size:11.5px;min-width:88px;white-space:nowrap}}
.it .x{{flex:1;word-break:break-word}}
.empty{{color:var(--dim);padding:10px 0;opacity:.8}}
.state{{display:inline-block;padding:4px 12px;border-radius:999px;background:rgba(255,95,126,.14);color:#ff96a8;font-size:12.5px}}
input[type=text]{{background:#0e1218;border:1px solid var(--line);color:var(--txt);border-radius:9px;padding:8px 11px;font-size:13px}}
button{{background:linear-gradient(180deg,#ff5f7e,#e04a68);border:0;color:#fff;padding:9px 16px;border-radius:10px;font-size:13px;cursor:pointer}}
a.mini{{padding:3px 9px;border-radius:7px;font-size:11.5px;text-decoration:none;background:rgba(255,95,126,.14);color:#ffb3c2}}
a.mini.ghost{{background:#1a2130;color:var(--dim);border:1px solid var(--line)}}
</style></head><body>
<aside><div class="brand">御坂 · 控制台<small>在线 · {d.get('now','')}</small></div>
<nav>
<a href="#overview">▤ 总览</a><a href="#emotion">♡ 情绪</a><a href="#memory">▦ 记忆</a>
<a href="#chat">💬 对话</a><a href="#graph">◈ 关系网</a><a href="#diary">✎ 日记</a>
<a href="#promise">◷ 承诺</a><a href="#people">☺ 人物</a><a href="#style">✦ 风格</a>
<a href="#system">⚙ 系统</a>
</nav></aside>
<main>
<section id="overview"><h1>总览</h1>
<div class="sub">状态：<span class="state">{state}</span> ｜ 今天记了 {t.get('notes',0)} 条、说了 {t.get('talks',0)} 句</div>
<div class="grid">{cards_html}</div>
<div class="row2" style="margin-top:14px">
 <div class="panel"><h3>情绪雷达</h3>{radar}</div>
 <div class="panel"><h3>近七天</h3>{week}
   <h3 style="margin-top:14px">开关</h3><div class="chips">{switch_html}</div>
   <h3 style="margin-top:14px">跟她说一句</h3>
   <form action="/act" method="get" style="display:flex;gap:8px;flex-wrap:wrap">
     <input type="hidden" name="do" value="say"><input type="hidden" name="back" value="/#overview">
     <input type="text" name="text" placeholder="以鱼片身份发到 QQ 私聊…" style="flex:1;min-width:200px">
     <button type="submit">发给她</button>
   </form>
   <div class="chips" style="margin-top:10px">
     <a class="chip" href="/act?do=reach&back=/#overview">让她主动说</a>
     <a class="chip" href="/act?do=backup&back=/#system">备份</a>
     <a class="chip" href="/act?do=digest&back=/#system">周记</a>
     <a class="chip d" href="/tts" target="_blank">听她的声音</a>
   </div>
 </div>
</div>
<div class="panel" style="margin-top:14px"><h3>心里挂着</h3><div class="chips">{thoughts}</div></div>
</section>

<section id="emotion"><h1>情绪</h1>
<div class="sub">期待 {e.get('expectation',0)} ｜ 责怪 {round(float(e.get('blame') or 0))}</div>
<div class="grid">{dims_html}</div>
<div class="chips" style="margin-top:12px">
 <a class="chip d" href="/act?do=tick&back=/#emotion">跑一次心跳</a>
 <a class="chip d" href="/act?do=reset&back=/#emotion">重置情绪</a>
</div>
<div class="row2" style="margin-top:14px">
 <div class="panel"><h3>心境曲线</h3>{spark}</div>
 <div class="panel"><h3>成长曲线（近 8 周）</h3>{growth}</div>
</div></section>

<section id="memory"><h1>记忆</h1>
<div class="row2">
 <div class="panel"><h3>类型分布</h3>{donut}</div>
 <div class="panel"><h3>搜索</h3>
  <form action="/#memory" method="get" style="display:flex;gap:8px">
    <input type="text" name="q" placeholder="关键词…" value="{_h(kw)}" style="flex:1">
    <button type="submit">搜</button>
  </form>
  {search_result}
 </div>
</div>
<div class="panel" style="margin-top:14px"><h3>审核台（置顶 / 删除）</h3>{notes_html}</div></section>

<section id="chat"><h1>对话</h1><div class="panel">{chat_html}</div></section>
<section id="graph"><h1>关系网</h1><div class="panel">{graph}</div></section>
<section id="diary"><h1>日记</h1>{diary_html}</section>
<section id="promise"><h1>承诺</h1><div class="panel">{promise_html}</div></section>
<section id="people"><h1>人物</h1><div class="panel">{people_html}</div></section>
<section id="style"><h1>风格</h1><div class="sub">待批的说话方式，采纳了才生效</div><div class="panel">{style_html}</div></section>
<section id="system"><h1>系统</h1>
<div class="row2">
 <div class="panel"><h3>备份</h3>{backups_html}
   <div class="chips" style="margin-top:10px"><a class="chip" href="/act?do=backup&back=/#system">立即备份</a></div></div>
 <div class="panel"><h3>人格（只读）</h3>
   <pre style="white-space:pre-wrap;max-height:320px;overflow:auto;color:#9aa5ba;font-size:12px">{_h(d.get('persona') or '')}</pre></div>
</div></section>
</main></body></html>"""


def write_snapshot(plugin) -> str:
    """把状态写进页面目录，页面直接读文件（不依赖页面桥）。"""
    try:
        import time as _t
        data = build_status()
        data["switches"] = _switches(plugin)
        out = Path(__file__).resolve().parent.parent / "pages" / "控制台" / "data.json"
        out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(out)
    except Exception as e:
        logger.warning(f"[御坂WebUI] 快照失败: {e}")
        return ""


def register_webui(plugin, context):
    async def handle_status():
        try:
            notes = _jsonl(NOTES / "notes.jsonl")
            emo = _load(EX / "emotion.json", {})
            dims = emo.get("dims") or {}
            promises = [r for r in _load(EX / "promises.json", []) if r.get("status") == "open"]
            styles = [x for x in _load(EX / "style_queue.json", []) if x.get("status") == "pending"]
            data = {
                "persona": _persona_preview(),
                "switches": _switches(plugin),
                "kinds": _kinds(notes),
                "counts": {
                    "notes": len(notes),
                    "scars": len(_load(NOTES / "scars.json", [])),
                    "habits": len(_load(NOTES / "habits.json", [])),
                    "promises": len(promises),
                    "people": len(_load(EX / "people.json", {})),
                    "worldbook": len(_load(EX / "worldbook.json", [])),
                    "graph": len(_load(EX / "graph.json", {})),
                },
                "emotion": {
                    "state": emo.get("state", "calm"),
                    "dims": {LABEL.get(k, k): v for k, v in dims.items()},
                    "expectation": emo.get("expectation", 0),
                    "blame": emo.get("blame", 0),
                    "thoughts": [x.get("type") for x in (emo.get("thoughts") or [])],
                },
                "promises": [
                    {"due": r.get("due"), "to": r.get("to"), "text": str(r.get("text"))[:80]}
                    for r in sorted(promises, key=lambda r: float(r.get("due") or 0))[:8]
                ],
                "pending_style": [str(x.get("text"))[:80] for x in styles[-5:]],
                "people": [
                    {"name": (v.get("name") or k),
                     "bits": "、".join(f"{lab}：{v.get(k)}" for k, lab in
                                       (("call", "称呼"), ("tone", "语气"), ("avoid", "禁忌"),
                                        ("relation", "关系"), ("note", "备注")) if v.get(k))[:120]}
                    for k, v in list(_load(EX / "people.json", {}).items())[:12]
                ],
                "recent_notes": [{"ts": r.get("ts"), "text": str(r.get("text"))[:90],
                                  "kind": r.get("kind", ""), "status": r.get("status", "")}
                                 for r in notes[-40:]][::-1],
                "history": _load(EX / "emotion_history.json", [])[-96:],
                "chat": _recent_chat(24),
                "diaries": [{"date": d.get("date"), "text": str(d.get("text"))[:400]}
                            for d in (_load(SHARED / "diaries.json", []) or [])[-3:]],
                "dreams": [{"date": d.get("date"), "text": str(d.get("text"))[:400]}
                           for d in (_load(SHARED / "dreams.json", []) or [])[-3:]],
                "backups": _backups(),
                "edges": _graph_edges()[:40],
                "daily": _daily(7),
                "weekly": _weekly(8),
                "today": {
                    "notes": sum(1 for r in notes if float(r.get("ts") or 0) >= _day_start()),
                    "talks": _count_today(),
                },
                "now": time.strftime("%Y-%m-%d %H:%M"),
            }
            return json_response({"status": "ok", "data": data})
        except Exception as e:
            logger.exception(f"[御坂WebUI] status 失败: {e}")
            return json_response({"status": "error", "message": str(e)}, status_code=500)

    async def handle_act():
        """页面上的按钮：备份 / 周记 / 批准风格。"""
        try:
            body = await request.json(default={}) or {}
            do = str(body.get("do") or "")
            if do == "backup":
                r = await plugin.tool_backup(None)
                return json_response({"status": "ok", "data": {"msg": str(r)[:120]}})
            if do == "digest":
                r = await plugin.tool_digest(None, 7)
                return json_response({"status": "ok", "data": {"msg": str(r)[:120]}})
            if do == "say":
                text = str(body.get("text") or "").strip()[:300]
                if not text:
                    return json_response({"status": "ok", "data": {"msg": "空消息"}})
                from astrbot.core.message.message_event_result import MessageChain
                from astrbot.api.message_components import Plain
                await plugin.context.send_message(
                    RUNTIME.get("owner_umo", ""),
                    MessageChain([Plain(text=text)]),
                )
                return json_response({"status": "ok", "data": {"msg": "已发出"}})
            if do == "reach":
                import asyncio as _a
                _a.create_task(plugin._offer())
                return json_response({"status": "ok", "data": {"msg": "已经问她要不要说话了"}})
            if do == "toggle":
                key = str(body.get("key") or "")
                val = bool(body.get("value"))
                conf = _load(plugin.CONF, {}) if hasattr(plugin, "CONF") else {}
                if key in ("proactive", "emotion", "autobackup"):
                    conf[key] = val
                    plugin.conf = {**(getattr(plugin, "conf", {}) or {}), key: val}
                    try:
                        (Path(plugin.DATA) / "extra_conf.json").write_text(
                            json.dumps(conf, ensure_ascii=False, indent=1), encoding="utf-8")
                    except Exception:
                        pass
                    return json_response({"status": "ok", "data": {"msg": f"{key} = {val}"}})
                return json_response({"status": "ok", "data": {"msg": "未知开关"}})
            if do == "tts":
                text = str(body.get("text") or "").strip()[:120]
                if not text:
                    return json_response({"status": "ok", "data": {"msg": "空文本"}})
                try:
                    import base64
                    import urllib.request as _u
                    payload = json.dumps({"text": text, "text_lang": "ja",
                                          "media_type": "wav", "emotion": "soft"}).encode()
                    tts = RUNTIME.get("tts_proxy", "")
                    if not tts:
                        return json_response({"status": "ok", "data": {"msg": "未配置 TTS 地址"}})
                    req = _u.Request(tts, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
                    with _u.urlopen(req, timeout=120) as resp:
                        raw = resp.read()
                    return json_response({"status": "ok", "data": {
                        "msg": f"合成好了（{len(raw)//1024} KB）",
                        "audio": "data:audio/wav;base64," + base64.b64encode(raw).decode(),
                    }})
                except Exception as e:
                    return json_response({"status": "ok", "data": {"msg": f"合成失败：{str(e)[:60]}"}})
            if do == "notes_list":
                return json_response({"status": "ok", "data": {"notes": _notes_page(body.get("offset", 0))}})
            if do in ("note_update", "note_delete", "note_pin"):
                return json_response({"status": "ok", "data": {
                    "msg": _note_edit(do, body.get("idx"), body.get("text"))}})
            if do == "forget":
                kw = str(body.get("q") or "").strip()
                if not kw:
                    return json_response({"status": "ok", "data": {"msg": "要个关键词"}})
                import io as _io
                f = NOTES / "notes.jsonl"
                try:
                    rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
                except Exception:
                    rows = []
                keep = [r for r in rows if kw not in str(r.get("text", ""))]
                removed = len(rows) - len(keep)
                try:
                    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in keep) + ("\n" if keep else ""),
                                 encoding="utf-8")
                except Exception as e:
                    return json_response({"status": "ok", "data": {"msg": f"删除失败：{e}"}})
                return json_response({"status": "ok", "data": {"msg": f"删掉了 {removed} 条"}})
            if do == "reset_emotion":
                f = EX / "emotion.json"
                try:
                    import time as _t
                    f.write_text(json.dumps({
                        "dims": {k: 50 for k in ("attach", "curious", "ruminate", "duty", "social",
                                                 "fatigue", "intimacy", "stress", "joy")},
                        "expectation": 0.0, "blame": 0.0, "thoughts": [],
                        "last_tick": _t.time(), "state": "calm",
                        "last_inject": 0.0, "last_inject_state": "",
                        "fake_comfort": 0, "last_harsh": 0.0, "last_soft": 0.0,
                    }, ensure_ascii=False, indent=1), encoding="utf-8")
                except Exception as e:
                    return json_response({"status": "error", "message": str(e)}, status_code=500)
                return json_response({"status": "ok", "data": {"msg": "情绪已重置"}})
            if do == "tick":
                try:
                    plugin._emo_tick()
                except Exception:
                    pass
                return json_response({"status": "ok", "data": {"msg": "心跳已触发"}})
            if do == "restore":
                name = str(body.get("name") or "")
                r = _restore_backup(plugin, name)
                return json_response({"status": "ok", "data": {"msg": r}})
            if do == "search":
                kw = str(body.get("q") or "").strip()
                if not kw:
                    return json_response({"status": "ok", "data": {"hits": []}})
                hits = []
                try:
                    plugin._fts_build() if not plugin.FTS_DB.exists() else None
                    for t, k, _ in plugin._fts_search(kw, 10):
                        hits.append(f"[{k}] {str(t)[:80]}")
                except Exception:
                    pass
                if not hits:
                    for r in plugin._read_jsonl(plugin.NOTES):
                        if kw in str(r.get("text", "")):
                            hits.append(str(r.get("text"))[:80])
                return json_response({"status": "ok", "data": {"hits": hits[:10]}})
            if do in ("approve", "reject"):
                idx = int(body.get("index") or 0)
                r = await plugin.tool_review_style(None, idx, do == "approve")
                return json_response({"status": "ok", "data": {"msg": str(r)[:120]}})
            return json_response({"status": "error", "message": "不认识的动作"}, status_code=400)
        except Exception as e:
            logger.exception(f"[御坂WebUI] 动作失败: {e}")
            return json_response({"status": "error", "message": str(e)[:120]}, status_code=500)

    try:
        write_snapshot(plugin)
    except Exception:
        pass
    try:
        context.register_web_api(f"/{PLUGIN}/status", handle_status, ["GET"], "御坂状态")
        context.register_web_api(f"/{PLUGIN}/act", handle_act, ["POST"], "御坂操作")
        logger.info(f"[御坂WebUI] 注册 GET /{PLUGIN}/status")
    except Exception as exc:
        logger.error(f"[御坂WebUI] 注册失败: {exc}")
