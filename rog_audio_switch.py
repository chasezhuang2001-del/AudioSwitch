# -*- coding: utf-8 -*-
"""
ROG Delta II 2.4G 自动音频切换守护程序 (v3 - 秒级切换 + 托盘电量)
================================================================
- 秒级检测: 高速监听 COL02 RACE 通道 LINK UP/DOWN 指示 (0x2CB1)
- 备份检测: 每 2s 电量查询 (CC 12 07), 覆盖无指示的异常场景
- 切换: IPolicyConfig 设置默认播放 (console/multimedia/communications)
- 托盘: 右下角图标实时显示电量数字, 连接/断开弹气泡通知, 右键菜单
- 开机自启: HKCU\\...\\Run → ROGAudioSwitch

用法:
  python rog_audio_switch.py            # 常驻 (托盘 + 秒级切换)
  python rog_audio_switch.py --no-tray  # 常驻 (无托盘, 纯后台)
  python rog_audio_switch.py --once     # 单次检测
  python rog_audio_switch.py --query    # 只查电量
"""
import os
import sys

# 清除 PYTHONPATH 污染 (系统级变量指向其他 venv, 会导致 PIL 等加载错误版本)
os.environ.pop("PYTHONPATH", None)
sys.path = [p for p in sys.path if "hermes-agent" not in p.lower()]

import json
import time
import ctypes
import struct
import argparse
import threading
import traceback
from ctypes import wintypes
from datetime import datetime

# ---------------------------------------------------------------------------
# DPI 感知: 任务栏是 PerMonitorV2 DPI-aware, 若本进程 DPI-unaware,
# 系统会把角标窗口内容位图缩放 (虚拟化) → 显示模糊/缩放异常,
# 且 explorer 重绘任务栏时表面会被丢弃 → 消失。必须先声明 DPI 感知。
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)                       # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        pass

# ---------------------------------------------------------------------------
GUID_DEVINTERFACE_HID = (0x4d1e55b2, 0xf16f, 0x11cf, (0x88, 0xcb, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30))
VID, PID = 0x0B05, 0x1AFA
HEADSET_NAME = "ROG DELTA II"
FALLBACK_NAME = "R27U81"
# PyInstaller onefile 下 __file__ 指向临时解压目录, 必须用 exe 所在目录
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "state.txt")
LOG_FILE = os.path.join(BASE, "rog_switch.log")
MEM_FILE = os.path.join(BASE, "fallback.json")
CONFIG_FILE = os.path.join(BASE, "config.json")
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "ROGAudioSwitch"
LOW_BATT = 20                     # 低电量提醒阈值 (%)
BATT_BACKOFF_STREAK = 5           # 连续失败次数 → 拉长查询间隔
BATT_BACKOFF_INTERVAL = 10.0      # 退避后的查询间隔 (s)


def _load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass

POLL_MS = 0.03             # COL02 轮询间隔
BATT_INTERVAL = 2.0        # 电量查询间隔
BATT_FAIL_LIMIT = 3        # 连续电量无效 -> 断开 (备份)
BATT_OK_LIMIT = 3          # 连续电量有效 -> 连接 (备份)
READ_TIMEOUT_MS = 1500
INVALID_HANDLE = ctypes.c_void_p(-1).value

LINK_UP = b"\x02\x01\x01"
LINK_DOWN = b"\x02\x00\x01"

# ---------------------------------------------------------------------------
# 托盘图标 (内嵌通知区, 与 QQ/Steam 等应用一致)
# ---------------------------------------------------------------------------
HAS_TRAY = False
try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
    HAS_TRAY = True
except Exception:
    HAS_TRAY = False

try:
    _u = ctypes.WinDLL("user32", use_last_error=True)
    _u.GetSystemMetrics.restype = ctypes.c_int
    TRAY_SLOT = _u.GetSystemMetrics(49) or 16   # SM_CXSMICON
    ICON_SIZE = TRAY_SLOT * 2                   # 渲染 2 倍尺寸再缩放, 更清晰
except Exception:
    TRAY_SLOT, ICON_SIZE = 16, 32

_TRAY_FONT_PATH = None
for _fp in (r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf"):
    if os.path.exists(_fp):
        _TRAY_FONT_PATH = _fp
        break


def render_icon_number(level):
    """大号数字托盘图标: 电量数字, >=50% 绿色, <50% 红色。"""
    s = ICON_SIZE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    level = int(max(0, min(100, level or 0)))
    color = (50, 205, 50) if level >= 50 else (255, 80, 80)
    text = str(level)
    if _TRAY_FONT_PATH:
        try:
            # 字号从大往下找, 让数字尽量占满图标
            font, bbox = None, None
            for fs in range(s, 5, -2):
                font = ImageFont.truetype(_TRAY_FONT_PATH, fs)
                bbox = d.textbbox((0, 0), text, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                if tw <= s * 0.96 and th <= s * 0.94:
                    break
            if font:
                d.text((s / 2, s / 2), text, font=font, fill=color, anchor="mm")
        except Exception:
            pass
    return img


def render_icon_off():
    """断开图标: 灰色 X。"""
    s = ICON_SIZE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = max(2, s // 7)
    lw = max(2, s // 8)
    d.line([(m, m), (s - m, s - m)], fill=(128, 128, 128), width=lw)
    d.line([(s - m, m), (m, s - m)], fill=(128, 128, 128), width=lw)
    return img


class TrayUI:
    """托盘图标 (内嵌通知区): 电池样式显示电量, 断开时隐藏。
    左键双击刷新, 右键菜单: 状态 / 切换设备 / 刷新 / 退出。"""

    def __init__(self, stop_event, no_tray=False):
        self.stop_event = stop_event
        self.icon = None
        self.connected = None
        self.last_pct = None
        self._lock = threading.Lock()
        self._wd = None
        self._last_default = None
        self._low_warned = False       # 低电量提醒去重
        self.hide_when_off = _load_config().get("hide_when_off", True)
        if no_tray or not HAS_TRAY:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem(lambda item: self.status_text(), None, enabled=False),
                pystray.MenuItem("耳机关机后切回",
                                 pystray.Menu(lambda: self._fallback_menu_items())),
                pystray.MenuItem("恢复自动切换", self._on_resume_auto,
                                 enabled=lambda item: self._wd is not None
                                 and self._wd.user_suppress),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("关机后隐藏图标", self._on_toggle_hide,
                                 checked=lambda item: self.hide_when_off),
                pystray.MenuItem("开机自启", self._on_toggle_autostart,
                                 checked=lambda item: self._autostart_enabled()),
                pystray.MenuItem("立即刷新电量", self._on_refresh),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._on_quit),
            )
            self.icon = pystray.Icon("ROGAudioSwitch", render_icon_number(0),
                                     "ROG Delta II 自动切换", menu,
                                     default_action=self._on_refresh)
            self.icon.run_detached()
            self.icon.visible = False      # 初始隐藏, 连接后显示
        except Exception as e:
            self.icon = None
            print(f"[tray] 托盘初始化失败: {e}", flush=True)

    def attach_watchdog(self, wd):
        self._wd = wd

    def status_text(self):
        with self._lock:
            fb = f" · 回退: {self._wd.fallback_name}" if (self._wd and self._wd.fallback_name) else ""
            if self.connected is None:
                return f"状态: 检测中{fb}"
            if self.connected:
                return f"已连接 · 电量 {self.last_pct}%{fb}"
            return f"已断开 (声音已切回{self._wd.fallback_name or '音箱'})"

    def _notify_short(self, msg, title="ROG Delta II"):
        """气泡通知并 3 秒后自动关闭 (系统默认约 6 秒, 太久了)。"""
        try:
            self.icon.notify(msg, title)
            threading.Timer(3.0, self._dismiss_notification, daemon=True).start()
        except Exception:
            pass

    def _dismiss_notification(self):
        try:
            if self.icon:
                self.icon._remove_notification()   # NIF_INFO 空消息关闭气球
        except Exception:
            pass

    def _on_toggle_hide(self, icon=None, item=None):
        """切换: 关机后隐藏图标 / 显示关机图标 (灰 X)。"""
        self.hide_when_off = not self.hide_when_off
        cfg = _load_config()
        cfg["hide_when_off"] = self.hide_when_off
        _save_config(cfg)
        log(f"关机后隐藏图标: {'开' if self.hide_when_off else '关 (显示灰 X)'}")
        try:
            self.update(self.connected, self.last_pct)   # 立即应用
        except Exception:
            pass

    def _autostart_enabled(self):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
                winreg.QueryValueEx(k, AUTOSTART_NAME)
                return True
        except Exception:
            return False

    def _on_toggle_autostart(self, icon=None, item=None):
        import winreg
        try:
            if self._autostart_enabled():
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                log("已关闭开机自启")
            else:
                cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
                    winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
                log("已开启开机自启")
        except Exception as e:
            log(f"自启设置失败: {e}")

    def _on_resume_auto(self, icon=None, item=None):
        """手动切换抑制后, 一键恢复自动切换。"""
        if self._wd:
            self._wd.user_suppress = False
            log("用户手动恢复自动切换")

    def _fallback_menu_items(self):
        """动态构建回退设备子菜单 (每次打开时刷新, 当前回退设备打勾)。"""
        items = []
        try:
            renders, _ = get_audio()
            cur = self._wd.fallback_id if self._wd else None
            for did, nm in renders:
                label = (nm or "(未命名)").replace("&", "&&")
                items.append(pystray.MenuItem(
                    label,
                    self._mk_action(did, nm),
                    checked=self._mk_checked(did, cur)))
        except Exception:
            log("菜单设备枚举失败:\n" + traceback.format_exc())
        if not items:
            items.append(pystray.MenuItem("无可用设备", None, enabled=False))
        return items

    def _mk_action(self, d, n):
        """pystray action 回调必须是 2 参 (icon, item); 闭包捕获设备。"""
        return lambda icon, item: self._on_set_fallback(d, n)

    @staticmethod
    def _mk_checked(d, cur):
        return lambda item: d == cur

    def _on_set_fallback(self, dev_id, name):
        """设置耳机关机后切换回的设备 (不立即切换)。"""
        if self._wd:
            self._wd.set_fallback(dev_id, name)
            try:
                self._notify_short(f"耳机关机后将切回: {name}")
            except Exception:
                pass

    def _on_refresh(self, icon=None, item=None):
        # 由托盘线程调用: 触发主循环立即查电量
        _force_refresh.set()

    def _on_quit(self, icon=None, item=None):
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass
        self.stop_event.set()

    def update(self, connected, pct):
        """主循环调用: 更新托盘图标; 断开时隐藏; 低电量/连接时通知。"""
        if not self.icon:
            return
        with self._lock:
            was_connected = self.connected
            self.connected = connected
            if pct is not None:
                self.last_pct = pct
        try:
            if connected:
                if not self.icon.visible:
                    self.icon.visible = True      # 连接 → 显示
                self.icon.icon = render_icon_number(self.last_pct or 0)
                self.icon.title = (f"ROG Delta II · 电量 {self.last_pct}%"
                                   if self.last_pct is not None
                                   else "ROG Delta II · 已连接")
                # 刚连接 → 通知 (含电量)
                if was_connected is False:
                    self._notify_short(
                        f"耳机已连接, 电量 {self.last_pct}%" if self.last_pct is not None
                        else "耳机已连接")
                # 低电量提醒 (≤阈值, 只提醒一次, 回升后重置)
                if self.last_pct is not None:
                    if self.last_pct <= LOW_BATT and not self._low_warned:
                        self._low_warned = True
                        self._notify_short(f"耳机电量低 ({self.last_pct}%), 请及时充电")
                    elif self.last_pct > LOW_BATT:
                        self._low_warned = False
            else:
                if self.hide_when_off:
                    if self.icon.visible:
                        self.icon.visible = False     # 断开 → 隐藏图标
                else:
                    # 断开 → 显示关机图标 (灰 X)
                    self.icon.icon = render_icon_off()
                    self.icon.title = "ROG Delta II · 已断开"
                    if not self.icon.visible:
                        self.icon.visible = True
        except Exception:
            pass

class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


k32 = ctypes.WinDLL("kernel32", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
hid.HidD_GetInputReport.restype = wintypes.BOOL
hid.HidD_GetInputReport.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.ULONG]

# 主循环强制刷新事件 (托盘"立即刷新"菜单用)
_force_refresh = threading.Event()
_mutex_handle = None


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        # 日志轮转: 超 1MB 归档为 .old
        if os.path.getsize(LOG_FILE) > 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".old")
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_state(text):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def find_hid_path(col):
    class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", wintypes.DWORD * 4),
                    ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]
    guid = GUID(*GUID_DEVINTERFACE_HID)
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD]
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                                     ctypes.POINTER(GUID), wintypes.DWORD, wintypes.LPVOID]
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID,
                                                          wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    hdevs = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x2 | 0x10)
    if hdevs in (None, INVALID_HANDLE, ctypes.c_void_p(-1).value):
        return None
    try:
        for i in range(256):
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdevs, None, ctypes.byref(guid), i, ctypes.byref(did)):
                break
            req = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdevs, ctypes.byref(did), None, 0, ctypes.byref(req), None)
            need = req.value
            if need <= 8:
                continue
            class Detail(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", wintypes.WCHAR * ((need + 8) // 2))]
            d = Detail()
            d.cbSize = 8
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(hdevs, ctypes.byref(did), ctypes.byref(d), need, None, None):
                continue
            p = d.DevicePath
            if f"VID_{VID:04X}&PID_{PID:04X}" in p.upper() and col in p.upper():
                return p
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdevs)
    return None


def open_hid(path):
    h = k32.CreateFileW(path, 0xC0000000, 0x3, None, 3, 0x40000000, None)
    if h in (None, INVALID_HANDLE):
        return None
    return h


def read_interrupt(h, timeout_ms=150):
    buf = (ctypes.c_ubyte * 62)()
    ov = OVERLAPPED()
    evt = k32.CreateEventW(None, True, False, None)
    ov.hEvent = evt
    n = wintypes.DWORD(0)
    ok = k32.ReadFile(h, buf, 62, ctypes.byref(n), ctypes.byref(ov))
    if not ok:
        err = ctypes.get_last_error()
        if err != 997:
            k32.CloseHandle(evt)
            return None
        if k32.WaitForSingleObject(evt, timeout_ms) != 0:
            k32.CancelIo(h)
            k32.CloseHandle(evt)
            return None
        k32.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(n), False)
    k32.CloseHandle(evt)
    return bytes(buf[:n.value]) if n.value else None


class BatteryQuery:
    def __init__(self):
        self._path = None

    def query(self):
        path = self._path or find_hid_path("COL04")
        if not path:
            return None
        self._path = path
        h = k32.CreateFileW(path, 0xC0000000, 0x3, None, 3, 0x40000000, None)
        if h in (None, INVALID_HANDLE):
            return None
        try:
            out = (ctypes.c_ubyte * 64)()
            out[0], out[1], out[2] = 0xCC, 0x12, 0x07
            wov = OVERLAPPED()
            wevt = k32.CreateEventW(None, True, False, None)
            wov.hEvent = wevt
            written = wintypes.DWORD(0)
            ok = k32.WriteFile(h, out, 64, ctypes.byref(written), ctypes.byref(wov))
            if not ok:
                werr = ctypes.get_last_error()
                if werr != 997:
                    k32.CloseHandle(wevt)
                    return None
                if k32.WaitForSingleObject(wevt, 2000) != 0:
                    k32.CancelIo(h)
                    k32.CloseHandle(wevt)
                    return None
                k32.GetOverlappedResult(h, ctypes.byref(wov), ctypes.byref(written), False)
            k32.CloseHandle(wevt)
            buf = (ctypes.c_ubyte * 64)()
            ov = OVERLAPPED()
            evt = k32.CreateEventW(None, True, False, None)
            ov.hEvent = evt
            n = wintypes.DWORD(0)
            ok = k32.ReadFile(h, buf, 64, ctypes.byref(n), ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error()
                if err != 997:
                    k32.CloseHandle(evt)
                    return None
                if k32.WaitForSingleObject(evt, READ_TIMEOUT_MS) != 0:
                    k32.CancelIo(h)
                    k32.CloseHandle(evt)
                    return None
                k32.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(n), False)
            k32.CloseHandle(evt)
            data = bytes(buf[:n.value]) if n.value else bytes(buf[:64])
            if len(data) >= 7 and data[0] == 0xCC and data[1] == 0x12 and data[2] == 0x07:
                pct = data[6]
                if 1 <= pct <= 100:
                    return pct
            return None
        finally:
            k32.CloseHandle(h)


def _ensure_com():
    """确保当前线程 COM 已初始化 (幂等; pycaw/comtypes 需要)。
    S_OK(0)=刚初始化, S_FALSE(1)=已初始化, RPC_E_CHANGED_MODE(0x80010106)=
    已以其他模式初始化 — 三种情况 COM 都可用, 不得 Uninitialize 破坏状态。"""
    try:
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        if hr not in (0, 1, 0x80010106):
            ctypes.windll.ole32.CoUninitialize()
    except Exception:
        pass


def get_audio():
    _ensure_com()
    from pycaw.utils import AudioUtilities
    from pycaw.constants import DEVICE_STATE
    renders = []
    for d in AudioUtilities.GetAllDevices():
        st = getattr(d.state, "value", d.state)
        if st != DEVICE_STATE.ACTIVE.value:
            continue
        did = d.id or ""
        if did.startswith("{0.0.0."):
            renders.append((did, d.FriendlyName or ""))
    try:
        default_id = AudioUtilities.GetSpeakers().id
    except Exception:
        default_id = None
    return renders, default_id


def find_endpoint(renders, name_part):
    for did, nm in renders:
        if name_part in nm.upper():
            return did
    return None


def set_default_playback(dev_id):
    _ensure_com()
    from pycaw.utils import AudioUtilities
    from pycaw.constants import ERole
    AudioUtilities.SetDefaultDevice(dev_id, [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])


def switch_to(dev_id, what):
    try:
        set_default_playback(dev_id)
        log(f"已切换默认播放 → {what} ({dev_id})")
        return True
    except Exception as e:
        log(f"切换失败 → {what}: {e}")
        return False


class Watchdog:
    def __init__(self, ui=None):
        self.connected = None
        self.fallback_id = None
        self.fallback_name = None
        self.batt_fail_streak = 0
        self.batt_ok_streak = 0
        self.batt = BatteryQuery()
        self.ui = ui
        self.last_pct = None
        self.last_link_event = 0.0
        self.user_suppress = False    # 用户手动切换后抑制自动切换
        self.last_auto_switch = 0.0   # 最近一次守护自动切换时间

    def note_user_switch(self):
        """用户手动切换了默认设备: 暂停自动切换, 直到下次耳机操作 (状态翻转)。"""
        self.user_suppress = True
        log("用户手动切换设备, 暂停自动切换 (下次耳机开关机恢复)")

    def set_fallback(self, dev_id, name):
        """设置耳机关机后切换回的设备 (持久化到 fallback.json)。"""
        self.fallback_id = dev_id
        self.fallback_name = name
        self._save_mem()
        log(f"回退设备已设置: {name} ({dev_id[:48]})")
        return True

    def _load_mem(self):
        try:
            with open(MEM_FILE, "r", encoding="utf-8") as f:
                m = json.load(f)
                self.fallback_id = m.get("id")
                self.fallback_name = m.get("name")
        except Exception:
            pass

    def _save_mem(self):
        try:
            with open(MEM_FILE, "w", encoding="utf-8") as f:
                json.dump({"id": self.fallback_id, "name": self.fallback_name}, f)
        except Exception:
            pass

    def _resolve_fallback(self, renders, default_id):
        if self.fallback_id:
            for did, nm in renders:
                if did == self.fallback_id:
                    return self.fallback_id, nm
        rid = find_endpoint(renders, FALLBACK_NAME)
        if rid:
            return rid, FALLBACK_NAME
        if default_id:
            for did, nm in renders:
                if did == default_id and HEADSET_NAME not in nm.upper():
                    return did, nm
        for did, nm in renders:
            if HEADSET_NAME not in nm.upper():
                return did, nm
        return None, None

    def set_state(self, connected, battery=None, via=""):
        if connected == self.connected:
            return
        if battery is not None:
            self.last_pct = battery
        self.connected = connected
        self.batt_fail_streak = 0
        self.batt_ok_streak = 0
        # 耳机操作 (开关机) → 恢复自动切换
        if self.user_suppress:
            self.user_suppress = False
            log("耳机状态变化, 恢复自动切换")
        log(f"状态翻转: {'连接' if connected else '断开'} (via {via})")
        if self.ui:
            # 电量未知时显示最近一次已知值, 避免短暂显示 "--"
            self.ui.update(connected, battery if battery is not None else self.last_pct)
        try:
            renders, default_id = get_audio()
        except Exception:
            log("音频端点枚举失败: " + traceback.format_exc().splitlines()[-1])
            return
        headset_id = find_endpoint(renders, HEADSET_NAME)
        if connected:
            if self.fallback_id is None:
                fid, fname = self._resolve_fallback(renders, default_id)
                if fid:
                    self.fallback_id, self.fallback_name = fid, fname
                    self._save_mem()
                    log(f"记住回退设备: {fname} ({fid})")
            if headset_id and default_id != headset_id:
                if switch_to(headset_id, "ROG Delta II 耳机"):
                    self.last_auto_switch = time.time()
        else:
            if default_id and headset_id and default_id == headset_id:
                fid, fname = self._resolve_fallback(renders, default_id)
                if fid:
                    if switch_to(fid, fname or "回退设备"):
                        self.last_auto_switch = time.time()

    def on_link_packet(self, payload):
        if LINK_UP in payload:
            self.last_link_event = time.time()
            if self.connected is not True:
                self.set_state(True, via="LINK UP 指示")
                # 立即补查电量, 尽快显示数字
                try:
                    pct = self.batt.query()
                    if pct is not None:
                        self.last_pct = pct
                        if self.ui:
                            self.ui.update(True, pct)
                except Exception:
                    pass
        elif LINK_DOWN in payload:
            self.last_link_event = time.time()
            if self.connected is not False:
                self.set_state(False, via="LINK DOWN 指示")

    def battery_backup(self, battery):
        """电量备份检测: 仅在无 LINK 指示时 (链路事件 >30s 前) 才允许翻转状态。"""
        if battery is not None:
            self.last_pct = battery
            self.batt_ok_streak += 1
            self.batt_fail_streak = 0
            if (self.connected is not True and self.batt_ok_streak >= BATT_OK_LIMIT
                    and time.time() - self.last_link_event > 30):
                self.set_state(True, battery, via="电量确认")
        else:
            self.batt_fail_streak += 1
            self.batt_ok_streak = 0
            if (self.connected is not False and self.batt_fail_streak >= BATT_FAIL_LIMIT
                    and time.time() - self.last_link_event > 30):
                self.set_state(False, via="电量失效")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tray", action="store_true", help="无托盘图标运行")
    ap.add_argument("--once", action="store_true", help="查一次电量后退出")
    ap.add_argument("--query", action="store_true", help="只查电量")
    args = ap.parse_args()

    if args.query or args.once:
        b = BatteryQuery()
        pct = b.query()
        print(f"battery={pct if pct is not None else 'N/A'}")
        return

    # 单实例锁 (仅守护模式): 句柄必须保存引用, 否则被 GC 回收导致互斥失效
    global _mutex_handle
    _mutex_handle = k32.CreateMutexW(None, False, "ROGAudioSwitch_SingleInstance")
    if ctypes.get_last_error() == 183:
        print("另一个 ROGAudioSwitch 实例已在运行, 退出")
        sys.exit(0)

    stop_event = threading.Event()
    ui = None
    if not args.no_tray:
        ui = TrayUI(stop_event)

    wd = Watchdog(ui=ui)
    if ui:
        ui.attach_watchdog(wd)
    wd._load_mem()
    log("ROG Delta II 自动音频切换守护程序启动 (v6 托盘版)")
    log(f"回退设备记忆: {wd.fallback_name or '(无)'}")

    # COL02 打开失败自动重试 (dongle 未插/暂不可用时守护保持等待)
    col02 = find_hid_path("COL02")
    while not col02:
        log("未找到 COL02 RACE 通道, 5 秒后重试...")
        time.sleep(5)
        col02 = find_hid_path("COL02")
    h = open_hid(col02)
    while not h:
        log("无法打开 COL02, 5 秒后重试...")
        time.sleep(5)
        h = open_hid(col02)
    log("COL02 RACE 通道监听中")

    last_batt = 0.0
    while not stop_event.is_set():
        try:
            buf = (ctypes.c_ubyte * 62)()
            buf[0] = 7
            if hid.HidD_GetInputReport(h, buf, 62):
                data = bytes(buf)
                if len(data) >= 3 and data[0] == 0x07:
                    ln = struct.unpack("<H", data[1:3])[0]
                    if ln > 0:
                        wd.on_link_packet(data[3:3 + ln])
            r = read_interrupt(h, 120)
            if r and len(r) >= 3 and r[0] == 0x07:
                ln = struct.unpack("<H", r[1:3])[0]
                if ln > 0:
                    wd.on_link_packet(r[3:3 + ln])
            now = time.time()
            interval = (BATT_BACKOFF_INTERVAL if wd.batt_fail_streak >= BATT_BACKOFF_STREAK
                        else BATT_INTERVAL)
            if now - last_batt >= interval or _force_refresh.is_set():
                _force_refresh.clear()
                last_batt = now
                pct = wd.batt.query()
                wd.battery_backup(pct)
                write_state(f"CONNECTED battery={pct}" if wd.connected else "DISCONNECTED")
                if ui:
                    ui.update(wd.connected, pct)
                try:
                    renders, default_id = get_audio()
                    headset_id = find_endpoint(renders, HEADSET_NAME)                    # 期望: 连接→耳机, 断开→回退设备
                    want_headset = bool(wd.connected and headset_id
                                        and default_id != headset_id)
                    want_fallback = bool(wd.connected is False and default_id
                                         and headset_id and default_id == headset_id)
                    if want_headset or want_fallback:
                        if time.time() - wd.last_auto_switch <= 5:
                            pass          # 守护自己刚切换, 等生效
                        elif wd.user_suppress:
                            pass          # 用户手动切换过, 抑制直到下次耳机操作
                        else:
                            # 非守护所为的偏离 → 用户手动更改 → 抑制, 不再干预
                            wd.user_suppress = True
                            log("检测到默认播放被手动更改, 暂停自动切换 (下次耳机开关机恢复)")
                except Exception:
                    log("主循环音频处理失败: " + traceback.format_exc().splitlines()[-1])
        except Exception:
            log("轮询异常: " + traceback.format_exc())
        time.sleep(POLL_MS)

    log("守护程序退出")


if __name__ == "__main__":
    main()
