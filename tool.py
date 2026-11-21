# -*- coding: utf-8 -*-
"""
阅见阅读器 —— 本地网页版，零第三方依赖（edge-tts 朗读为可选，装了才启用）

做什么：
1. 选一个文件夹 -> 扫描出里面的读物（txt / md / epub / pdf）
2. 自动认章节、生成左侧目录，网页里点书名就能读
3. 朗读用微软晓晓（zh-CN-XiaoxiaoNeural），先装 edge-tts 才可用
4. 图片 / 扫描版 PDF 没有文字层 -> 不做目录、不朗读，只标记出来

安全：只读扫描；朗读只在缓存目录写 mp3；不动任何原始读物文件。
"""
import os
import sys
import re
import io
import json
import html
import hashlib
import zipfile
import subprocess
import webbrowser
import urllib.request
import socket
import shutil
import time
import threading
import queue
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(BASE, "index.html")
CACHE = os.path.join(BASE, "音频缓存")
STATE = os.path.join(BASE, "state.json")
ERRLOG = os.path.join(BASE, "error.log")
os.makedirs(CACHE, exist_ok=True)


def _build_favicon():
    """纯标准库画一枚 32x32 图标：绿色圆底＋白色翻开的书，封成 ICO（内嵌 PNG）。
    页面带上它，--app 窗口和任务栏图标就是自己的脸，不再借 Edge 默认地球。"""
    import struct, zlib

    W = H = 32
    rows = []
    for y in range(H):
        row = bytearray([0])  # 每行 PNG 过滤字节
        for x in range(W):
            r2 = (x - 15.5) ** 2 + (y - 15.5) ** 2
            if r2 > 15.5 ** 2:            # 圆外透明
                c = (0, 0, 0, 0)
            else:
                c = (46, 125, 84, 255)    # 品牌绿圆底
                if 7 <= y <= 24:
                    # 左右两页（各带一点圆角感：靠边行缩进）
                    inset = 1 if y in (7, 24) else 0
                    if 8 + inset <= x <= 14 and 9 <= y <= 23:   # 左页
                        c = (255, 255, 255, 255)
                    elif 17 + inset <= x <= 23 and 9 <= y <= 23:  # 右页
                        c = (255, 255, 255, 255)
                    elif 15 <= x <= 16 and 8 <= y <= 24:        # 中缝（绿色书脊）
                        c = (46, 125, 84, 255)
            row += bytes(c)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))

    ico = struct.pack("<HHH", 0, 1, 1)                      # ICONDIR：1 个图标
    ico += struct.pack("<BBBBHHII", W, H, 0, 0, 1, 32, len(png), 22)  # 目录项
    ico += png
    return ico


FAVICON = _build_favicon()


def log_err(tag, ex):
    """无黑窗口模式下，把启动/运行错误写进 error.log，方便截图排查。"""
    try:
        with open(ERRLOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s: %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                       tag, repr(ex)))
    except Exception:
        pass

# 记录最近一次扫描的根目录，用于 PDF 阅读接口的路径白名单
SCAN_ROOT = [None]
APP_MODE = [False]  # 是否用 Edge --app 极简窗口模式打开
srv_store = [None]  # 当前 HTTP 服务实例，供 /api/quit 关闭
EDGE_PROC = [None]  # 本次启动的 Edge --app 进程，退出时一并关掉窗口
HEARTBEAT = [0.0]   # 页面最后心跳时间戳（时间.time()），0 表示还没收到过

EDGE_PROFILE = os.path.join(BASE, "_edge_profile")

# ---- 系统原生"选择文件夹/图片"对话框 ----
# tkinter 非线程安全：专门开一个常驻线程，用队列串行处理弹窗请求，一次只弹一个、用完即毁。
_pick_q = queue.Queue()       # handler 放入 {"kind": "folder"/"image", "initial":..., "result_q":...}
_pick_thread_ready = threading.Event()

_PICK_FILETYPES = [("图片", "*.jpg *.jpeg *.png *.webp *.bmp *.gif"), ("所有文件", "*.*")]


def _force_dialog_front(stop_evt, timeout=180):
    """辅助线程：本进程的系统对话框一冒头，就把它钉到最前台。
    零依赖——ctypes 直接调 Windows 的 user32，不装任何东西。
    病根：对话框从后台线程弹出，Windows 默认不给焦点、会躲到主窗后面；
    在隐藏的 Tk 主窗上设 topmost 传不到对话框（上次这么修，没用）。
    这里按「本进程 PID + 标准对话框类名 #32770」认出它，反复置顶＋抢焦点
    直到对话框关闭（stop_evt 置位）。非 Windows 或出错就静静退出，不影响弹窗本身。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        pid = kernel32.GetCurrentProcessId()

        HWnd = wintypes.HWND
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWnd, wintypes.LPARAM)
        # 必须显式声明参数签名：64 位下 HWND 是指针，不声明 ctypes 会按 32 位传，
        # 系统接口静默失败——实测过：不声明时置顶标志写不上，声明后立马生效。
        kernel32.GetCurrentProcessId.argtypes = []
        kernel32.GetCurrentProcessId.restype = wintypes.DWORD
        user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [HWnd, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsWindowVisible.argtypes = [HWnd]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetClassNameW.argtypes = [HWnd, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.ShowWindow.argtypes = [HWnd, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [HWnd, HWnd, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [HWnd]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [HWnd]
        user32.SetForegroundWindow.restype = wintypes.BOOL

        HWND_TOPMOST = -1
        SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040

        found = []
        def _enum_cb(hwnd, _lparam):
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf, 256)
                if buf.value == "#32770":          # Windows 标准对话框类＝系统选文件/文件夹窗
                    found.append(hwnd)
                    return False                   # 认出一个就够，停止枚举
            return True

        deadline = time.time() + timeout
        while not stop_evt.is_set() and time.time() < deadline:
            del found[:]
            try:
                user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
            except Exception:
                pass
            for hwnd in found:
                try:
                    user32.ShowWindow(hwnd, 9)     # SW_RESTORE：万一最小化，先还原
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            stop_evt.wait(0.15)                    # 抢得够勤，弹窗一露头就被压到最前
    except Exception as ex:
        log_err("force_dialog_front", ex)


def _pick_dialog_worker():
    """常驻线程：收到请求就在本线程里弹系统对话框（文件夹或选图片）。"""
    import tkinter as tk
    from tkinter import filedialog
    while True:
        req = _pick_q.get()
        kind = req.get("kind", "folder")
        initial = req.get("initial", "")
        result_q = req["result_q"]
        chosen = ""
        root = None
        stop_evt = None
        try:
            root = tk.Tk()
            # 主窗口挪到屏幕外并缩到最小：只让系统对话框可见，不露多余的 Tk 空窗
            root.geometry("1x1+9999+9999")
            root.withdraw()
            root.update_idletasks()
            # 系统级置顶：辅助线程盯住本进程弹出的对话框，一冒头就钉到最前。
            # 原来在隐藏 Tk 主窗上设 topmost 传不到对话框，已废弃。
            stop_evt = threading.Event()
            threading.Thread(target=_force_dialog_front, args=(stop_evt,), daemon=True).start()
            if kind == "image":
                opts = {"title": "选择纸底图片", "filetypes": _PICK_FILETYPES}
                if initial and os.path.isdir(initial):
                    opts["initialdir"] = initial
                chosen = filedialog.askopenfilename(**opts) or ""
            else:
                opts = {"title": "选择读物文件夹"}
                if initial and os.path.isdir(initial):
                    opts["initialdir"] = initial
                chosen = filedialog.askdirectory(**opts) or ""
            if chosen:
                chosen = os.path.abspath(chosen)
        except Exception as ex:
            log_err("pick_dialog", ex)
            chosen = ""
        finally:
            if stop_evt is not None:
                stop_evt.set()          # 对话框关了，停掉置顶辅助线程
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
            try:
                result_q.put_nowait(chosen)
            except Exception:
                pass

def pick_dialog(kind="folder", initial=""):
    """供 HTTP handler 调用：弹对话框，返回选中路径，取消/失败返回空串。带超时保护。"""
    result_q = queue.Queue(maxsize=1)
    if not _pick_thread_ready.is_set():
        threading.Thread(target=_pick_dialog_worker, daemon=True).start()
        _pick_thread_ready.set()
    _pick_q.put({"kind": kind, "initial": initial, "result_q": result_q})
    try:
        return result_q.get(timeout=180) or ""  # 给用户充足时间挑路径
    except queue.Empty:
        return ""

def pick_folder(initial=""):
    return pick_dialog("folder", initial)

def cleanup_temp(clean_audio=True):
    """退场清理：浏览器配置目录必清；音频缓存 mp3 按 clean_audio 决定清不清。
    点「清理并退出」清干净；点 ✕ 关窗保留——下次对同一章同一语速点朗读，秒开不用重新生成。"""
    n = 0
    if clean_audio:
        try:
            for fn in os.listdir(CACHE):
                if fn.endswith(".mp3"):
                    try:
                        os.remove(os.path.join(CACHE, fn)); n += 1
                    except Exception:
                        pass
        except Exception:
            pass
    if os.path.isdir(EDGE_PROFILE):
        # 逐文件删，避开部分安全软件对“整目录树一次性删除”的批量拦截
        for root, dirs, files in os.walk(EDGE_PROFILE, topdown=False):
            for fn in files:
                try:
                    os.remove(os.path.join(root, fn))
                except Exception:
                    pass
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except Exception:
                    pass
        try:
            os.rmdir(EDGE_PROFILE)
        except Exception:
            pass
    return n

def shutdown_all(srv, clean_audio=True):
    """彻底退场：关 Edge 窗口 → 清理缓存 → 关服务。
    clean_audio=True：点「清理并退出」/退出工具.bat，音频缓存一起清；
    clean_audio=False：点 ✕ 关窗自动退场，音频缓存保留，下次朗读秒开。"""
    # 先关 Edge --app 窗口（terminate 主进程，子进程随之退出）
    p = EDGE_PROC[0]
    if p:
        try:
            p.terminate()
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()
        except Exception:
            pass
    # 关键状态先收干净：关服务 + 删端口标记（即使后面清理被拦，下次启动也不受影响）
    try:
        if srv:
            srv.shutdown()
    except Exception:
        pass
    try:
        os.remove(os.path.join(BASE, "_port"))
    except Exception:
        pass
    # 最后清 profile / 音频缓存（可失败，不影响退场）
    try:
        cleanup_temp(clean_audio=clean_audio)
    except Exception:
        pass


def watchdog(srv, grace=120.0):
    """窗口关闭看门狗：页面每20秒发一次心跳，超过 grace 秒没心跳
    说明窗口已关（不是刷新——刷新几秒内就会重新心跳；最小化窗口的
    定时器会被 Edge 降到一分钟一次，所以宽限给足120秒），服务自动退场。
    ✕ 关窗走这里：音频缓存保留，供下次快速朗读。"""
    time.sleep(grace + 10)  # 启动宽限，等第一次心跳
    while True:
        last = HEARTBEAT[0]
        # 没收到过心跳（窗口没弹出来）或心跳断得太久（窗口已关）→ 退场
        if (not last) or (time.time() - last) > grace:
            shutdown_all(srv, clean_audio=False)
            return
        time.sleep(5)

TEXT_EXT = {".txt", ".md"}
READ_EXT = TEXT_EXT | {".epub"}
KNOWN_EXT = TEXT_EXT | {".epub", ".pdf"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}

# 章节识别：中文小说/读物的常见标题写法
CHAP_PATTERNS = [
    re.compile(r"^\s*第\s*[0-9零〇一二三四五六七八九十百千两]+\s*[章节回卷部篇集折出幕]\s*.{0,40}$"),
    re.compile(r"^\s*[0-9]{1,4}\s*[章节回卷部篇集]\s*[^\d].{0,40}$"),
    re.compile(r"^\s*[Cc]hapter\s+[0-9IVXLC]+.{0,40}$", re.I),
    re.compile(r"^\s*[0-9]{1,3}\s*[、.．]\s*\S.{0,40}$"),
    re.compile(r"^\s*[一二三四五六七八九十]+\s*[、.．]\s*\S.{0,40}$"),
]


def maybe_chapter(line):
    s = line.strip()
    if not s or len(s) > 60:
        return False
    for p in CHAP_PATTERNS:
        if p.match(s):
            return True
    return False


def decode_text(path):
    """中文 txt 常见编码挨个试，谁先不报错用谁。"""
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "big5"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="ignore")


def epub_text(path):
    """epub 本质就是 zip 包着 html，用标准库抽出正文。"""
    parts = []
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith((".html", ".xhtml", ".htm", ".txt"))]
        def key(n):
            m = re.search(r"(\d+)", os.path.basename(n))
            return (int(m.group(1)) if m else 9999, n)
        for n in sorted(names, key=key):
            raw = z.read(n)
            try:
                txt = raw.decode("utf-8")
            except Exception:
                txt = raw.decode("gb18030", errors="ignore")
            txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", txt, flags=re.I | re.S)
            txt = re.sub(r"<br\s*/?>|</p>|</div>|</h[1-6]>", "\n", txt, flags=re.I)
            txt = re.sub(r"<[^>]+>", "", txt)
            txt = html.unescape(txt)
            parts.append(txt)
    return "\n".join(parts)


def load_book(path):
    """返回 (纯文本, 来源类型)。pdf 不在这里处理。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT:
        return decode_text(path), ext.lstrip(".")
    if ext == ".epub":
        return epub_text(path), "epub"
    return "", ""


def build_chapters(text):
    """按识别到的章节标题切分成 [{title, text, chars}]。"""
    lines = text.splitlines()
    chapters = []
    cur_title = "正文"
    buf = []
    for ln in lines:
        if maybe_chapter(ln):
            if buf:
                chapters.append({"title": cur_title, "text": "\n".join(buf)})
                buf = []
            cur_title = ln.strip()
        buf.append(ln)
    if buf:
        chapters.append({"title": cur_title, "text": "\n".join(buf)})
    if not chapters:
        chapters = [{"title": "全文", "text": text}]
    for c in chapters:
        c["chars"] = len(c["text"])
    return chapters


def scan_folder(root):
    """只读扫描：列出读物 + 图片，标记各自能做目录/朗读的能力。"""
    books = []
    images = 0
    for dp, dns, fns in os.walk(root):
        for fn in fns:
            ext = os.path.splitext(fn)[1].lower()
            fp = os.path.join(dp, fn)
            try:
                size = os.path.getsize(fp)
            except Exception:
                size = 0
            rel = os.path.relpath(fp, root)
            if ext in TEXT_EXT or ext == ".epub":
                books.append({
                    "path": fp, "name": fn, "rel": rel, "ext": ext.lstrip("."),
                    "size": size, "can_read": True, "can_tts": True,
                })
            elif ext == ".pdf":
                books.append({
                    "path": fp, "name": fn, "rel": rel, "ext": "pdf",
                    "size": size, "can_read": False, "can_tts": False,
                    "note": "PDF 需文字层才能朗读；纯标准库无法可靠提取，故不在此朗读",
                })
            elif ext in IMAGE_EXT:
                images += 1
    books.sort(key=lambda b: b["rel"])
    return {"root": root, "books": books, "images": images}


def sweep_part_files():
    """启动清扫：上次合成中途退场留下的半成品碎片清掉。
    清 _part_（旧整章合成）和 _seg_（v5.1 边合边播分段），不动 audio_ 缓存（那是 ✕ 关窗留给下次秒开的）。"""
    n = 0
    try:
        for fn in os.listdir(CACHE):
            if (fn.startswith("_part_") or fn.startswith("_seg_")) and fn.endswith(".mp3"):
                try:
                    os.remove(os.path.join(CACHE, fn)); n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return n


def tts_available():
    try:
        import edge_tts  # noqa
        return True
    except Exception:
        return False


def voice_check():
    """探测晓晓是否可用：真发一个字，成功才算通。"""
    if not tts_available():
        return {"ok": False, "reason": "未安装 edge-tts"}
    try:
        import asyncio
        import edge_tts
        async def go():
            c = edge_tts.Communicate("测", "zh-CN-XiaoxiaoNeural")
            await c.save(os.path.join(CACHE, "_voice_check.mp3"))
        asyncio.run(go())
        return {"ok": True, "voice": "zh-CN-XiaoxiaoNeural"}
    except Exception as ex:
        return {"ok": False, "reason": "%s: %s" % (type(ex).__name__, str(ex)[:160])}


def synth_chapter(text, chapter_key, rate):
    """把一段文字用晓晓合成 mp3，存在缓存里，返回可播放的 URL 片段。"""
    if not tts_available():
        return {"ok": False, "error": "未安装 edge-tts，无法朗读"}
    digest = hashlib.md5((chapter_key + "|" + str(rate) + "|" + text[:80]).encode("utf-8")).hexdigest()[:16]
    out = os.path.join(CACHE, "audio_%s.mp3" % digest)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return {"ok": True, "file": os.path.basename(out), "cached": True}   # 缓存命中：秒开
    # 按字数切段，逐段合成再拼接（mp3 帧可直接首尾相接播放）
    chunks = [text[i:i+2200] for i in range(0, len(text), 2200)] or [text]
    import asyncio
    import edge_tts
    # 微软接口偶发抽风（NoAudioReceived），自动重试 4 次、间隔递增（2/4/6 秒）
    last_err = None
    for attempt in range(4):
        tmp_files = []
        try:
            async def go():
                # 多段并行合成（两路并发），单段失败只重试该段（最多3次），不拖累整章
                sem = asyncio.Semaphore(2)
                async def one(i, ch):
                    tmp = os.path.join(CACHE, "_part_%s_%d.mp3" % (digest, i))
                    tmp_files.append(tmp)
                    for a in range(3):
                        try:
                            async with sem:
                                comm = edge_tts.Communicate(ch, "zh-CN-XiaoxiaoNeural", rate=rate)
                                await comm.save(tmp)
                            return tmp
                        except Exception:
                            if a == 2:
                                raise
                            await asyncio.sleep(1 + a)
                parts = await asyncio.gather(*[one(i, ch) for i, ch in enumerate(chunks) if ch.strip()])
                with open(out, "wb") as w:
                    for t in parts:
                        with open(t, "rb") as r:
                            w.write(r.read())
                for t in parts:
                    try:
                        os.remove(t)
                    except Exception:
                        pass
            asyncio.run(go())
            return {"ok": True, "file": os.path.basename(out)}
        except Exception as ex:
            last_err = ex
            for t in tmp_files:   # 半成品碎片清掉，不留垃圾
                try:
                    os.remove(t)
                except Exception:
                    pass
            if attempt < 3:
                time.sleep(1.0 * (attempt + 1))   # 1/2/3 秒，配合并行别让用户等太久
    return {"ok": False, "error": "%s: %s（已自动重试 4 次仍失败，多半是网络或微软接口抽风，稍后再试）" % (type(last_err).__name__, str(last_err)[:200])}


# ========== v5.1 任务9：边合边播 ==========
# 一个 key（书#章#语速）对应一个后台合成任务。首段优先出、剩余两路并行、拼整落缓存。
# cancel 事件用于换语速时掐断后台队列——不是只断前端 HTTP 请求。
TTS_JOBS = {}          # key -> {cancel, parts:{i:文件名}, segs, digest, done, error, full, rate}
TTS_JOBS_LOCK = threading.Lock()


def tts_digest(key, rate, text):
    return hashlib.md5((key + "|" + str(rate) + "|" + text[:80]).encode("utf-8")).hexdigest()[:16]


def synth_one_part(text, rate, out_path, retries=4):
    """合成单段到 out_path，带自动重试（微软接口偶发抽风）。供后台 worker 调用。"""
    import asyncio
    import edge_tts
    last = None
    for a in range(retries):
        try:
            async def go():
                c = edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural", rate=rate)
                await c.save(out_path)
            asyncio.run(go())
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return True
            last = RuntimeError("空音频")
        except Exception as ex:
            last = ex
        if a < retries - 1:
            time.sleep(1.0 * (a + 1))
    return False


def tts_job_worker(key, text, rate, job):
    """后台：首段先合（前端拿到就开播）→ 剩余两路并行 → 拼整落缓存 → 清段文件。"""
    digest = job["digest"]
    cancel = job["cancel"]
    chunks = [text[i:i + 2200] for i in range(0, len(text), 2200)] or [text]
    job["segs"] = len(chunks)

    def part_path(i):
        return os.path.join(CACHE, "_seg_%s_%d.mp3" % (digest, i))

    def make(i):
        """合成第 i 段；每段开始前和完成后都查 cancel，掐断后不再往下合。"""
        if cancel.is_set():
            return False
        p = part_path(i)
        # 已有完整段（比如上次掐断留下又没清掉的）直接用
        if os.path.exists(p) and os.path.getsize(p) > 0:
            with TTS_JOBS_LOCK:
                job["parts"][i] = os.path.basename(p)
            return True
        ok = synth_one_part(chunks[i], rate, p)
        if ok and not cancel.is_set():
            with TTS_JOBS_LOCK:
                job["parts"][i] = os.path.basename(p)
        else:
            # 失败或合成途中被取消：段文件删掉，不留垃圾
            try:
                os.remove(p)
            except Exception:
                pass
        return ok

    try:
        # 首段优先：合完立刻暴露，前端开播，不再等整章
        if not make(0):
            if not cancel.is_set():
                job["error"] = "合成首段失败"
                job["done"] = True
            return
        rest = list(range(1, len(chunks)))
        if rest and not cancel.is_set():
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as ex:
                futs = [ex.submit(make, i) for i in rest]
                for f in futs:
                    try:
                        f.result()
                    except Exception:
                        pass
        if cancel.is_set():
            return
        # 全部就绪 → 静默拼成完整 mp3 落缓存，保住"下次秒开"
        out = os.path.join(CACHE, "audio_%s.mp3" % digest)
        with open(out, "wb") as w:
            for i in range(len(chunks)):
                p = part_path(i)
                if os.path.exists(p):
                    with open(p, "rb") as r:
                        w.write(r.read())
        job["full"] = os.path.basename(out)
        for i in range(len(chunks)):   # 段文件用完即清，不留垃圾
            try:
                os.remove(part_path(i))
            except Exception:
                pass
    except Exception as ex:
        log_err("tts_job", ex)
        job["error"] = "%s: %s" % (type(ex).__name__, str(ex)[:160])
    finally:
        job["done"] = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, path, ctype):
        """流式返回文件，支持 Range 分段——音频进度条拖动靠它（206 响应）。"""
        try:
            size = os.path.getsize(path)
        except Exception:
            self._json({"error": "无法读取文件"}, 404)
            return
        range_hdr = self.headers.get("Range", "")
        start, end = 0, size - 1
        partial = False
        m = re.match(r"bytes=(\d*)-(\d*)", range_hdr.strip())
        if m and (m.group(1) or m.group(2)):
            partial = True
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = int(m.group(2))
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            end = min(end, size - 1)
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except Exception:
            pass

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except Exception:
            self._json({"error": "文件不存在"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")  # 改版后浏览器不许拿旧页面糊弄人
        self.end_headers()
        self.wfile.write(body)

    def _pdf(self, path):
        """流式返回 PDF，支持 Range 分段，永不整本读入内存（大画册可达数 GB）。"""
        try:
            size = os.path.getsize(path)
        except Exception:
            self._json({"error": "无法读取文件"}, 404)
            return
        range_hdr = self.headers.get("Range", "")
        start, end = 0, size - 1
        partial = False
        m = re.match(r"bytes=(\d*)-(\d*)", range_hdr.strip())
        if m:
            partial = True
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = int(m.group(2))
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            end = min(end, size - 1)
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1024 * 256, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except Exception:
            pass  # 客户端关闭（翻页/停止）时正常中断，不记录

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._file(INDEX, "text/html; charset=utf-8")
        elif u.path in ("/pdfjs/pdf.min.mjs", "/pdfjs/pdf.worker.min.mjs"):
            # v5.1：pdf.js 库（ES module）。白名单两个文件名，防目录穿越。
            self._file(os.path.join(BASE, u.path.lstrip("/")), "text/javascript; charset=utf-8")
        elif u.path == "/favicon.ico":
            # 程序画的图标：绿色圆底＋白色翻开的书。有它，--app 窗口/任务栏就不再是 Edge 默认地球。
            self.send_response(200)
            self.send_header("Content-Type", "image/x-icon")
            self.send_header("Content-Length", str(len(FAVICON)))
            self.end_headers()
            self.wfile.write(FAVICON)
        elif u.path == "/api/voice":
            self._json(voice_check())
        elif u.path == "/api/audio":
            name = parse_qs(u.query).get("name", [""])[0]
            # audio_ 完整缓存 / _seg_ 边合边播的分段，都允许
            if not (re.fullmatch(r"[\w\-. ]+\.mp3", name) and
                    (name.startswith("audio_") or name.startswith("_seg_"))):
                self._json({"error": "非法文件名"}, 400)
                return
            self._stream(os.path.join(CACHE, name), "audio/mpeg")
        elif u.path == "/api/pdf":
            path = parse_qs(u.query).get("path", [""])[0]
            root = SCAN_ROOT[0]
            # 白名单：必须是最近扫描目录内的、真实存在的 .pdf
            try:
                ap = os.path.abspath(path)
                ok = (root and os.path.commonpath([root, ap]) == root
                      and ap.lower().endswith(".pdf") and os.path.isfile(ap))
            except Exception:
                ok = False
            if not ok:
                self._json({"error": "非法或越界的 PDF 路径"}, 400)
                return
            self._pdf(ap)
        elif u.path == "/api/pdfpages":
            # 数 PDF 总页数（给"尾页"按钮用）：数 "/Type /Page" 出现次数、扣掉 "/Type /Pages"。
            # 粗估法，个别压缩对象的 PDF 可能数不准；数不出来就返回 0，前端隐藏尾页钮。
            path = parse_qs(u.query).get("path", [""])[0]
            root = SCAN_ROOT[0]
            try:
                ap = os.path.abspath(path)
                ok = (root and os.path.commonpath([root, ap]) == root
                      and ap.lower().endswith(".pdf") and os.path.isfile(ap))
            except Exception:
                ok = False
            if not ok:
                self._json({"error": "非法或越界的 PDF 路径"}, 400)
                return
            n = 0
            try:
                with open(ap, "rb") as f:
                    raw = f.read()
                n = raw.count(b"/Type /Page") - raw.count(b"/Type /Pages")
                if n <= 0:
                    m = re.findall(rb"/Count\s+(\d+)", raw)
                    if m:
                        n = max(int(x) for x in m)
            except Exception as ex:
                log_err("pdfpages", ex)
            self._json({"pages": max(0, n)})
        elif u.path == "/api/tts_status":
            # v5.1 任务9：前端轮询合成进度。key 定位任务，返回已就绪段 + 总段数 + 是否拼整完成
            key = parse_qs(u.query).get("key", [""])[0]
            with TTS_JOBS_LOCK:
                job = TTS_JOBS.get(key)
                if not job:
                    self._json({"ok": False, "error": "无此任务"})
                else:
                    self._json({"ok": True, "segs": job["segs"], "parts": dict(job["parts"]),
                                "done": job["done"], "full": job["full"], "error": job.get("error", ""),
                                "cancelled": job["cancel"].is_set()})
        elif u.path == "/api/browse":
            self._json(self._browse(parse_qs(u.query).get("path", [""])[0]))
        elif u.path == "/api/pick_folder":
            # 弹系统原生"选择文件夹"窗口；initial 用输入框当前路径。取消返回空串，前端不动。
            initial = parse_qs(u.query).get("path", [""])[0]
            self._json({"path": pick_dialog("folder", initial)})
        elif u.path == "/api/pick_image":
            # 弹系统原生"选图片"窗口，供正文纸底用。取消返回空串。
            initial = parse_qs(u.query).get("path", [""])[0]
            self._json({"path": pick_dialog("image", initial)})
        elif u.path == "/api/paper_file":
            # 返回纸底图片内容（页面是 http 来源，本地图片须经本服务转发才能显示）
            p = parse_qs(u.query).get("path", [""])[0]
            ext = os.path.splitext(p)[1].lower()
            ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                     ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif"}.get(ext)
            if not ctype or not os.path.isfile(p):
                self._json({"error": "非法图片路径"}, 400)
                return
            try:
                with open(p, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
            except Exception as ex:
                log_err("paper_file", ex)
        elif u.path == "/api/ping":
            HEARTBEAT[0] = time.time()  # 页面心跳，供看门狗判断窗口是否已关
            self._json({"ok": True})
        elif u.path == "/api/state":
            self._json(load_state())
        elif u.path == "/api/readme":
            # 首开引导：把工具自带的 README.md 交给正文区当"使用说明"
            try:
                with io.open(os.path.join(BASE, "README.md"), "r", encoding="utf-8") as f:
                    self._json({"ok": True, "text": f.read()})
            except Exception as ex:
                self._json({"ok": False, "error": str(ex)})
        else:
            self._json({"error": "not found"}, 404)

    def _browse(self, p):
        p = (p or "").strip().strip('"')
        if not p:
            import string
            return {"path": "", "dirs": [c + ":\\" for c in string.ascii_uppercase if os.path.exists(c + ":\\")]}
        p = os.path.abspath(os.path.expanduser(p))
        if not os.path.isdir(p):
            p = os.path.dirname(p)
        if not os.path.isdir(p):
            return {"path": p, "dirs": [], "error": "路径不存在"}
        try:
            dirs = sorted([n for n in os.listdir(p) if os.path.isdir(os.path.join(p, n))], key=str.lower)
        except Exception:
            dirs = []
        return {"path": p, "dirs": dirs[:500]}

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {}
        try:
            if u.path == "/api/scan":
                root = os.path.abspath(os.path.expanduser((data.get("path") or "").strip().strip('"')))
                if not os.path.isdir(root):
                    self._json({"ok": False, "error": "目录不存在"})
                else:
                    SCAN_ROOT[0] = os.path.abspath(root)
                    res = scan_folder(root)
                    res["ok"] = True
                    self._json(res)
            elif u.path == "/api/read":
                path = data.get("path", "")
                text, kind = load_book(path)
                chapters = build_chapters(text) if text else []
                self._json({"ok": True, "kind": kind, "chars": len(text),
                            "chapters": chapters[:400]})
            elif u.path == "/api/tts":
                text = data.get("text", "")
                key = data.get("key", "x")
                rate = data.get("rate", "+0%")
                if not text.strip():
                    self._json({"ok": False, "error": "空文本"})
                else:
                    self._json(synth_chapter(text, key, rate))
            elif u.path == "/api/tts_begin":
                # v5.1 任务9：启动边合边播。先查完整缓存（秒开），没有就起后台分段合成。
                text = data.get("text", "")
                key = data.get("key", "x")
                rate = data.get("rate", "+0%")
                if not text.strip():
                    self._json({"ok": False, "error": "空文本"})
                    return
                digest = tts_digest(key, rate, text)
                full = "audio_%s.mp3" % digest
                fp = os.path.join(CACHE, full)
                if os.path.exists(fp) and os.path.getsize(fp) > 0:
                    self._json({"ok": True, "cached": True, "full": full,
                                "segs": 0, "chars": len(text)})
                    return
                nseg = max(1, (len(text) + 2199) // 2200)
                with TTS_JOBS_LOCK:
                    prev = TTS_JOBS.get(key)
                    if prev and not prev["done"] and not prev["cancel"].is_set() and prev["rate"] == rate:
                        pass   # 同 key 同语速的任务还在跑，直接复用，不重复起
                    else:
                        if prev:
                            prev["cancel"].set()   # 旧任务（换语速等）掐掉
                        TTS_JOBS[key] = {"cancel": threading.Event(), "parts": {}, "segs": nseg,
                                         "digest": digest, "done": False, "error": "", "full": "", "rate": rate}
                        threading.Thread(target=tts_job_worker,
                                         args=(key, text, rate, TTS_JOBS[key]), daemon=True).start()
                self._json({"ok": True, "cached": False, "segs": nseg, "chars": len(text)})
            elif u.path == "/api/tts_cancel":
                # 换语速/停止：掐掉后台合成队列——设 cancel 事件，worker 每段前后都查它
                key = data.get("key", "x")
                with TTS_JOBS_LOCK:
                    job = TTS_JOBS.get(key)
                    if job:
                        job["cancel"].set()
                        # 该任务锁定的段文件也清掉，免得残留占空间（worker 若正在写会随后退出）
                        digest = job["digest"]
                        for fn in os.listdir(CACHE):
                            if fn.startswith("_seg_%s_" % digest) and fn.endswith(".mp3"):
                                try:
                                    os.remove(os.path.join(CACHE, fn))
                                except Exception:
                                    pass
                self._json({"ok": True})
            elif u.path == "/api/clear_cache":
                n = 0
                for fn in os.listdir(CACHE):
                    if fn.startswith("audio_") and fn.endswith(".mp3"):
                        try:
                            os.remove(os.path.join(CACHE, fn)); n += 1
                        except Exception:
                            pass
                self._json({"ok": True, "removed": n})
            elif u.path == "/api/del_audio":
                # 听完即删：前端 onended 回调，删掉刚播完的那个缓存音频
                fn = data.get("file", "")
                if re.fullmatch(r"audio_[\w]+\.mp3", fn) and os.path.isfile(os.path.join(CACHE, fn)):
                    try:
                        os.remove(os.path.join(CACHE, fn))
                        self._json({"ok": True})
                    except Exception as e:
                        self._json({"ok": False, "error": str(e)})
                else:
                    self._json({"ok": False, "error": "非法文件名"})
            elif u.path == "/api/state":
                # 白名单：只落这些字段。v5.1 新增 strip_blank/text_dim/body_font/paper_dir
                # （paper_dir 是补漏——v5.1 前端写了但没进白名单，"记住上次选图目录"一直没生效）
                self._json({"ok": save_state({k: data.get(k) for k in
                            ("last_path", "last_book", "last_chapter", "shelf_w", "chap_w",
                             "theme", "paper", "paper_dir", "strip_blank", "text_dim", "body_font")})})
            elif u.path == "/api/quit":
                # 先回包，再从另起的线程彻底退场（关窗口+清缓存+关服务），避免把自己掐断
                self._json({"ok": True})
                threading.Thread(target=lambda: shutdown_all(srv_store[0], clean_audio=True), daemon=True).start()
            else:
                self._json({"error": "not found"}, 404)
        except Exception as ex:
            self._json({"ok": False, "error": "%s: %s" % (type(ex).__name__, str(ex)[:200])})


def load_state():
    """读取记住的书架路径与进度；文件不存在或损坏就返回空。"""
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(d):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except Exception as ex:
        log_err("save_state", ex)
        return False


def pick_port(preferred=8766):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", preferred)); s.close(); return preferred
    except OSError:
        s.close(); s2 = socket.socket(); s2.bind(("127.0.0.1", 0))
        p = s2.getsockname()[1]; s2.close(); return p


EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
EDGE2 = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"


def real_desktop():
    """取当前用户真实桌面路径（跟随重定向，不写死 C 盘或某盘符）。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_DESKTOPDIRECTORY=0x10，取的是重定向后的真实桌面
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)
        if buf.value and os.path.isdir(buf.value):
            return buf.value
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def trim_edge_shortcut():
    """Edge --app 每次启动会自动往桌面补一张 Microsoft Edge 快捷方式，清掉它。
    只认这一个精确文件名，别的文件一律不动。"""
    lnk = os.path.join(real_desktop(), "Microsoft Edge.lnk")
    if os.path.exists(lnk):
        try:
            os.remove(lnk)
        except Exception:
            pass


def open_window(url):
    """默认用 Edge --app 开无地址栏无工具栏的独立窗口；不可用则退回系统浏览器。"""
    if "--normal" in sys.argv:
        webbrowser.open(url)
        return
    edge = EDGE if os.path.exists(EDGE) else (EDGE2 if os.path.exists(EDGE2) else None)
    if edge:
        # --app：独立窗口、无地址栏/工具栏/标签；--user-data-dir 用独立配置避免串号
        profile = os.path.join(BASE, "_edge_profile")
        try:
            # 退场时会清空 profile，下次启动在 Edge 眼里就是"新用户"，
            # 新版会强开一个普通窗口弹欢迎/导数据/设默认浏览器的向导，盖住 --app 窗口。
            # 写一个空的 First Run 标记，再加三个开关，明确告诉它别走首次运行流程。
            os.makedirs(profile, exist_ok=True)
            first_run = os.path.join(profile, "First Run")
            if not os.path.exists(first_run):
                with open(first_run, "w"):
                    pass
            EDGE_PROC[0] = subprocess.Popen([edge, "--app=" + url,
                                             "--window-size=1280,860",
                                             "--user-data-dir=" + profile,
                                             "--no-first-run",
                                             "--no-default-browser-check",
                                             "--disable-fre",
                                             # 系统里登了微软账号时，Edge 每个新配置都会弹
                                             # "正在同步你的浏览数据"确认框，挡在 --app 窗口前面。
                                             "--disable-sync"])
            # Edge 启动会自动补桌面快捷方式（策略管不住），延时清两次
            threading.Timer(2, trim_edge_shortcut).start()
            threading.Timer(6, trim_edge_shortcut).start()
            return
        except Exception:
            pass
    webbrowser.open(url)


def quit_running():
    """被 退出工具.bat 以 --quit 调用：读 _port，通知服务退出。"""
    try:
        with open(os.path.join(BASE, "_port")) as f:
            port = int(f.read().strip())
    except Exception:
        return
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/quit" % port,
                                     data=b"{}", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass
    # 无论服务在不在，都清掉端口标记文件，避免残留误导下次启动
    try:
        os.remove(os.path.join(BASE, "_port"))
    except Exception:
        pass


def main():
    if "--quit" in sys.argv:
        quit_running()
        return
    port = pick_port()
    url = "http://127.0.0.1:%d/" % port
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True   # 退场不等合成线程拖尾，残留碎片下次启动清扫
    sweep_part_files()          # 启动即清扫上次残留的合成碎片
    srv_store[0] = srv
    silent = "--silent" in sys.argv
    if not silent:
        print("=" * 56)
        print("  阅见阅读器 已启动")
        print("  窗口：" + url)
        print("  关闭：点窗口右上角「退出」，或双击 退出工具.bat")
        print("=" * 56)
    # 把本次端口写进一个文件，供 退出工具.bat 精确找到并关闭服务
    try:
        with open(os.path.join(BASE, "_port"), "w") as f:
            f.write(str(port))
    except Exception as ex:
        log_err("write_port", ex)
    threading.Timer(1.2, lambda: (open_window(url) if "--nowindow" not in sys.argv else None)).start()
    # 无黑窗口模式（pythonw --silent）下启动看门狗：窗口关闭即自动退场清理
    if silent:
        threading.Thread(target=watchdog, args=(srv,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        log_err("serve_forever", ex)
    finally:
        try:
            os.remove(os.path.join(BASE, "_port"))
        except Exception:
            pass
        try:
            cleanup_temp(clean_audio=False)  # 兜底退场：浏览器配置清掉，音频缓存留给 ✕ 关窗的约定
        except Exception:
            pass


if __name__ == "__main__":
    main()
