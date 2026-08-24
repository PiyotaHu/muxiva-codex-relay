# muxiva-codex-relay

让 Muxiva ESP32 通过蓝牙使用同一台 Windows 或 macOS 电脑上的 Codex。Codex 通道不需要 Wi-Fi、HTTP、IP 地址、端口或共享 token。

```text
ESP32 麦克风
  -- BLE PCM --> 桌面 Relay
  --> SenseVoice 本地 ASR / 可选 S1-mini 清洗
  --> 本机 Codex app-server
  -- BLE 状态 --> ESP32 Codex 页面
```

Relay 只在本机调用 Codex。蓝牙连接范围和电脑登录会话构成设备边界，不会把 Codex socket、HTTP 服务或操作权限暴露到局域网。

## 一键开始

需要 Python 3.11+、已登录的 Codex CLI，以及开启的电脑蓝牙。

macOS：

```bash
./scripts/install-macos.sh
./scripts/start-relay.sh
```

Windows：

```powershell
python -m pip install -e .
python scripts/install-sensevoice.py
.\scripts\start-relay.ps1
```

首次安装会从 `.env.example` 创建 `.env`。通常只需把 `MUXIVA_CODEX_CWD` 改成 Codex 要操作的工作目录；不需要配置 token、ESP32 地址或电脑 IP。

随后在开发板按 KEY 切到 Codex 页：

- 只发现一块 `Muxiva-RLCD` 时，Relay 自动连接，不弹确认。
- 同时发现多块同名设备时，前台首次运行会列出蓝牙设备并要求选一个；选择保存到 `runtime/ble-device.json`，以后登录自启动直接重连该设备。
- 在 Codex 页按 BOOT 开始说话。停顿会结束录音；识别结果显示后，可按 BOOT 立即提交、按 KEY 取消，或等待 3 秒自动提交。

离开 Codex 页后，固件关闭该页的 BLE 服务并释放资源；天气和小喵使用的 Wi-Fi 链路保持原样。

## 会话规则

`MUXIVA_CODEX_TARGET=latest` 会在每次进入 Codex 页时选择电脑上最近更新的任务，并在本次页面停留期间固定该任务。也可以配置具体任务 UUID。语音确认后，Relay 使用 Codex `app-server` 的 `turn/start` 或 `turn/steer` 续接同一任务，不会为每条语音创建新任务。

当目标任务正在 Codex Desktop 中执行时，Relay 会使用平台本机 IPC 把请求交给真实 owner：Windows 使用当前用户 named pipe，macOS/Linux 使用当前用户 Unix socket。该 IPC 不通过蓝牙或网络暴露。

## ASR 与清洗

默认使用 SenseVoiceSmall INT8 在电脑本地识别 16 kHz PCM。可选的 S1-mini by Superwhisper 是英文文本清洗模型，不是 ASR；服务不可用时自动使用 SenseVoice 原文。

安装/启动可选 S1-mini：

```powershell
.\scripts\install-llama-cpp.ps1
.\scripts\start-s1-mini.ps1
```

```bash
brew install llama.cpp
./scripts/start-s1-mini.sh
```

## 登录自启动

首次有多块同名设备时，请先前台运行一次并完成选择，再安装后台启动。

Windows：

```powershell
.\scripts\install-autostart.ps1
```

macOS：

```bash
./scripts/install-autostart-macos.sh
```

脚本带退出重启策略。macOS 首次使用时需在系统提示中允许 Terminal/Python 使用蓝牙。

## 配置

主要配置均为可选：

- `MUXIVA_BLE_DEVICE_NAME`：开发板广播名，默认 `Muxiva-RLCD`。
- `MUXIVA_BLE_SELECTION_PATH`：已确认设备的本机记录文件。
- `MUXIVA_CODEX_TARGET`：`latest` 或固定任务 UUID。
- `MUXIVA_CODEX_CWD`：Codex 工作目录。
- `MUXIVA_CODEX_SANDBOX`、`MUXIVA_CODEX_APPROVAL_POLICY`：Codex 本机执行策略。
- `MUXIVA_ASR_PROVIDER`：默认 `sensevoice`，也支持已有的 Qwen ASR 配置。

待确认文本保存在 Git 忽略的 `runtime/pending-previews.json`，Relay 重启后仍可完成确认。设备选择只保存蓝牙标识，不保存权限 token。

## 开发与测试

```powershell
$env:PYTHONPATH = "src"
python -m pytest
```

测试覆盖 BLE 控制帧、PCM 重组、预览/确认/取消、设备选择、状态分片、会话恢复和本机 Codex adapter。CI 在 Windows 与 macOS 上运行同一组测试。

## License

MIT。模型权重遵循各自许可证，本仓库不分发权重。
