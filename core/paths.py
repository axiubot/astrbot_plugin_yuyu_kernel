# -*- coding: utf-8 -*-
"""路径解析：全部跟着 AstrBot 的数据目录走，不写死任何本机路径。"""
from pathlib import Path


def _data_root() -> Path:
    """AstrBot 的 data 目录；取不到就退回当前文件下的 data/。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        p = Path(get_astrbot_data_path())
        if p.exists():
            return p
    except Exception:
        pass
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_path
        p = Path(get_astrbot_path()) / "data"
        if p.exists():
            return p
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent / "data"


DATA_ROOT = _data_root()
PLUGIN_DATA = DATA_ROOT / "plugin_data"

# 本插件自己的数据
OWN_DIR = PLUGIN_DATA / "astrbot_plugin_yuyu_kernel"
NOTES = OWN_DIR / "notes"                      # 笔记 / 疤 / 习惯
EX = OWN_DIR / "extra"                         # 情绪 / 承诺 / 档案 / 世界书 / 关系网
BACKUP_DIR = OWN_DIR / "backups"
DIGEST_DIR = OWN_DIR / "digests"

# 和 shared_memory 插件共用（没装就自动跳过）
SHARED = PLUGIN_DATA / "astrbot_plugin_shared_memory"

for _d in (NOTES, EX, BACKUP_DIR, DIGEST_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

# 运行时配置（由 main.py 在启动时填入，面板与接口共用）
RUNTIME = {
    "owner_id": "",
    "owner_umo": "",
    "owner_name": "主人",
    "tts_proxy": "",
    "panel_actions": True,
}
