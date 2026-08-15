# ROGAudioSwitch — Automatic Audio Switcher for ASUS ROG Delta II

> A Windows tray tool: automatically switches the default playback device to the ROG Delta II headset when it powers on, and back to your chosen fallback device when it powers off. The tray icon shows the battery level in real time.

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
- **Python**: 3.14 (venv, see `requirements.txt`)
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
:: 1. Create venv and install dependencies
python -m venv venv
venv\Scripts\pip install -r requirements.txt

:: 2. Run
start.bat

:: 3. Query battery (debug)
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

## Hardware Adaptation Reference

- Protocol reference: measureer/ROGDeltaTray (docs/protocol.md)
