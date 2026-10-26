"""极简双语机制:t("中文", "English") 内联包装。

语言解析:XBM_LANG 环境变量(zh|en|auto)→ Windows/UI 系统语言。
默认跟随系统:中文系统输出中文,其余输出英文。
"""

import os
import sys

_LANG = None


def detect_lang() -> str:
    global _LANG
    if _LANG:
        return _LANG
    env = (os.environ.get("XBM_LANG") or "auto").strip().lower()
    if env in ("zh", "en"):
        _LANG = env
        return env
    # 跟随系统:常见中文 locale(含香港/台湾/新加坡)输出中文,其余英文
    import locale
    try:
        pref = (locale.getdefaultlocale()[0] or "").lower()
    except Exception:  # noqa: BLE001 - 某些环境 locale 不可用
        pref = ""
    for probe in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = (os.environ.get(probe) or "").lower()
        if v:
            pref = pref or v
            break
    _LANG = "zh" if any(k in pref for k in
                        ("zh_cn", "zh_tw", "zh_hk", "zh_sg", "chinese", "chs", "cht")) else "en"
    if not pref and sys.platform == "win32":
        try:
            import ctypes
            windll = ctypes.windll.kernel32
            _LANG = "zh" if windll.GetUserDefaultUILanguage() in (
                0x0804, 0x0404, 0x0C04, 0x1004, 0x1404) else "en"
        except Exception:  # noqa: BLE001
            pass
    return _LANG


def set_lang(lang: str):
    global _LANG
    _LANG = lang


def t(zh: str, en: str) -> str:
    return zh if detect_lang() == "zh" else en
