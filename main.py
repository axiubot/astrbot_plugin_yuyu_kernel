# -*- coding: utf-8 -*-
r"""御坂的"内核"：把原来六个小插件合并成一个。

包含：
  1. 群聊唤醒    谁的话算"叫她"，接话窗口
  2. 内在状态    记事 / 执念 / 疤 / 习惯 / 心境（4 个工具 + 注入）
  3. 作息        她说睡就睡、说醒就醒，主人一说话自动喊醒
  4. 主动找人    隔一阵给她一次机会，说不说、多久后再问都由她定
  5. 工具裁剪    摘掉她用不上的群管理工具
  6. 看媒体      视频抽帧+转写、动图抽帧

依赖：ffmpeg（可选，视频/动图抽帧）
      faster-whisper（可选，视频转写）
      alive.py（可选，睡/忙判断）
"""
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply, Video
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.custom_filter import CustomFilter

try:
    import alive as ALIVE      # 可选：同目录若有 alive.py，用来判断睡/忙
except Exception:
    ALIVE = None

LOG = "[内核]"
OWNER_IDS = set()          # 由配置 owner_id 填入
OWNER_UMO = ""             # 由配置 owner_umo 填入
OWNER_NAME = "主人"         # 由配置 owner_name 填入
NAME_HINTS = ("御坂", "御板")   # 由配置 wake_names 填入

# ── 群唤醒 ─────────────────────────────────
CHIME_PROB = 0.0
CHIME_COOLDOWN = 90
CHAT_WINDOW = 120

# ── 内在状态（路径全部由 core.paths 解析）────
from .core.paths import NOTES as _NOTES_DIR, EX as _EX_DIR, SHARED as _SHARED_DIR
from .core.paths import BACKUP_DIR, DIGEST_DIR

DATA = _NOTES_DIR

# ── 约定回访 / 群友档案（借鉴 Super_AstrBot，只注入事实不改人格）──
EX_DIR = _EX_DIR
PROMISES = EX_DIR / "promises.json"

# ── 世界书 / 轻量知识图谱 / 记忆衰减 ──────────
WORLD_BOOK = EX_DIR / "worldbook.json"      # [{keys:[], fact:"", on:true}]
GRAPH = EX_DIR / "graph.json"               # {实体: {关系: [目标...]}}
DECAY = EX_DIR / "decay.json"

# ── 人格演化（只调状态不改文本）/ 三级记忆 / 自动审核 ──
EVO_STATE = EX_DIR / "evo.json"            # {stat:{...}, state:"", updated:ts}
AUDIT_WORDS = ("自杀", "杀了你", "去死", "开房", "约炮", "裸聊", "赌博", "毒品")


# ── 全文检索（FTS5） / 身份归属 ──────────
FTS_DB = EX_DIR / "search.db"
IDENTITY = EX_DIR / "identity.json"       # {uid: {name, umo, first, last}}


STYLE_QUEUE = EX_DIR / "style_queue.json"   # [{text, status: pending/approved/rejected}]


SHARED_DIR = _SHARED_DIR


# ── 情绪核心：九维 + 慢心跳 + 期待/责怪 ─────
EMO_FILE = EX_DIR / "emotion.json"
EMO_HISTORY = EX_DIR / "emotion_history.json"
EMO_TICK = 45 * 60
EMO_DIMS = ("attach", "curious", "ruminate", "duty", "social",
            "fatigue", "intimacy", "stress", "joy")
EMO_LABEL = {"attach": "依恋", "curious": "好奇", "ruminate": "反刍", "duty": "责任",
             "social": "社交", "fatigue": "疲惫", "intimacy": "亲密",
             "stress": "压力", "joy": "喜悦"}
EMO_HARSH = ("滚", "烦", "闭嘴", "讨厌", "笨", "傻", "别烦我", "走开", "没用")
EMO_SOFT = ("乖", "抱", "摸摸", "喜欢你", "对不起", "别生气", "想你了", "亲")
EMO_PRAISE = ("厉害", "好看", "聪明", "可爱", "乖", "做得好", "真棒")
EMO_THOUGHTS = ("想被夸", "想被抱", "想找他说话", "想问他今天怎样",
                "想找点事做", "想安静", "想撒娇", "在等他回话")


def _emo_default():
    return {"dims": {k: 50 for k in EMO_DIMS}, "expectation": 0.0, "blame": 0.0,
            "thoughts": [], "last_tick": time.time(), "state": "calm",
            "last_inject": 0.0, "last_inject_state": "", "fake_comfort": 0,
            "last_harsh": 0.0, "last_soft": 0.0}


def _emo_load():
    d = _ex_load(EMO_FILE, {}) or {}
    base = _emo_default()
    if not isinstance(d, dict):
        return base
    for k, v in base.items():
        if k not in d:
            d[k] = v
        elif k == "dims" and isinstance(d[k], dict):
            for dim in EMO_DIMS:
                d[k].setdefault(dim, 50)
    # 读的时候顺手重算状态（避免"责怪已经归零还挂着委屈"）
    try:
        d["state"] = _emo_state(d.get("blame", 0), (d.get("dims") or {}).get("stress", 50))
    except Exception:
        pass
    return d


def _emo_state(blame, stress):
    """状态只看"比基线高多少"，常态不喊委屈。"""
    b = float(blame)
    over = max(0.0, float(stress) - 50.0)
    if b >= 80 or over >= 45:
        return "closed"
    if b >= 55 or over >= 32:
        return "afraid"
    if b >= 30 or over >= 20:
        return "hurt"
    return "calm"
               # {记忆hash: 上次引用时间}

PEOPLE = EX_DIR / "people.json"


def _ex_load(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def _ex_save(p, d):
    try:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.warning(f"{LOG} 保存 {Path(p).name} 失败: {e}")


def _decay_pick(rows, fresh_days=30, fresh_max=12, old_max=4):
    """记忆衰减（轻量版）：新的照常注入，旧的只留少量、标注变淡。"""
    now = time.time()
    fresh, old = [], []
    for r in rows:
        ts = float(r.get("ts") or 0)
        (fresh if now - ts <= fresh_days * 86400 else old).append(r)
    fresh = fresh[-fresh_max:]
    old = old[-old_max:]
    return fresh, old


def _fmt_when(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(float(ts or 0)))

NOTES = DATA / "notes.jsonl"
SCARS = DATA / "scars.json"
HABITS = DATA / "habits.json"
MOOD = WS / "mood_state.json"
MAX_NOTES, MAX_OPEN, MAX_SCARS, MAX_HABITS = 6, 4, 5, 8

# ── 作息 ───────────────────────────────────
SLEEP_STATE = WS / "sleep_state.json"

# ── 主动 ───────────────────────────────────
REACH_STATE = WS / "reach_out_state.json"
MIN_GAP_SECONDS = 20 * 60         # 安静 20 分钟就可以考虑（她嫌 45 分钟太久）
OPPORTUNITY_EVERY = (40, 70)      # 每 40-70 分钟给一次机会
ACTIVE_HOURS = (9, 24)
REACH_DAILY_MAX = 5               # 一天最多喊 5 次（主人后来同意了）

# ── 媒体 ───────────────────────────────────
FF = "ffmpeg"     # 由配置 ffmpeg_path 覆盖
ASR_PY = ""       # 可选：带 faster-whisper 的 python
MEDIA_TMP = WS / "tmp" / "media"
ASR_SCRIPT = MEDIA_TMP / "asr_one.py"
ASR_CODE = """
import sys
from faster_whisper import WhisperModel
m = WhisperModel("medium", device="cpu", compute_type="int8", cpu_threads=4)
segs, _ = m.transcribe(sys.argv[1], vad_filter=True, beam_size=1)
print(" ".join(s.text.strip() for s in segs)[:500])
"""

# ── 工具裁剪 ───────────────────────────────
TRIM = [
    "qq_group_manage", "qq_group_member_manage", "qq_group_request",
    "qq_essence", "qq_notice", "qq_message_manage", "qq_friend_manage",
]

# 运行时状态
_active_chat = {}
_last_chime = {}
_last_called = {}


# ══════════════════════════════════════════
#  小工具
# ══════════════════════════════════════════
def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_jsonl(p: Path, limit: int = 200):
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _write_jsonl(p: Path, rows):
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")


def _mark(event):
    try:
        event.is_wake = True
        event.is_at_or_wake_command = True
    except Exception:
        pass


def _mood_text() -> str:
    d = _read_json(MOOD, {})
    if not d:
        return ""
    try:
        v = float(d.get("valence", 0.15))
        e = float(d.get("energy", 0.55))
        reason = (d.get("reason") or "").strip()
    except Exception:
        return ""
    if e < 0.3:
        line, hint = "你现在很累、没劲。", "说话更短，别长篇解释，能不接话就不接。"
    elif e < 0.5:
        line, hint = "你有点累。", "句子短一些，别主动展开话题。"
    elif v > 0.5 and e > 0.6:
        line, hint = "你精神不错，心情也好。", "可以多说一点，愿意接话、愿意玩。"
    elif v > 0.25:
        line, hint = "你心情还行。", "正常说话就行。"
    elif v < -0.25:
        line, hint = "你心情不太好。", "话少一点，别假装没事，也别发脾气。"
    else:
        line, hint = "你没什么特别的情绪。", "平常那样说。"
    if reason:
        line += f"（因为：{reason}）"
    return f"{line}\n{hint}"


# ══════════════════════════════════════════
#  跨会话自述记忆（原 self_context）
# ══════════════════════════════════════════
from collections import deque as _deque

SELF_KEEP = 20
SELF_TTL = 3 * 3600
SELF_INJECT_N = 4
SELF_STORE = EX_DIR / "recent_self.json"
_self_buf = _deque(maxlen=SELF_KEEP)

_GROUP_NAMES = {
    "764611007": "三只孤寡的日常",
    "180316427": "蛊王争霸",
    "962992485": "洗头乐、清泠、御坂",
    "1067973825": "洗头乐、night、御坂",
    "1097114307": "使命召唤开黑群",
}


def _self_load():
    try:
        rows = json.loads(SELF_STORE.read_text(encoding="utf-8"))
    except Exception:
        return
    now = time.time()
    kept = 0
    for r in rows[-SELF_KEEP:]:
        try:
            ts = float(r["ts"]) if isinstance(r, dict) else float(r[0])
            umo = str(r["umo"]) if isinstance(r, dict) else str(r[1])
            say = (str(r.get("say", "")) if isinstance(r, dict) else (str(r[2]) if len(r) >= 4 else ""))
            text = str(r["text"]) if isinstance(r, dict) else str(r[3] if len(r) >= 4 else r[2])
        except Exception:
            continue
        if now - ts < SELF_TTL and text:
            _self_buf.append((ts, umo, say, text))
            kept += 1
    if kept:
        logger.info(f"{LOG} 自述记忆恢复 {kept} 条")


def _self_save():
    try:
        SELF_STORE.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"ts": round(t, 3), "umo": u, "say": s, "text": x} for t, u, s, x in _self_buf]
        SELF_STORE.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _self_strip(t: str) -> str:
    t = re.sub(r"<system_reminder>.*?</system_reminder>", "", t, flags=re.S)
    t = re.sub(r"\[\[[^\]]+\]\]", "", t)
    return t.strip()


def _self_where(umo: str) -> str:
    if "GroupMessage" in umo:
        gid = umo.split(":")[-1]
        return f"群「{_GROUP_NAMES.get(gid, gid)}」"
    if "FriendMessage" in umo:
        return "私聊"
    return "别处"


# ══════════════════════════════════════════
#  群唤醒过滤器
# ══════════════════════════════════════════
class WakeFilter(CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        try:
            if event.is_private_chat():
                return False
            text = (event.message_str or "").strip()
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return False
            if text.startswith("/"):
                return False

            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id())
            now = time.time()
            key = (gid, uid)

            at_me = False
            for seg in event.get_messages():
                if isinstance(seg, At) and str(seg.qq) == str(event.get_self_id()):
                    at_me = True
                    break
            if at_me:
                _active_chat[key] = now
                return False          # 交给系统默认的 @ 唤醒

            if uid not in OWNER_IDS:
                if now - _active_chat.get(key, 0) < CHAT_WINDOW:
                    _active_chat[key] = now
                    return True
                return False

            # 主人本人
            for seg in event.get_messages():
                if isinstance(seg, At) and str(seg.qq) != str(event.get_self_id()):
                    return False
                if isinstance(seg, Reply) and str(getattr(seg, "sender_id", "")) != str(event.get_self_id()):
                    return False

            if any(h in text for h in NAME_HINTS):
                _mark(event)
                _active_chat[key] = now
                logger.info(f"{LOG} 主人叫她 [{gid}] {text[:20]}")
                return True
            if now - _active_chat.get(key, 0) < CHAT_WINDOW:
                _mark(event)
                _active_chat[key] = now
                return True
            return False
        except Exception as e:
            logger.error(f"{LOG} 唤醒判断出错: {e}")
            return False


# ══════════════════════════════════════════
#  媒体处理
# ══════════════════════════════════════════
def _run(cmd, timeout=180):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "ignore"), (r.stderr or b"").decode("utf-8", "ignore")
    except Exception as e:
        return "", str(e)


def _duration(src: str) -> float:
    _, err = _run([FF, "-hide_banner", "-i", src], timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", err or "")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _frames(src: str, n: int = 0) -> list:
    MEDIA_TMP.mkdir(parents=True, exist_ok=True)
    dur = _duration(src)
    if n <= 0:
        n = 6 if dur <= 15 else (9 if dur <= 60 else 12)
    tag = f"{int(time.time()*1000)}"
    out = []
    for i in range(n):
        ts = dur * (i + 0.5) / n if dur > 0 else i * 2
        jpg = MEDIA_TMP / f"{tag}_{i}.jpg"
        _run([FF, "-hide_banner", "-loglevel", "error", "-y",
              "-ss", f"{ts:.2f}", "-i", src, "-frames:v", "1",
              "-vf", "scale=720:-1", str(jpg)], timeout=60)
        if jpg.exists() and jpg.stat().st_size > 1000:
            out.append(str(jpg))
    return out


def _asr(src: str) -> str:
    MEDIA_TMP.mkdir(parents=True, exist_ok=True)
    if not ASR_SCRIPT.exists():
        ASR_SCRIPT.write_text(ASR_CODE, encoding="utf-8")
    wav = MEDIA_TMP / f"{int(time.time()*1000)}.wav"
    _run([FF, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
          "-vn", "-ac", "1", "-ar", "16000", str(wav)], timeout=120)
    if not wav.exists():
        return ""
    env = dict(os.environ)
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    try:
        r = subprocess.run([ASR_PY, str(ASR_SCRIPT), str(wav)],
                           capture_output=True, timeout=300, env=env)
        return (r.stdout or b"").decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def _video_local(seg) -> str | None:
    import urllib.request as _ur
    from urllib.parse import unquote
    for cand in (getattr(seg, "path", None), getattr(seg, "file", None), getattr(seg, "url", None)):
        if not cand:
            continue
        c = str(cand)
        if c.startswith("file://"):
            p = unquote(c[7:])
            if os.path.exists(p):
                return p
        elif os.path.exists(c):
            return c
        elif c.startswith("http"):
            MEDIA_TMP.mkdir(parents=True, exist_ok=True)
            dst = MEDIA_TMP / f"v_{int(time.time()*1000)}.mp4"
            try:
                req = _ur.Request(c, headers={"User-Agent": "Mozilla/5.0"})
                with _ur.urlopen(req, timeout=120) as r, open(dst, "wb") as fh:
                    fh.write(r.read())
                if dst.exists() and dst.stat().st_size > 5000:
                    return str(dst)
            except Exception:
                continue
    return None


# ══════════════════════════════════════════
#  插件本体
# ══════════════════════════════════════════
class LocalEmbedding:
    """本地 embedding（bge-small-zh），没有云端 embedding 时用它。"""
    _model = None

    @classmethod
    def instance(cls):
        if cls._model is None:
            import os
            os.environ.setdefault("HF_HOME", r"F:\ai-cache\huggingface")
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            from sentence_transformers import SentenceTransformer
            cls._model = SentenceTransformer("BAAI/bge-small-zh-v1.5",
                                             cache_folder=r"F:\ai-cache\huggingface")
        return cls._model

    async def get_embedding(self, text: str):
        import asyncio
        m = await asyncio.to_thread(self.instance)
        vec = await asyncio.to_thread(lambda: m.encode([str(text)[:300]])[0])
        return [float(x) for x in vec]

    async def get_embeddings(self, texts):
        import asyncio
        m = await asyncio.to_thread(self.instance)
        arr = await asyncio.to_thread(lambda: m.encode([str(t)[:300] for t in texts], batch_size=16))
        return [[float(x) for x in row] for row in arr]


@register("yuyu_kernel", "阿修", "御坂内核：情绪 / 记忆 / 承诺 / 控制台", "1.0.0")
class YuyuInner(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.conf = config or {}
        self._apply_config()
        DATA.mkdir(parents=True, exist_ok=True)
        self._task = None
        self.state = _read_json(REACH_STATE, {"last_talk": 0, "last_offer": 0})
        import builtins
        builtins.__yuyu_plugin = self
        self._trim_tools()
        if self.conf.get("enable_panel", True):
            try:
                from .core import panel_server
                import asyncio as _a
                panel_server.start(self, _a.get_event_loop(),
                                   int(self.conf.get("panel_port", 6197) or 6197))
            except Exception as e:
                logger.warning(f"{LOG} 面板服务启动失败: {e}")
        try:
            from .core.webui import register_webui
            register_webui(self, context)
        except Exception as e:
            logger.warning(f"{LOG} 页面注册失败: {e}")
        _self_load()
        logger.info(f"{LOG} 已加载（唤醒/内在/作息/主动/媒体/自述）")
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._task = loop.create_task(self._reach_loop())
                self._promise_task = loop.create_task(self._promise_loop())
                self._emo_task = loop.create_task(self._emo_heartbeat())
        except Exception:
            pass

    def _apply_config(self):
        """把插件配置翻译成运行时参数（不给默认值就按单主人模式跑）。"""
        global OWNER_IDS, OWNER_UMO, OWNER_NAME, NAME_HINTS, FF, ASR_PY
        c = self.conf

        owner = str(c.get("owner_id", "") or "").strip()
        umo = str(c.get("owner_umo", "") or "").strip()
        if not umo and owner:
            umo = f"aiocqhttp:FriendMessage:{owner}"
        OWNER_IDS = {owner} if owner else set()
        OWNER_UMO = umo
        OWNER_NAME = str(c.get("owner_name", "") or "主人").strip() or "主人"

        names = [x.strip() for x in str(c.get("wake_names", "") or "").split(",") if x.strip()]
        NAME_HINTS = tuple(names) if names else ("御坂", "御板")

        FF = str(c.get("ffmpeg_path", "") or "ffmpeg")
        ASR_PY = str(c.get("asr_python", "") or "")

        try:
            from .core.paths import RUNTIME
            RUNTIME["owner_id"] = owner
            RUNTIME["owner_umo"] = OWNER_UMO
            RUNTIME["owner_name"] = OWNER_NAME
            RUNTIME["tts_proxy"] = str(c.get("tts_proxy", "") or "")
            RUNTIME["panel_actions"] = bool(c.get("enable_panel_actions", True))
        except Exception:
            pass

    # ── 工具裁剪 ───────────────────────────
    def _trim_tools(self):
        mgr = None
        try:
            mgr = self.context.provider_manager.llm_tools
        except Exception:
            try:
                mgr = self.context.get_llm_tool_manager()
            except Exception:
                mgr = None
        if mgr is None:
            return
        removed = 0
        for name in TRIM:
            try:
                if mgr.get_func(name):
                    mgr.remove_func(name)
                    removed += 1
            except Exception:
                pass
        if removed:
            logger.info(f"{LOG} 摘掉 {removed} 个群管理工具")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        self._trim_tools()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._reach_loop())

    # ══════════════════════════════════════
    #  ① 群唤醒
    # ══════════════════════════════════════
    @filter.custom_filter(WakeFilter)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def wake_for_group(self, event: AstrMessageEvent):
        return

    # ══════════════════════════════════════
    #  ② 内在状态：工具
    # ══════════════════════════════════════
    @filter.llm_tool(name="remember_note")
    async def remember_note(self, event: AstrMessageEvent, text: str, kind: str = "事情"):
        """记下一件值得记住的事（约定、承诺、喜好、心结）。

        Args:
            text(string): 要记的内容，写成一句完整的话
            kind(string): 类型，约定/事情/喜好/心结
        """
        DATA.mkdir(parents=True, exist_ok=True)
        row = {"ts": int(time.time()), "kind": kind, "text": text.strip(),
               "status": "open", "umo": getattr(event, "unified_msg_origin", "")}
        with open(NOTES, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        logger.info(f"{LOG} 记下[{kind}] {text[:40]}")
        return f"记下了：{text}"

    @filter.llm_tool(name="resolve_note")
    async def resolve_note(self, event: AstrMessageEvent, keyword: str):
        """把某件还搁着的事标记为已经了结。

        Args:
            keyword(string): 能匹配到那条事的关键词
        """
        rows = _read_jsonl(NOTES)
        hit = 0
        for r in rows:
            if r.get("status") == "open" and keyword and keyword in r.get("text", ""):
                r["status"] = "done"
                hit += 1
        if hit:
            _write_jsonl(NOTES, rows)
            return f"放下了 {hit} 件事"
        return "没找到还搁着的那件事"

    @filter.llm_tool(name="remember_scar")
    async def remember_scar(self, event: AstrMessageEvent, text: str):
        """记一道疤：只有真正改变了你的事才记。

        Args:
            text(string): 一句话写清这件事和它留下的影响
        """
        rows = _read_json(SCARS, [])
        rows.append({"ts": int(time.time()), "text": text.strip()})
        _write_json(SCARS, rows[-20:])
        logger.info(f"{LOG} 疤：{text[:40]}")
        return "这道疤记下来了"

    @filter.llm_tool(name="learn_habit")
    async def learn_habit(self, event: AstrMessageEvent, habit: str):
        """把反复出现的偏好或反应变成习惯（同一件事重复两三次后才用）。

        Args:
            habit(string): 一句话写清习惯
        """
        rows = _read_json(HABITS, [])
        key = habit.strip()
        for r in rows:
            if r.get("text") == key:
                r["n"] = int(r.get("n", 1)) + 1
                r["ts"] = int(time.time())
                break
        else:
            rows.append({"ts": int(time.time()), "text": key, "n": 1})
        _write_json(HABITS, rows[-30:])
        return f"记住了这个习惯：{key}"

    # ══════════════════════════════════════
    #  ③ 作息：工具
    # ══════════════════════════════════════
    @filter.llm_tool(name="go_sleep")
    async def go_sleep(self, event: AstrMessageEvent, wake_at: str = ""):
        """你说要睡了：进入睡眠，期间能回但很短。

        Args:
            wake_at(string): 想几点起，如 "07:30"；不填就明早七点
        """
        wake_ts = None
        if wake_at.strip():
            try:
                hh, mm = wake_at.strip().split(":")
                now = time.localtime()
                t = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                 int(hh), int(mm), 0, 0, 0, -1))
                wake_ts = t if t > time.time() else t + 86400
            except Exception:
                wake_ts = None
        if wake_ts is None:
            now = time.localtime()
            t = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 7, 0, 0, 0, 0, -1))
            wake_ts = t if t > time.time() else t + 86400
        _write_json(SLEEP_STATE, {"asleep": True, "since": time.time(), "wake_at": wake_ts})
        logger.info(f"{LOG} 她要睡了，{time.strftime('%H:%M', time.localtime(wake_ts))} 醒")
        return "好，睡吧"

    @filter.llm_tool(name="wake_up")
    async def wake_up(self, event: AstrMessageEvent):
        """你醒了。"""
        _write_json(SLEEP_STATE, {"asleep": False, "since": 0, "wake_at": None})
        return "醒了"

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def owner_wakes(self, event: AstrMessageEvent):
        try:
            uid = str(event.get_sender_id())
            if uid in OWNER_IDS and uid != str(event.get_self_id()):
                d = _read_json(SLEEP_STATE, {})
                if d.get("asleep"):
                    _write_json(SLEEP_STATE, {"asleep": False, "since": 0, "wake_at": None})
                    logger.info(f"{LOG} 主人说话，把她喊醒了")
                self.state["last_talk"] = time.time()
                _write_json(REACH_STATE, self.state)
        except Exception:
            pass

    # ══════════════════════════════════════
    #  ④ 主动：循环
    # ══════════════════════════════════════
    # ── 世界书 / 图谱 / 衰减 工具 ─────────────
    @filter.llm_tool(name="add_fact")
    async def tool_add_fact(self, event: AstrMessageEvent, fact: str, keys: str = ""):
        """往世界书里加一条事实卡：聊到这些词时它会自动想起来。

        Args:
            fact(string): 事实内容，一句话
            keys(string): 触发词，逗号分隔
        """
        f = str(fact or "").strip()
        if not f:
            return "内容为空"
        klist = [k.strip() for k in str(keys or "").split(",") if k.strip()]
        if not klist:
            klist = [f[:6]]
        book = _ex_load(WORLD_BOOK, [])
        book.append({"keys": klist[:6], "fact": f[:160], "on": True, "ts": int(time.time())})
        _ex_save(WORLD_BOOK, book[-200:])
        return f"记下了：聊到「{'、'.join(klist[:3])}」时会想起「{f[:30]}」"

    @filter.llm_tool(name="link_fact")
    async def tool_link(self, event: AstrMessageEvent, subject: str, relation: str, obj: str):
        """把两个东西连起来，存进关系网（轻量知识图谱）。

        Args:
            subject(string): 主体，比如 主人
            relation(string): 关系，比如 养了
            obj(string): 客体，比如 一只猫
        """
        g = _ex_load(GRAPH, {})
        sub = str(subject or "").strip()
        rel = str(relation or "").strip()
        ob = str(obj or "").strip()
        if not (sub and rel and ob):
            return "参数不全"
        g.setdefault(sub, {}).setdefault(rel, [])
        if ob not in g[sub][rel]:
            g[sub][rel].append(ob)
        g.setdefault(ob, {}).setdefault("…被" + rel, [])
        if sub not in g[ob]["…被" + rel]:
            g[ob]["…被" + rel].append(sub)
        _ex_save(GRAPH, g)
        return f"连上了：{sub} —{rel}→ {ob}"

    @filter.llm_tool(name="recall_links")
    async def tool_recall_links(self, event: AstrMessageEvent, name: str):
        """查一个东西的关系网（谁跟它有关）。

        Args:
            name(string): 实体名，比如 咪咪 / 主人
        """
        g = _ex_load(GRAPH, {})
        node = g.get(str(name or "").strip())
        if not node:
            return "关系网里没有这个"
        lines = [f"{rel}：{'、'.join(map(str, objs))}" for rel, objs in node.items() if objs]
        return "\n".join(f"· {x}" for x in lines[:8])

    async def _emo_heartbeat(self):
        """情绪慢心跳：一分钟查一次，内部 45 分钟才真正推一次。"""
        tick = 0
        while True:
            try:
                self._emo_tick()
                tick += 1
                if tick % 2 == 1:      # 每两分钟刷一次面板快照
                    try:
                        from .core.webui import write_snapshot
                        write_snapshot(self)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"{LOG} 心跳异常: {e}")
            await asyncio.sleep(60)

    # ── 情绪核心：听 / 主动 / 心跳 / 注入 ──────
    def _emo_hear(self, text: str):
        """她说的话进来时，先让她自己"感觉"一下。"""
        try:
            t = str(text or "")
            d = _emo_load()
            dims = d["dims"]
            now = time.time()
            harsh = any(w in t for w in EMO_HARSH)
            soft = any(w in t for w in EMO_SOFT)
            praise = any(w in t for w in EMO_PRAISE)

            if harsh:
                # 真假哄：刚被凶不到 3 分钟就来哄 → 效果减半
                fake = (now - float(d.get("last_harsh") or 0)) < 180
                d["last_harsh"] = now
                d["blame"] = min(100.0, float(d["blame"]) + 10)
                dims["stress"] = min(100, dims["stress"] + 8)
                dims["attach"] = max(0, dims["attach"] - 6)
                dims["joy"] = max(0, dims["joy"] - 10)
                d["fake_comfort"] = 1 if fake else 0
            if soft:
                fake = bool(d.get("fake_comfort"))
                d["last_soft"] = now
                cut = 0.5 if fake else 1.0
                d["blame"] = max(0.0, float(d["blame"]) - 12 * cut)
                dims["attach"] = min(100, dims["attach"] + 4 * cut)
                dims["intimacy"] = min(100, dims["intimacy"] + 5 * cut)
                dims["joy"] = min(100, dims["joy"] + 4 * cut)
            if praise:
                dims["joy"] = min(100, dims["joy"] + 4)
                d["thoughts"] = [x for x in d["thoughts"] if x.get("type") != "想被夸"]
            # 他说话了 → 期待清零，依恋回升
            # 责怪自然消退：按距离上次说话的小时数衰减
            last_hear = float(d.get("last_hear") or now)
            hours = max(0.0, (now - last_hear) / 3600.0)
            if hours > 0 and float(d["blame"]) > 0:
                d["blame"] = max(0.0, float(d["blame"]) - 1.5 * hours)
            d["last_hear"] = now

            d["expectation"] = 0.0
            # 小幅波动：一句话只推 1 点左右，靠哄/夸才明显涨
            dims["attach"] = min(100, dims["attach"] + (1 if len(t) >= 6 else 0))
            dims["social"] = min(100, dims["social"] + 1)
            d["state"] = _emo_state(d["blame"], dims["stress"])
            self._emo_satisfy(d, t)
            _ex_save(EMO_FILE, d)
        except Exception as e:
            logger.debug(f"{LOG} 情绪听失败: {e}")

    def _emo_satisfy(self, d, text: str):
        """念头被满足 → 清掉，并留下一点好心情。"""
        try:
            t = str(text or "")
            dims = d["dims"]
            ts = d.get("thoughts") or []
            hit = set()
            if any(w in t for w in ("厉害", "好看", "聪明", "可爱", "真棒", "做得好", "乖")):
                hit.add("想被夸")
            if any(w in t for w in ("抱", "摸摸", "亲", "贴贴", "靠")):
                hit.add("想被抱")
            if any(w in t for w in ("撒娇", "嘛", "哼哼", "宝宝")):
                hit.add("想撒娇")
            # 他回话了 → 等回话 / 想找他说话 都算被满足
            hit.update({"在等他回话", "想找他说话"})
            if not hit:
                return
            keep = []
            got = 0
            for x in ts:
                if str(x.get("type")) in hit:
                    got += 1
                else:
                    keep.append(x)
            if got:
                dims["joy"] = min(100, dims["joy"] + 5 * got)
                dims["intimacy"] = min(100, dims["intimacy"] + 3 * got)
                dims["attach"] = min(100, dims["attach"] + 2 * got)
                d["thoughts"] = keep
        except Exception as e:
            logger.debug(f"{LOG} 念头满足失败: {e}")

    def _emo_proactive(self):
        """她主动开口了 → 期待涨，开始惦记你回没回。"""
        try:
            d = _emo_load()
            d["expectation"] = min(5.0, float(d["expectation"]) + 1)
            d["dims"]["social"] = max(0, d["dims"]["social"] - 3)
            d["dims"]["attach"] = min(100, d["dims"]["attach"] + 2)
            if not any(x.get("type") == "在等他回话" for x in d["thoughts"]):
                d["thoughts"].append({"type": "在等他回话", "ts": int(time.time())})
            _ex_save(EMO_FILE, d)
        except Exception as e:
            logger.debug(f"{LOG} 情绪主动失败: {e}")

    def _emo_tick(self):
        """45 分钟慢心跳：数值慢慢回落，没回就掉依恋。"""
        try:
            d = _emo_load()
            now = time.time()
            last_hear = float(d.get("last_hear") or 0)
            active = (now - last_hear) < 1800
            tick = 15 * 60 if active else EMO_TICK
            if now - float(d.get("last_tick") or 0) < tick:
                return
            d["last_tick"] = now
            dims = d["dims"]
            exp = float(d.get("expectation") or 0)
            for k in EMO_DIMS:
                base = 50
                dims[k] = int(round(dims[k] + (base - dims[k]) * 0.18))
            if exp > 0:
                dims["attach"] = max(0, dims["attach"] - int(3 * exp))
                dims["joy"] = max(0, dims["joy"] - int(2 * exp))
                if exp >= 3 and not any(x.get("type") == "想找他说话" for x in d["thoughts"]):
                    d["thoughts"].append({"type": "想找他说话", "ts": int(now)})
            # 念头：按状态挂一个
            if len(d["thoughts"]) < 3 and random.random() < 0.5:
                if dims["social"] > 60:
                    pick = random.choice(["想找他说话", "想问他今天怎样", "想撒娇"])
                elif dims["stress"] > 55:
                    pick = "想安静"
                else:
                    pick = random.choice(["想被夸", "想被抱", "想找点事做"])
                if not any(x.get("type") == pick for x in d["thoughts"]):
                    d["thoughts"].append({"type": pick, "ts": int(now)})
            # 太旧的念头散掉（12 小时）
            d["thoughts"] = [x for x in d["thoughts"] if now - float(x.get("ts") or 0) < 12 * 3600]
            d["state"] = _emo_state(d["blame"], dims["stress"])
            _ex_save(EMO_FILE, d)
            # 记一条历史（最多 288 条，2 天左右）
            try:
                hist = _ex_load(EMO_HISTORY, [])
                hist.append({"ts": int(now), "state": d["state"],
                             "dims": {k: dims[k] for k in EMO_DIMS},
                             "exp": float(d.get("expectation") or 0)})
                _ex_save(EMO_HISTORY, hist[-288:])
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"{LOG} 情绪心跳失败: {e}")

    def _emo_inject(self) -> str:
        """只在状态变化或超过 20 分钟时注入一句（≤120 字）。"""
        try:
            d = _emo_load()
            now = time.time()
            state = str(d.get("state") or "calm")
            changed = state != str(d.get("last_inject_state") or "")
            stale = now - float(d.get("last_inject") or 0) > 20 * 60
            if not (changed or stale):
                return ""
            dims = d["dims"]
            try:
                mood = _mood_text()
            except Exception:
                mood = ""
            bits = []
            if state == "hurt":
                bits.append("你有点委屈，说话会带出来，但别反复提；这轮最多两句")
            elif state == "afraid":
                bits.append("你有点怕他生气，句子会短、会试探；这轮最多一句半")
            elif state == "closed":
                bits.append("你不太想说话了；这轮一两个字就够，别解释")
            if dims["joy"] >= 66:
                bits.append("心情不错")
            elif dims["joy"] <= 34:
                bits.append("提不起劲")
            if float(d.get("expectation") or 0) >= 2:
                bits.append("你刚主动找过他、还没回")
            ts = d.get("thoughts") or []
            if ts:
                bits.append("心里挂着：" + "、".join(str(x.get("type")) for x in ts[:2]))
            if mood:
                bits.append("心底：" + str(mood)[:40])
            if not bits:
                return ""
            d["last_inject"] = now
            d["last_inject_state"] = state
            _ex_save(EMO_FILE, d)
            return "【你这会儿的心情】\n" + "；".join(bits)[:120]
        except Exception as e:
            logger.debug(f"{LOG} 情绪注入失败: {e}")
            return ""

    @filter.llm_tool(name="check_mood")
    async def tool_check_mood(self, event: AstrMessageEvent):
        """看看自己现在什么心情、心里挂着什么。"""
        d = _emo_load()
        dims = d["dims"]
        top = sorted(dims.items(), key=lambda x: -x[1])[:3]
        bits = [f"{EMO_LABEL.get(k, k)}{v}" for k, v in top]
        ts = "、".join(str(x.get("type")) for x in (d.get("thoughts") or [])[:3])
        return f"现在 {d.get('state')}：{'、'.join(bits)}" + (f"；心里挂着：{ts}" if ts else "")

    # ── 向量检索（有 embedding 就用，没有就退关键词）──
    def _emb_provider(self):
        try:
            mgr = getattr(self.context, "provider_manager", None)
            insts = getattr(mgr, "embedding_provider_insts", None) or []
            if insts:
                return insts[0]
        except Exception:
            pass
        # 没配云端 embedding → 用本地模型（懒加载，第一次慢）
        try:
            return LocalEmbedding()
        except Exception as e:
            logger.debug(f"{LOG} 本地向量不可用: {e}")
            return None

    async def _vec_build(self) -> int:
        """把所有记忆做成向量，存进 search.db。没有 embedding 就跳过。"""
        import sqlite3
        prov = self._emb_provider()
        if prov is None:
            return 0
        try:
            rows = []
            for r in _read_jsonl(NOTES):
                rows.append((str(r.get("text") or ""), "note"))
            for r in _read_json(SCARS, []):
                rows.append((str(r.get("text") or ""), "scar"))
            for r in _read_json(HABITS, []):
                rows.append((str(r.get("text") or ""), "habit"))
            for name, kind in (("diaries.json", "diary"), ("dreams.json", "dream")):
                for d in _ex_load(SHARED_DIR / name, []) or []:
                    rows.append((str(d.get("text") or ""), kind))
            rows = [(t, k) for t, k in rows if t.strip()]
            if not rows:
                return 0
            vecs = await prov.get_embeddings([t[:400] for t, _ in rows])
            FTS_DB.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(FTS_DB)
            con.execute("CREATE TABLE IF NOT EXISTS vec(id INTEGER PRIMARY KEY, text TEXT, kind TEXT, v BLOB)")
            con.execute("DELETE FROM vec")
            import array
            for (t, k), v in zip(rows, vecs):
                con.execute("INSERT INTO vec(text, kind, v) VALUES (?,?,?)",
                            (t[:800], k, array.array("f", v).tobytes()))
            con.commit()
            con.close()
            return len(rows)
        except Exception as e:
            logger.debug(f"{LOG} 向量索引失败: {e}")
            return 0

    async def _vec_search(self, kw: str, limit: int = 8):
        """余弦相似度找最接近的几条。"""
        import array
        import math
        import sqlite3
        prov = self._emb_provider()
        if prov is None or not FTS_DB.exists():
            return []
        try:
            qv = await prov.get_embedding(str(kw)[:200])
            con = sqlite3.connect(FTS_DB)
            cur = con.execute("SELECT text, kind, v FROM vec")
            rows = cur.fetchall()
            con.close()
            scored = []
            for text, kind, blob in rows:
                arr = array.array("f")
                arr.frombytes(blob)
                dot = sum(a * b for a, b in zip(qv, arr))
                na = math.sqrt(sum(a * a for a in qv)) or 1
                nb = math.sqrt(sum(b * b for b in arr)) or 1
                scored.append((dot / (na * nb), text, kind))
            scored.sort(reverse=True)
            return [(t, k, sc) for sc, t, k in scored[:limit] if sc > 0.2]
        except Exception as e:
            logger.debug(f"{LOG} 向量检索失败: {e}")
            return []

     # ── 双过程内省（提示层，不额外调模型）──
    def _introspect(self) -> str:
        return ("回之前先在心里过一遍：我现在是什么感觉 → 所以我打算怎么回。\n"
                "这一步只影响语气和长短，别把过程写出来。")

    # ── 全文检索（FTS5）────────────────────
    def _fts_build(self):
        import sqlite3
        try:
            FTS_DB.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(FTS_DB)
            con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem USING fts5(text, kind, ts UNINDEXED)")
            con.execute("DELETE FROM mem")
            rows = []
            for r in _read_jsonl(NOTES):
                rows.append((str(r.get("text") or ""), "note", float(r.get("ts") or 0)))
            for r in _read_json(SCARS, []):
                rows.append((str(r.get("text") or ""), "scar", float(r.get("ts") or 0)))
            for r in _read_json(HABITS, []):
                rows.append((str(r.get("text") or ""), "habit", float(r.get("ts") or 0)))
            for name, kind in (("diaries.json", "diary"), ("dreams.json", "dream")):
                for d in _ex_load(SHARED_DIR / name, []) or []:
                    rows.append((str(d.get("text") or ""), kind, float(d.get("created_at") or 0)))
            for card in _ex_load(WORLD_BOOK, []) or []:
                rows.append((str(card.get("fact") or ""), "fact", float(card.get("ts") or 0)))
            con.executemany("INSERT INTO mem(text, kind, ts) VALUES (?,?,?)", rows)
            con.commit()
            con.close()
            return len(rows)
        except Exception as e:
            logger.debug(f"{LOG} 建索引失败: {e}")
            return 0

    def _fts_search(self, kw: str, limit: int = 8):
        import sqlite3
        try:
            con = sqlite3.connect(FTS_DB)
            cur = con.execute(
                "SELECT text, kind, ts FROM mem WHERE mem MATCH ? ORDER BY rank LIMIT ?",
                (f'"{kw}"', limit),
            )
            rows = cur.fetchall()
            con.close()
            return rows
        except Exception:
            return []

    # ── 身份归属 ─────────────────────────
    def _identity_note(self, uid: str, name: str, umo: str = ""):
        try:
            d = _ex_load(IDENTITY, {})
            key = str(uid or name)
            rec = d.get(key) or {}
            now = int(time.time())
            rec.update({"name": name or rec.get("name", ""), "last": now,
                        "umo": umo or rec.get("umo", "")})
            rec.setdefault("first", now)
            d[key] = rec
            _ex_save(IDENTITY, d)
        except Exception:
            pass

    @filter.llm_tool(name="who_is")
    async def tool_who_is(self, event: AstrMessageEvent, name: str = ""):
        """查某个人的档案和在哪些地方说过话（身份归属）。

        Args:
            name(string): 昵称或 QQ 号
        """
        key = str(name or "").strip()
        people = _ex_load(PEOPLE, {})
        ident = _ex_load(IDENTITY, {})
        if not key:
            try:
                key = str(event.get_sender_id() or "") or (event.get_sender_name() or "")
            except Exception:
                key = ""
        prof = people.get(key) or {}
        rec = ident.get(key) or {}
        if not prof and not rec:
            return f"没有「{key}」的记录"
        bits = [f"名字：{prof.get('name') or rec.get('name') or key}"]
        for k, label in (("call", "称呼"), ("tone", "语气"), ("avoid", "禁忌"),
                         ("relation", "关系"), ("note", "备注")):
            if prof.get(k):
                bits.append(f"{label}：{prof[k]}")
        if rec.get("first"):
            bits.append(f"首次见面：{time.strftime('%m-%d', time.localtime(rec['first']))}")
        return "；".join(bits)

    # ── 拟人化学习（默认进审查队列）───────────
    @filter.llm_tool(name="learn_style")
    async def tool_learn_style(self, event: AstrMessageEvent, note: str):
        """记下一条"该怎么说话"的观察，进审查队列，等你批了才用。

        Args:
            note(string): 一条风格观察，比如"他喜欢短句，别说太长"
        """
        t = str(note or "").strip()
        if not t:
            return "空的"
        q = _ex_load(STYLE_QUEUE, [])
        q.append({"text": t[:120], "status": "pending", "ts": int(time.time())})
        _ex_save(STYLE_QUEUE, q[-100:])
        return f"先记着：{t[:30]}（等你批准才生效，共 {sum(1 for x in q if x.get('status') == 'pending')} 条待批）"

    @filter.llm_tool(name="review_style")
    async def tool_review_style(self, event: AstrMessageEvent, index: int = -1, approve: bool = True):
        """批或否一条风格观察。

        Args:
            index(int): 第几条（0 开始，-1 = 最新一条）
            approve(bool): True 采纳，False 删掉
        """
        q = _ex_load(STYLE_QUEUE, [])
        pend = [i for i, x in enumerate(q) if x.get("status") == "pending"]
        if not pend:
            return "没有待批的"
        i = pend[-1] if index < 0 else (pend[index] if index < len(pend) else pend[-1])
        q[i]["status"] = "approved" if approve else "rejected"
        _ex_save(STYLE_QUEUE, q)
        return ("采纳：" if approve else "否掉：") + str(q[i].get("text"))[:40]

    # ── 运行监控（统计）────────────────────
    @filter.llm_tool(name="stats_panel")
    async def tool_stats(self, event: AstrMessageEvent):
        """看一眼她的数据规模：记忆、疤、习惯、承诺、档案、世界书、关系网、心情。"""
        notes = _read_jsonl(NOTES)
        emo = _emo_load()
        dims = emo.get("dims") or {}
        top = sorted(dims.items(), key=lambda x: -x[1])[:2]
        return ("；".join([
            f"记忆 {len(notes)} 条",
            f"疤 {len(_read_json(SCARS, []))} 条",
            f"习惯 {len(_read_json(HABITS, []))} 条",
            f"承诺 {sum(1 for r in _ex_load(PROMISES, []) if r.get('status') == 'open')} 件待办",
            f"人物档案 {len(_ex_load(PEOPLE, {}))} 个",
            f"世界书 {len(_ex_load(WORLD_BOOK, []))} 张卡",
            f"关系网 {len(_ex_load(GRAPH, {}))} 个实体",
            f"心情 {emo.get('state')}（{'、'.join(f'{EMO_LABEL.get(k,k)}{v}' for k,v in top)}）",
        ]))

    # ── 全局备份 ─────────────────────────
    @filter.llm_tool(name="backup_now")
    async def tool_backup(self, event: AstrMessageEvent):
        """把她的记忆、人格、承诺、档案、世界书打包成一个 zip 备份。"""
        import zipfile
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            name = BACKUP_DIR / f"yuyu-{time.strftime('%Y%m%d-%H%M%S')}.zip"
            srcs = [
                (DATA, "notes"),
                (EX_DIR, "extra"),
                (SHARED_DIR, "shared"),
            ]
            n = 0
            with zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED) as z:
                for root, prefix in srcs:
                    if not root.exists():
                        continue
                    for f in root.rglob("*"):
                        if f.is_file():
                            z.write(f, f"{prefix}/{f.relative_to(root)}")
                            n += 1
                # 人格单独存一份
                try:
                    per = _persona()
                    if per:
                        z.writestr("persona.txt", per)
                        n += 1
                except Exception:
                    pass
            size = round(name.stat().st_size / 1024, 1)
            old = sorted(BACKUP_DIR.glob("yuyu-*.zip"))
            for f in old[:-10]:
                f.unlink(missing_ok=True)
            return f"备份好了：{name.name}（{n} 个文件，{size} KB）"
        except Exception as e:
            return f"备份失败：{str(e)[:80]}"

    # ── 周记导出 ─────────────────────────
    @filter.llm_tool(name="weekly_digest")
    async def tool_digest(self, event: AstrMessageEvent, days: int = 7):
        """把最近几天的日记、梦境、记忆汇总成一篇 markdown 存到工作区。

        Args:
            days(int): 统计最近几天，默认 7
        """
        try:
            days = max(1, min(30, int(days or 7)))
            since = time.time() - days * 86400
            lines = [f"# 御坂周记 · {time.strftime('%Y-%m-%d')}（近 {days} 天）", ""]

            rows = [r for r in _read_jsonl(NOTES) if float(r.get("ts") or 0) >= since]
            if rows:
                lines.append("## 记下的事")
                lines += [f"- {r.get('text', '')}" for r in rows[-40:]]
                lines.append("")

            for f, title in ((SHARED_DIR / "diaries.json", "## 日记"),
                             (SHARED_DIR / "dreams.json", "## 梦境")):
                data = _ex_load(f, [])
                picked = [d for d in data if float(d.get("created_at") or 0) >= since]
                if picked:
                    lines.append(title)
                    lines += [f"- {str(d.get('text', ''))[:160]}" for d in picked[-10:]]
                    lines.append("")

            mp = [r for r in _read_json(SCARS, [])] + [r for r in _read_json(HABITS, [])]
            if mp:
                lines.append("## 疤与习惯")
                lines += [f"- {r.get('text', '')}" for r in mp[-10:]]

            DIGEST_DIR.mkdir(parents=True, exist_ok=True)
            out = DIGEST_DIR / f"{time.strftime('%Y-%m-%d')}-{days}d.md"
            out.write_text("\n".join(lines), encoding="utf-8")
            return f"写好了：{out.name}（{len(lines)} 行）"
        except Exception as e:
            return f"周记失败：{str(e)[:80]}"

    # ── 人格演化（观察 + 只注入状态）──────────
    def _evo_update(self, text: str):
        try:
            st = _ex_load(EVO_STATE, {"stat": {}, "state": "", "updated": 0})
            stat = st.get("stat") or {}
            n = int(stat.get("n", 0)) + 1
            ln = len(str(text or ""))
            avg = (float(stat.get("avg_len", 0)) * (n - 1) + ln) / max(1, n)
            soft = sum(1 for w in ("唔", "诶", "嘛", "呀", "哼") if w in str(text or ""))
            stat.update({"n": n, "avg_len": round(avg, 1),
                         "soft_rate": round((float(stat.get("soft_rate", 0)) * (n - 1) + soft) / max(1, n), 2),
                         "last": int(time.time())})
            st["stat"] = stat
            # 每 30 条更新一次"最近状态"
            if n % 30 == 0:
                bits = []
                if stat["avg_len"] > 45:
                    bits.append("话比以前多一点")
                elif stat["avg_len"] < 14:
                    bits.append("话变短了")
                if stat["soft_rate"] > 1.5:
                    bits.append("语气软了些")
                st["state"] = "；".join(bits)
                st["updated"] = int(time.time())
            _ex_save(EVO_STATE, st)
        except Exception as e:
            logger.debug(f"{LOG} 演化统计失败: {e}")

    # ── 自动审核 ─────────────────────────
    def _audit(self, text: str) -> str:
        t = str(text or "")
        for w in AUDIT_WORDS:
            if w in t:
                logger.warning(f"{LOG} 审核拦下：{t[:40]}")
                return "唔……这个御坂不说。"
        return t

    @filter.on_llm_response()
    async def audit_response(self, event: AstrMessageEvent, resp=None):
        try:
            if resp is None or not hasattr(resp, "completion_text"):
                return
            self._evo_update(resp.completion_text or "")
            fixed = self._audit(resp.completion_text or "")
            if fixed != (resp.completion_text or ""):
                resp.completion_text = fixed
        except Exception as e:
            logger.debug(f"{LOG} 审核失败: {e}")

    # ── 三级记忆：全库检索（含归档）───────────
    @filter.llm_tool(name="search_memory")
    async def tool_search_memory(self, event: AstrMessageEvent, keyword: str):
        """在所有记忆里搜（包括很久以前的），想不起来某件事时用。

        Args:
            keyword(string): 关键词
        """
        kw = str(keyword or "").strip()
        if not kw:
            return "要给个关键词"
        # 向量优先（有 embedding 才有结果），没有就退 FTS
        try:
            vres = await self._vec_search(kw, 6)
            if not vres:
                # 索引可能是空的，建一次再试
                n = await self._vec_build()
                if n:
                    vres = await self._vec_search(kw, 6)
            if vres:
                return "\n".join(f"· [{k}] {str(t)[:70]}（相似 {sc:.2f}）" for t, k, sc in vres[:8])
        except Exception as e:
            logger.debug(f"{LOG} 向量检索跳过: {e}")

        # 再走 FTS，没有索引就建一次
        try:
            if not FTS_DB.exists():
                self._fts_build()
            fts = self._fts_search(kw, 8)
            if fts:
                return "\n".join(f"· [{k}] {str(t)[:70]}" for t, k, _ in fts[:8])
        except Exception:
            pass
        rows = _read_jsonl(NOTES)
        hit = [r for r in rows if kw in str(r.get("text", ""))]
        extra = []
        for f in (SCARS, HABITS):
            for r in _read_json(f, []):
                if kw in str(r.get("text", "")):
                    extra.append(r)
        allrows = hit + extra
        if not allrows:
            return f"记忆里没有「{kw}」"
        allrows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
        return "\n".join(f"· {str(r.get('text'))[:70]}" for r in allrows[:8])

    # ── 共情（轻量）：只提醒别急着讲道理 ─────
    def _empathy_hint(self, text: str) -> str:
        t = str(text or "")
        neg = ("难受", "累", "烦", "生气", "难过", "崩", "哭", "焦虑", "压力", "不想活")
        pos = ("开心", "高兴", "爽", "成了", "过了", "赢了", "太好了")
        if any(w in t for w in neg):
            return "他这会儿情绪不太好，先顺着他说，别讲道理、别劝。"
        if any(w in t for w in pos):
            return "他心情不错，可以跟着高兴一下。"
        return ""

    # ── 约定回访 ─────────────────────────
    async def _promise_loop(self):
        """每分钟看一眼：有没有到点该说的约定。"""
        while True:
            try:
                rows = _ex_load(PROMISES, [])
                now = time.time()
                due = [r for r in rows if r.get("status") == "open" and float(r.get("due") or 0) <= now]
                if due:
                    for r in due:
                        text = str(r.get("text") or "").strip()
                        if not text:
                            r["status"] = "done"
                            continue
                        try:
                            from astrbot.core.message.message_event_result import MessageChain
                            from astrbot.api.message_components import Plain
                            await self.context.send_message(
                                str(r.get("umo") or OWNER_UMO),
                                MessageChain([Plain(text=text)]),
                            )
                            r["status"] = "done"
                            r["fired_at"] = int(now)
                            logger.info(f"{LOG} 已投递约定：{text[:30]}")
                        except Exception as e:
                            logger.warning(f"{LOG} 投递约定失败: {e}")
                    _ex_save(PROMISES, rows)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"{LOG} 约定循环出错: {e}")
            await asyncio.sleep(60)

    @filter.llm_tool(name="remember_promise")
    async def tool_promise(self, event: AstrMessageEvent, text: str, minutes: int = 60):
        """答应别人的事记下来，到点系统会替你发出去。

        Args:
            text(string): 到点要说的话
            minutes(int): 多少分钟后触发
        """
        minutes = max(1, min(60 * 24 * 30, int(minutes or 60)))
        rows = _ex_load(PROMISES, [])
        try:
            who = event.get_sender_name() or "主人"
            umo = event.unified_msg_origin or OWNER_UMO
        except Exception:
            who, umo = "主人", OWNER_UMO
        rows.append({
            "id": uuid.uuid4().hex[:8],
            "text": str(text)[:200],
            "to": who,
            "umo": umo,
            "due": time.time() + minutes * 60,
            "status": "open",
            "created": int(time.time()),
        })
        _ex_save(PROMISES, rows[-100:])
        return f"记住了：{minutes} 分钟后跟{who}说「{str(text)[:30]}」"

    @filter.llm_tool(name="list_promises")
    async def tool_promises(self, event: AstrMessageEvent):
        """看看还有哪些答应过、没到点的事。"""
        rows = [r for r in _ex_load(PROMISES, []) if r.get("status") == "open"]
        if not rows:
            return "没有待办的约定"
        rows.sort(key=lambda r: float(r.get("due") or 0))
        return "\n".join(f"· {_fmt_when(r['due'])} → {r.get('to')}：{str(r.get('text'))[:40]}"
                          for r in rows[:10])

    @filter.llm_tool(name="set_person_note")
    async def tool_person(self, event: AstrMessageEvent, name: str = "", call: str = "",
                          tone: str = "", avoid: str = "", relation: str = "", note: str = ""):
        """给某个人记一份档案：怎么称呼、什么语气、别聊什么。

        Args:
            name(string): 对方昵称
            call(string): 怎么称呼他
            tone(string): 对他说话的语气
            avoid(string): 别聊的话题
            relation(string): 你们的关系
            note(string): 关于他的事
        """
        try:
            who = str(name or "").strip() or (event.get_sender_name() or "")
            uid = str(event.get_sender_id() or "")
        except Exception:
            who, uid = str(name or ""), ""
        if not who:
            return "不知道是谁"
        people = _ex_load(PEOPLE, {})
        prof = people.get(uid) or people.get(who) or {}
        for k, v in (("call", call), ("tone", tone), ("avoid", avoid),
                     ("relation", relation), ("note", note)):
            if str(v or "").strip():
                prof[k] = str(v).strip()[:120]
        prof["name"] = who
        prof["updated"] = int(time.time())
        people[uid or who] = prof
        _ex_save(PEOPLE, people)
        got = "、".join(f"{k}={str(prof.get(k))[:16]}" for k in ("call", "tone", "avoid", "relation", "note") if prof.get(k))
        return f"记下了{who}：{got}"

    async def _reach_loop(self):
        """主动循环：自己会复活，别一声不响地停掉。"""
        first = True
        while True:
            try:
                if first:
                    wait_min = 3
                elif self.state.get("next_minutes"):
                    wait_min = int(self.state["next_minutes"])
                else:
                    wait_min = random.randint(*OPPORTUNITY_EVERY)
                first = False
                wait_min = max(1, min(720, int(wait_min)))
                logger.info(f"{LOG} 下次机会 {wait_min} 分钟后")
                await asyncio.sleep(wait_min * 60)
                await self._offer()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"{LOG} 主动循环出错，60 秒后继续: {e}")
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    raise

    def _ensure_reach_task(self):
        """循环挂了就重新拉起来（每个请求前检查一次）。"""
        try:
            task = getattr(self, "_task", None)
            if task is None or task.done():
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    self._task = loop.create_task(self._reach_loop())
                    logger.info(f"{LOG} 主动循环已重启")
        except Exception:
            pass

    async def _offer(self):
        now = time.time()
        if ALIVE is not None:
            try:
                st = ALIVE.current()
                if ALIVE.asleep(st) or ALIVE.busy(st):
                    logger.info(f"{LOG} 她在{st.get('mode')}，这次不给机会")
                    return
            except Exception:
                pass
        hour = time.localtime(now).tm_hour
        if not (ACTIVE_HOURS[0] <= hour < ACTIVE_HOURS[1]):
            return
        if now - self.state.get("last_talk", 0) < MIN_GAP_SECONDS:
            return

        # 每日上限：别一分钟一句刷屏
        # 从文件读最新状态：插件重载后旧任务还在跑，别用内存里的旧计数
        disk = _read_json(REACH_STATE, {})
        if disk:
            self.state = disk
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.state.get("day") != day:
            self.state["day"] = day
            self.state["today_count"] = 0
        if int(self.state.get("today_count", 0)) >= REACH_DAILY_MAX:
            return

        provider = self.context.get_using_provider(umo=OWNER_UMO)
        if provider is None:
            return

        recent = _recent_chat()
        prompt = (
            "现在是没人说话的时刻。你想主动找主人说点什么吗？\n"
            "· 想说：只输出 [[SAY]] 加上你要说的话（一两句，像平时那样）\n"
            "· 不想说：输出 [[SKIP]] 然后立刻用一句话写下真实原因（不许编）\n"
            "想不想、什么时候再来问你，都由你定：最后可以加 [[NEXT:分钟]]。\n"
            "间隔自己掂量：刚聊完就隔久点（60-120），主人消失很久了可以短点（30-40）。\n"
            "例：[[SKIP]]现在懒得说话[[NEXT:240]]\n"
            "不要解释，不要加别的字。"
            + (f"\n\n[你们最近的聊天]\n{recent}" if recent else "")
        )
        try:
            resp = await provider.text_chat(prompt=prompt, context=[],
                                            system_prompt=_persona())
            text = (resp.completion_text or "").strip()
        except Exception as e:
            logger.warning(f"{LOG} 询问失败: {e}")
            return

        m = re.search(r"\[\[NEXT:(\d+)\]\]", text)
        if m:
            self.state["next_minutes"] = max(30, min(720, int(m.group(1))))   # 最少 30 分钟一次
            text = re.sub(r"\[\[NEXT:\d+\]\]", "", text).strip()

        self.state["last_offer"] = now
        _write_json(REACH_STATE, self.state)

        if "[[SAY]]" not in text:
            reason = text.replace("[[SKIP]]", "").strip()
            if reason:
                _remember_reason(reason)
            return

        msg = text.split("[[SAY]]", 1)[1].strip()
        msg = re.sub(r"\[\[(?:VOICE|SKIP|SAY|E:[a-zA-Z_]+|NEXT:\d+)\]\]", "", msg).strip()
        if not msg:
            return
        try:
            await self.context.send_message(OWNER_UMO, MessageChain([Plain(text=msg)]))
            self.state["today_count"] = int(self.state.get("today_count", 0)) + 1
            _write_json(REACH_STATE, self.state)
            logger.info(f"{LOG} 她主动说（今天第 {self.state['today_count']} 次）：{msg[:40]}")
            self._emo_proactive()
        except Exception as e:
            logger.error(f"{LOG} 发送失败: {e}")

    # ══════════════════════════════════════
    #  ⑤ 注入：心境/记事/疤/习惯 + 媒体
    # ══════════════════════════════════════
    @filter.on_llm_request()
    async def see_media(self, event: AstrMessageEvent, req=None):
        if req is None:
            return
        try:
            notes, extra = [], []
            for seg in event.get_messages():
                if isinstance(seg, Video):
                    path = await asyncio.to_thread(_video_local, seg)
                    if not path:
                        notes.append("（他发了一段视频，但文件没取到）")
                        continue
                    frames = await asyncio.to_thread(_frames, path, 0)
                    speech = await asyncio.to_thread(_asr, path)
                    extra.extend(frames)
                    desc = f"（他发了一段视频，你看到其中 {len(frames)} 个画面）"
                    if speech:
                        desc += f"里面的声音大意是：{speech[:300]}"
                    notes.append(desc)
                elif isinstance(seg, Image):
                    try:
                        p = await asyncio.to_thread(seg.convert_to_file_path)
                    except Exception:
                        p = None
                    if p and str(getattr(seg, "file", "")).lower().endswith(".gif"):
                        frames = await asyncio.to_thread(_frames, p, 0)
                        extra.extend(frames)
                        notes.append("（他发了一张动图，你看到其中几帧，注意差别）")
            if extra:
                req.image_urls = list(req.image_urls or []) + extra
            if notes:
                req.system_prompt = (req.system_prompt or "") + \
                    "\n\n<system_reminder>\n【你刚看到的】\n" + "\n".join(notes) + "\n</system_reminder>"
        except Exception as e:
            logger.warning(f"{LOG} 处理媒体失败: {e}")

    # ══════════════════════════════════════
    #  ⑥ 跨会话自述（原 self_context）
    # ══════════════════════════════════════
    @filter.on_llm_response()
    async def remember_self(self, event: AstrMessageEvent, resp=None):
        try:
            text = (getattr(resp, "completion_text", "") or "").strip()
            text = text.replace("[[VOICE]]", "").strip()
            if len(text) < 2:
                return
            say = _self_strip((event.message_str or "").strip().replace("\n", " "))[:120]
            umo = event.unified_msg_origin
            _self_buf.append((time.time(), umo, say, text[:160]))
            _self_save()
            logger.info(f"{LOG} 记下一条（{_self_where(umo)}）：{say[:16]} → {text[:16]}")
        except Exception as e:
            logger.error(f"{LOG} 自述记录失败: {e}")

    @filter.on_llm_request()
    async def recall_self(self, event: AstrMessageEvent, req=None):
        if req is None:
            return
        # 戳一戳：只有动作没有文字
        try:
            types = [type(m).__name__ for m in (event.get_messages() or [])]
            if "Poke" in types:
                req.system_prompt = (req.system_prompt or "") + (
                    "\n\n<system_reminder>\n【刚才发生的事】\n"
                    "主人戳了你一下——这是动作，不是说话，他没有发任何文字。\n"
                    "要么嫌弃一句，要么不理，别自己编一个话题出来。\n"
                    "</system_reminder>"
                )
                return
        except Exception:
            pass
        try:
            now = time.time()
            here = event.unified_msg_origin
            lines = [(t, u, s, x) for (t, u, s, x) in _self_buf
                     if now - t < SELF_TTL and u != here]
            if not lines:
                return
            picked = lines[-SELF_INJECT_N:]
            body = "\n".join(f"- {_self_where(u)}：他说「{s}」，你回「{x}」"
                             for _, u, s, x in picked if s) or \
                   "\n".join(f"- {_self_where(u)}：你说「{x}」" for _, u, s, x in picked)
            req.system_prompt = (req.system_prompt or "") + (
                "\n\n<system_reminder>\n【你刚才在别的地方聊过这些】\n"
                + body
                + "\n这边提起时，你记得聊过；别当成没发生，也别整句重复。\n"
                "</system_reminder>"
            )
            logger.info(f"{LOG} 带上 {len(picked)} 条别处的对话")
        except Exception as e:
            logger.error(f"{LOG} 自述注入失败: {e}")

    @filter.on_llm_request()
    async def inject_inner(self, event: AstrMessageEvent, req=None):
        self._ensure_reach_task()
        if req is None or not hasattr(req, "system_prompt"):
            return
        blocks = []
        # 先让她感觉这条消息（凶/哄/夸）
        try:
            self._emo_hear(str(getattr(event, "message_str", "") or ""))
            try:
                self._identity_note(str(event.get_sender_id() or ""),
                                    event.get_sender_name() or "",
                                    event.unified_msg_origin or "")
            except Exception:
                pass
            blocks.append("【回之前】\n" + self._introspect())
            emo_block = self._emo_inject()
            if emo_block:
                blocks.append(emo_block)
        except Exception as e:
            logger.debug(f"{LOG} 情绪接线失败: {e}")

        mood = _mood_text()
        if mood:
            blocks.append("【你现在的心境】\n" + mood)

        rows = _read_jsonl(NOTES)
        open_rows = [r for r in rows if r.get("status") == "open"][-MAX_OPEN:]
        if open_rows:
            lines = []
            for r in open_rows:
                ago = (time.time() - r.get("ts", 0)) / 3600
                when = f"{ago:.0f}小时前" if ago >= 1 else f"{ago*60:.0f}分钟前"
                lines.append(f"· {when}[{r.get('kind','')}] {r.get('text','')}")
            blocks.append("【你心里还搁着的】\n" + "\n".join(lines)
                          + "\n（合适的时候可以自己提一句；别硬塞）")

        recent_all = [r for r in rows if r.get("status") != "open"]
        fresh_rows, old_rows = _decay_pick(recent_all, 30, MAX_NOTES, 3)
        if fresh_rows:
            blocks.append("【你记着的事】\n" + "\n".join(f"· {r.get('text','')}" for r in fresh_rows))
        if old_rows:
            blocks.append("【有点模糊的旧事】\n" + "\n".join(f"· {r.get('text','')}" for r in old_rows)
                          + "\n（记不太清了，说出来时别太确定）")

        scars = _read_json(SCARS, [])[-MAX_SCARS:]
        if scars:
            blocks.append("【疤】\n" + "\n".join(f"· {s.get('text','')}" for s in scars)
                          + "\n（这些改变过你，说话时会带着，但不用主动讲）")

        habits = _read_json(HABITS, [])[-MAX_HABITS:]
        if habits:
            blocks.append("【你养成的习惯】\n" + "\n".join(f"· {h.get('text','')}" for h in habits)
                          + "\n（这些是慢慢长出来的，照做就行，不用解释）")

        skin = _read_json(SLEEP_STATE, {})
        if skin.get("asleep"):
            blocks.append("【你在睡】\n迷迷糊糊的，回复很短，别问东问西。")

        if ALIVE is not None:
            try:
                st = ALIVE.current()
                if st.get("mode") in ("busy", "sleepy"):
                    blocks.append(f"【此刻】\n{st.get('say','')}")
            except Exception:
                pass

        # 已批准的风格观察（最多 4 条）
        try:
            q = [x for x in _ex_load(STYLE_QUEUE, []) if x.get("status") == "approved"]
            if q:
                blocks.append("【你学到的说话方式】\n" + "\n".join(f"· {str(x.get('text'))[:60]}" for x in q[-4:]))
        except Exception:
            pass

        # 最近状态（演化的观察结果，只当状态用）
        try:
            evo = _ex_load(EVO_STATE, {})
            if evo.get("state"):
                blocks.append("【你最近的状态】\n" + str(evo["state"]))
        except Exception:
            pass

        # 共情提示
        try:
            eh = self._empathy_hint(str(getattr(event, "message_str", "") or ""))
            if eh:
                blocks.append("【读到的情绪】\n" + eh)
        except Exception:
            pass

        # 世界书：命中触发词就注入事实卡
        try:
            text_now = ""
            try:
                text_now = str(event.message_str or "")
            except Exception:
                pass
            book = _ex_load(WORLD_BOOK, [])
            hits = []
            for card in book:
                if not card.get("on", True):
                    continue
                keys = card.get("keys") or []
                if any(str(k) and str(k) in text_now for k in keys):
                    hits.append(str(card.get("fact") or "")[:120])
            if hits:
                blocks.append("【相关的事实】\n" + "\n".join(f"· {h}" for h in hits[:3]))
        except Exception as e:
            logger.debug(f"{LOG} 世界书失败: {e}")

        # 约定回访 + 群友档案
        try:
            opens = [r for r in _ex_load(PROMISES, []) if r.get("status") == "open"]
            opens.sort(key=lambda r: float(r.get("due") or 0))
            if opens:
                blocks.append("【你答应过的事（到点系统会替你说，不用自己记）】\n" + "\n".join(
                    f"· {_fmt_when(r.get('due'))} 要跟{r.get('to') or '主人'}说：{str(r.get('text'))[:60]}"
                    for r in opens[:3]))
            who = ""
            uid = ""
            try:
                who = event.get_sender_name() or ""
                uid = event.get_sender_id() or ""
            except Exception:
                pass
            people = _ex_load(PEOPLE, {})
            prof = people.get(str(uid)) or people.get(who) or {}
            if prof:
                bits = []
                for k, label in (("call", "称呼他"), ("tone", "对他用什么语气"),
                                 ("avoid", "别聊的话题"), ("relation", "你们的关系"),
                                 ("note", "你记得关于他的事")):
                    v = str(prof.get(k) or "").strip()
                    if v:
                        bits.append(f"{label}：{v}")
                if bits:
                    blocks.append(f"【眼前这个人：{prof.get('name') or who}】\n" + "\n".join(bits))
        except Exception as e:
            logger.debug(f"{LOG} 档案注入失败: {e}")

        if not blocks:
            return
        try:
            req.system_prompt = (req.system_prompt or "") + \
                "\n\n<system_reminder>\n" + "\n\n".join(blocks) + "\n</system_reminder>"
        except Exception as e:
            logger.warning(f"{LOG} 注入失败: {e}")


def _persona() -> str:
    import sqlite3
    try:
        from .core.paths import DATA_ROOT
        con = sqlite3.connect(str(DATA_ROOT / "data_v4.db"))
        row = con.execute("select system_prompt from personas where persona_id='default' limit 1").fetchone()
        con.close()
        return (row[0] if row else "") or ""
    except Exception:
        return ""


def _recent_chat(limit: int = 8) -> str:
    import sqlite3
    try:
        from .core.paths import DATA_ROOT
        con = sqlite3.connect(str(DATA_ROOT / "data_v4.db"))
        row = con.execute(
            "select content from conversations where user_id=? order by updated_at desc limit 1",
            (OWNER_UMO,),
        ).fetchone()
        con.close()
        if not row or not row[0]:
            return ""
        out = []
        for m in json.loads(row[0])[-limit:]:
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
            c = str(c or "").strip()
            c = re.sub(r"<system_reminder>.*?</system_reminder>", "", c, flags=re.S)
            c = re.sub(r"\[\[(?:VOICE|E:[a-zA-Z_]+|SAY|SKIP|NEXT:\d+)\]\]", "", c)
            c = c.replace("\n", " ").strip()
            if c:
                out.append(("主人：" if m.get("role") == "user" else "御坂：") + c[:120])
        return "\n".join(out)
    except Exception:
        return ""


def _remember_reason(reason: str):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        row = {"ts": int(time.time()), "kind": "刚才", "status": "done",
               "text": f"刚才没主动找主人，因为：{reason.strip()}"}
        with open(NOTES, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
