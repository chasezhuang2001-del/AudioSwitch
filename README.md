# ROGAudioSwitch — ASUS ROG Delta II 无线耳机自动音频切换器 / Automatic Audio Switcher

> Windows 托盘工具:耳机开机自动切换默认播放设备到耳机,关机自动切回所选设备,托盘图标实时显示电量。
> A Windows tray tool: auto-switches the default playback device to the headset when it powers on and back to your chosen fallback when it powers off, with live battery in the tray.

---

# 中文说明 / Chinese

## 致谢 Acknowledgements

本项目的 HID 协议研究基于开源项目 [**measureer/ROGDeltaTray**](https://github.com/measureer/ROGDeltaTray)(Windows 托盘电量显示 + 音频切换,无需 Armoury Crate)。感谢作者逆向并公开 ROG Delta II 的厂商协议。

**参考的技术点**
- [docs/protocol.md](https://github.com/measureer/ROGDeltaTray/blob/main/docs/protocol.md) 中的 HID 厂商协议文档
- **COL04 电量查询**:向 COL04 写输出报告 `CC 12 07`,响应第 6 字节 = 电量百分比
- **SetupAPI 枚举 HID 厂商集合**的方法 (打开 dongle 的厂商通道)

**本项目的改进**
1. **秒级检测**:参考实现依赖定时电量轮询 (分钟级),本项目新增 **COL02 RACE 链路指示**监听,开关机检测延迟提升到 **1-3 秒**
2. **双通道冗余**:链路指示 + 电量查询互相兜底,连续 3 次确认防误判
3. **稳定性**:查询失败自动退避 (防 dongle 卡死)、日志轮转、单实例锁、COM 显式初始化
4. **交互完善**:托盘电量数字 (**≥50% 绿 / <50% 红**)、回退设备可选、手动切换保护、开机自启开关、低电量提醒

## 功能

| 功能 | 说明 |
|---|---|
| ⚡ 秒级自动切换 | 耳机开机 (~2-3s) → 默认播放切到 ROG Delta II;关机 (~1-2s) → 切回所选设备 (默认 R27U81) |
| 🔋 托盘电量数字 | 通知区图标群内显示电量,**≥50% 绿色 / <50% 红色**,数字尽量大 |
| 📴 关机图标策略 | 耳机关机后默认**隐藏图标**;可在菜单改为**显示灰色 X 关机图标** |
| 🎧 回退设备可选 | 菜单"耳机关机后切回"选择断开后切回哪个设备 (持久保存) |
| 🛡 手动切换保护 | 在系统或菜单手动切换设备后,守护**不再抢回**,直到下次耳机开关机 (或菜单一键恢复) |
| 🔔 通知 | 连接时气泡 (含电量, 3 秒自动关闭);电量 ≤20% 提醒充电 (只提醒一次) |
| ⚙️ 菜单管理 | 关机图标策略、回退设备、开机自启开关、立即刷新、状态显示 |
| 🚀 开机自启 | 注册表 HKCU Run,可在菜单开关 |

## 运行环境

- **操作系统**:Windows 11 (24H2), 100% DPI 缩放 (托盘图标槽位 16×16)
- **屏幕**:2560×1440, 任务栏底部
- **Python**:3.14 (仅源码运行需要;`ROGAudioSwitch.exe` 免环境)
- **运行方式**:开机自启 (HKCU Run → `pythonw.exe rog_audio_switch.py`), 无窗口后台常驻

## 支持的设备

| 设备 | 说明 |
|---|---|
| **ROG Delta II (2.4GHz)** | 目标耳机;固件 AB156x (IoT_SDK_for_BT_Audio_V3.8.0) |
| **2.4G 接收器 (dongle)** | `USB\VID_0B05&PID_1AFA&MI_00`,VID 0B05 = 华硕 |
| **HID 通道** | `HID\VID_0B05&PID_1AFA&MI_03&COL02` (RACE 链路指示)、`COL04` (电量查询) |
| **回退设备 (示例)** | R27U81 (NVIDIA High Definition Audio),可在菜单更换 |

> 其他设备的适配需验证 dongle 是否使用相同的 ASUS 统一查询协议 (`CC 12 07`) 与 Airoha RACE 通道。

## 工作原理

### 1. 连接状态检测 — 双层机制

**主检测:COL02 RACE 链路指示 (秒级)**
- 通过 Windows SetupAPI 枚举 dongle 的 HID 集合,找到 COL02 (Airoha RACE 通道, usage page 0xFF13)
- 30ms 轮询读取,捕获链路状态指示帧:
  - `LINK UP` → 耳机连接
  - `LINK DOWN` → 耳机断开
- dongle 在链路变化瞬间主动推送,实测延迟 1-3 秒

**备份检测:COL04 电量查询 (兜底)**
- 每 2s 向 COL04 写输出报告 `CC 12 07`(ASUS 厂商统一查询)
- 响应第 6 字节 = 电量百分比;超时/无效值 (0xAA/0xFF) = 断开
- 连续 3 次确认翻转状态,覆盖 LINK 指示缺失的异常场景 (强制断电/超距/链路抖动)
- 连续 5 次失败自动退避 (查询间隔 2s→10s),防止 dongle 被高频查询卡死

> 为什么不用系统端点状态:耳机断电时 Windows 音频端点仍显示 Active,快照完全一致,无法区分。

### 2. 设备切换

- `pycaw` + `comtypes` 调用 `IPolicyConfig` COM 接口
- 同时设置 console / multimedia / communications 三角色默认播放设备
- 手动切换保护:检测到默认设备偏离期望且非守护所为 → 设置 `user_suppress`,停止干预;耳机状态翻转时自动恢复

### 3. 托盘图标

- `pystray` + `Pillow` 动态渲染 32×32 图标 (系统缩放到槽位大小,保证清晰)
- 电量数字自动适配字号;断开时按配置隐藏或显示灰 X

## 技术栈

- **Python 3.14**(ctypes 直接调用 Win32 API,无第三方 GUI 依赖)
- **SetupAPI**(枚举 HID 设备集合)/ **HID API**(打开通道、读写报告)
- **pycaw / comtypes**(WASAPI 音频端点枚举与默认设备切换)
- **pystray / Pillow**(系统托盘图标与渲染)
- **winreg**(开机自启管理)

## 调试设备切换时遇到的问题

1. **端点状态不可靠**:耳机关机后音频端点仍 Active,快照无差异,无法作为切换依据 → 改用 HID 厂商协议查询 (`CC 12 07`),并监听 Airoha RACE 通道的链路状态指示,实现秒级检测
2. **查询信号静默**:部分状态变化不产生任何 HID 报告 (开机闲置时全静默) → 双通道冗余设计 (LINK 指示 + 电量查询互相兜底),连续 3 次确认防误判
3. **dongle 被高频查询卡死**:返回 0xAA 且 LINK 指示消失 → 连续失败自动退避 (2s→10s),必要时重新插拔 dongle 恢复
4. **COM 未初始化导致切换失败**:多线程环境下 comtypes 全局初始化标志与线程实际初始化不一致,枚举/切换抛 `CO_E_NOTINITIALIZED` → `CoInitializeEx` 显式初始化 (幂等,兼容 `RPC_E_CHANGED_MODE`)

## 安装与使用

```bat
:: 方式一: 免安装 (推荐)
双击 ROGAudioSwitch.exe 即可运行

:: 方式二: 源码运行
python -m venv venv
venv\Scripts\pip install -r requirements.txt
start.bat

:: 查电量 (调试)
venv\Scripts\python rog_audio_switch.py --query
```

命令行参数:`--no-tray` (无图标运行)、`--once` (单次检测)、`--query` (只查电量)。

## 配置文件

| 文件 | 说明 |
|---|---|
| `config.json` | 界面配置 (当前:关机后是否隐藏图标) |
| `fallback.json` | 回退设备记忆 (耳机关机后切回的设备) |
| `state.txt` | 运行状态快照 |
| `rog_switch.log` | 日志 (超 1MB 自动轮转) |

## 已知限制

- Win11 通知区图标固定单槽位,无法显示自定义大小/宽图标
- 强制断电时电量缓存最长 ~3 分钟失效 (备份检测的兜底延迟)
- 需要 dongle 的 HID 集合访问权限 (普通用户即可)

---

# English Documentation / 英文说明

## Acknowledgements

The HID protocol research in this project is based on the open-source project [**measureer/ROGDeltaTray**](https://github.com/measureer/ROGDeltaTray) (a Windows tray app showing ROG Delta II battery level with auto audio device switching, no Armoury Crate required). Thanks to the author for reverse-engineering and publishing the ROG Delta II vendor protocol.

**Referenced techniques**
- The HID vendor protocol documentation in [docs/protocol.md](https://github.com/measureer/ROGDeltaTray/blob/main/docs/protocol.md)
- **COL04 battery query**: write output report `CC 12 07` to COL04; response byte 6 = battery percentage
- **SetupAPI enumeration of HID vendor collections** (opening the dongle's vendor channel)

**Improvements in this project**
1. **Second-level detection**: the reference relies on timed battery polling (minute-level); this project adds **COL02 RACE link indication** monitoring, cutting detection latency to **1-3 seconds**
2. **Dual-channel redundancy**: link indication + battery query back each other up, with 3-consecutive confirms against misjudgment
3. **Stability**: automatic backoff on query failures (prevents dongle hangs), log rotation, single-instance mutex, explicit COM init
4. **Polished interaction**: tray battery digits (**green ≥50% / red <50%**), selectable fallback device, manual-switch protection, autostart toggle, low-battery alert

## Features

| Feature | Description |
|---|---|
| ⚡ Second-level auto switch | Headset ON (~2-3s) → default playback → ROG Delta II; OFF (~1-2s) → back to chosen fallback (default: R27U81) |
| 🔋 Tray battery number | Live battery % in the notification area, **green ≥50% / red <50%**, maximized digit size |
| 📴 Off-state icon policy | Default: icon hidden when headset is off; optional: show a grey ✕ "off" icon instead |
| 🎧 Selectable fallback device | Menu "Switch back to when off" picks which device to return to (persisted) |
| 🛡 Manual-switch protection | After a manual device change (system or menu), the daemon **won't override** until the next headset power toggle (or one-click resume from menu) |
| 🔔 Notifications | Connect balloon (with battery %, auto-closes in 3s); low battery ≤20% reminder (once) |
| ⚙️ Menu management | Off-icon policy, fallback device, autostart toggle, refresh, status (incl. current fallback) |
| 🚀 Autostart | HKCU Run registry entry, toggleable from menu |

## Runtime Environment

- **OS**: Windows 11 (24H2), 100% DPI scaling (tray slot 16×16)
- **Display**: 2560×1440, taskbar at bottom
- **Python**: 3.14 (source mode only; `ROGAudioSwitch.exe` needs no environment)
- **Mode**: autostart background daemon (`pythonw.exe rog_audio_switch.py`, no window)

## Supported Devices

| Device | Notes |
|---|---|
| **ROG Delta II (2.4GHz)** | Target headset; firmware AB156x (IoT_SDK_for_BT_Audio_V3.8.0) |
| **2.4G dongle** | `USB\VID_0B05&PID_1AFA&MI_00` (VID 0B05 = ASUSTeK) |
| **HID channels** | `HID\VID_0B05&PID_1AFA&MI_03&COL02` (RACE link indication), `COL04` (battery query) |
| **Fallback device (example)** | R27U81 (NVIDIA High Definition Audio), changeable via menu |

> Adapting other devices requires verifying the dongle uses the same ASUS unified query protocol (`CC 12 07`) and the Airoha RACE channel.

## How It Works

### 1. Connection detection — dual-layer

**Primary: COL02 RACE link indication (seconds)**
- Enumerates the dongle's HID collections via Windows SetupAPI, finds COL02 (Airoha RACE channel, usage page 0xFF13)
- Polls every 30ms, captures link-state indication frames:
  - `LINK UP` → headset connected
  - `LINK DOWN` → headset disconnected
- The dongle pushes indications instantly on link change; measured latency 1-3s

**Backup: COL04 battery query (safety net)**
- Every 2s writes `CC 12 07` to COL04 (ASUS unified vendor query)
- Response byte 6 = battery %; timeout/invalid (0xAA/0xFF) = disconnected
- 3 consecutive confirms flip the state, covering scenarios with missing link indications (hard power cut / out of range / link flapping)
- 5 consecutive failures trigger backoff (interval 2s→10s) to avoid hanging the dongle with frequent queries

> Why not use endpoint state: when the headset powers off, the Windows audio endpoint stays Active — snapshots are identical and cannot distinguish.

### 2. Device switching

- `pycaw` + `comtypes` call the `IPolicyConfig` COM interface
- Sets default playback for console / multimedia / communications roles simultaneously
- Manual-switch protection: a default-device deviation not caused by the daemon sets `user_suppress`, halting intervention; auto-resumed on headset state flip

### 3. Tray icon

- `pystray` + `Pillow` dynamically render a 32×32 icon (system-scaled to the slot for sharpness)
- Digit size auto-fits; hidden or grey ✕ per config when off

## Tech Stack

- **Python 3.14** (ctypes calling Win32 APIs directly; no third-party GUI dependency)
- **SetupAPI** (HID device collection enumeration) / **HID API** (open channel, read/write reports)
- **pycaw / comtypes** (WASAPI endpoint enumeration & default-device switching)
- **pystray / Pillow** (system tray icon & rendering)
- **winreg** (autostart management)

## Issues Encountered While Debugging Device Switching

1. **Unreliable endpoint state**: the audio endpoint stays Active after power-off — useless as a switch trigger → moved to the HID vendor protocol (`CC 12 07`) plus the Airoha RACE link indication for second-level detection
2. **Silent query signals**: some state changes produce no HID report at all (idle-on silence) → dual-channel redundancy (link indication + battery query), 3-consecutive-confirm to avoid misjudgment
3. **Dongle hang from frequent queries**: returns 0xAA with link indications gone → automatic backoff (2s→10s); replug the dongle to recover if it still hangs
4. **COM not initialized causing switch failure**: in multi-threaded environments the comtypes global init flag diverges from the actual per-thread state, throwing `CO_E_NOTINITIALIZED` → explicit idempotent `CoInitializeEx` (tolerant of `RPC_E_CHANGED_MODE`)

## Install & Usage

```bat
:: Option 1: No-install (recommended)
Double-click ROGAudioSwitch.exe to run

:: Option 2: Run from source
python -m venv venv
venv\Scripts\pip install -r requirements.txt
start.bat

:: Query battery (debug)
venv\Scripts\python rog_audio_switch.py --query
```

CLI flags: `--no-tray` (run without icon), `--once` (single check), `--query` (battery only).

## Config Files

| File | Purpose |
|---|---|
| `config.json` | UI config (currently: hide icon when off) |
| `fallback.json` | Fallback device memory (device to switch back to when off) |
| `state.txt` | Runtime state snapshot |
| `rog_switch.log` | Log (auto-rotates at 1MB) |

## Known Limitations

- The Win11 notification area icon slot is fixed — no custom-size/wide icons
- After a hard power cut, battery cache can linger up to ~3 min (backup-detection latency)
- Requires HID collection access to the dongle (available to normal users)
