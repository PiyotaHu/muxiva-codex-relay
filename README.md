# muxiva-codex-relay

让同一局域网里的 ESP32 使用电脑上的 Codex，而不要求 ESP32 直接访问 Codex 云端。

## 它解决什么问题

```text
ESP32 麦克风
  -> PCM 直传电脑 relay（不经过树莓派/Xiaozhi）
  -> SenseVoiceSmall INT8 本地 ASR + ITN
  -> S1-mini by Superwhisper 二次清洗（支持范围内）
  -> Codex app-server
  -> Codex 会话状态
  -> ESP32 Codex 页面
```

Wi-Fi 与 BLE 可以同时启用。ESP32-S3 作为低功耗蓝牙 GATT 外设，电脑端 relay 作为中心设备主动重连；两条链路携带相同的 `request_id`，relay 会去重，因此同一条语音任务最多提交一次。BLE 使用 `bleak` 可选依赖：`pip install -e .[ble]`，再设置 `MUXIVA_BLE_ENABLED=1`。

relay 使用 Codex CLI 自带的本机 `app-server` JSONL 协议来恢复会话并追加新的 turn，不写 Codex 的 SQLite 数据库，也不模拟桌面 UI。`latest` 会在开发板每次进入 Codex 页面时选择最近更新的 Codex 会话，并在本次页面停留期间锁定该会话；后续语音任务会继续追加到同一会话，避免执行过程中跳转。离开后再次进入页面会重新选择最新会话。也可以把 `MUXIVA_CODEX_TARGET` 固定为具体 thread UUID；固定会话恢复失败时任务会明确失败，不会静默创建新会话。

Codex 连接默认遵循官方 App Server transport 边界：Windows、macOS 和 Linux 统一使用 `stdio://` JSONL。官方 App Server 负责会话读取，以及 relay 自己拥有的会话的 `turn/start` / `turn/steer`。但一个会话如果已被 Codex Desktop 的另一个 app-server 进程持有，第二个 app-server 会返回 `already has an active writer`；此时不能继续重试 `thread/resume`，也不能新建会话规避冲突。

为继续同一个 Desktop 会话，relay 只在上述 ownership 冲突时启用一个隔离的 Desktop IPC 兼容适配器：Windows 使用当前用户的 `\\.\pipe\codex-ipc` named pipe，macOS/Linux 使用 `~/.codex/ipc/ipc.sock` Unix socket，并通过显式版本的 `thread-owner-discovery` 和 follower turn 消息把请求转交给真实 owner。该 IPC 是 Codex Desktop 自身使用、带版本号但未列入公开 App Server API 的进程路由协议，因此适配器会严格 fail-closed：版本、权限或 owner 发现失败时显示真实错误，绝不把失败伪装成空闲会话或静默创建新会话。面向 ESP32 的 HTTP/BLE 仍是 relay 自己的设备入口，不能把任何 Codex 本机 socket 直接暴露给局域网设备。

如果目标会话已有一个 Codex 回答正在执行，relay 会读取真实的活动 turn id，并通过官方 `turn/steer` 把确认后的语音直接追加到当前回答，不创建新 turn，也不进入持久队列。会话空闲时才使用 `turn/start` 启动新 turn；无法确认真实活动 turn 时会明确报错，不再把 `thread/resume` 异常误报成“回答中、已排队”。

为避免影响同一开发板上的小智实时音频，Codex 状态推送默认休眠。ESP32 切入 Codex 页时调用 `/v1/display`，发送 `active=true` 和 `refresh_latest=true`，激活推送并重新选择最新会话；离开页面时立即停推。显式刷新不依赖 relay 先收到休眠通知，因此即使切出页面时短暂断网，下一次进入仍能自愈。提交录音只激活推送，不会在当前页面会话中途跳到另一个会话。

电脑端 relay 与 S1-mini 启动脚本都带异常退出后的自动重启循环；Windows 使用计划任务，macOS 使用用户级 `launchd`。relay 的 Python 服务同时支持 Windows、macOS Intel 和 Apple Silicon。两端都以官方 `codex app-server` stdio 协议为主；仅当目标会话正在 Codex Desktop 中被持有时，分别使用 named pipe 或 Unix socket 的 owner 路由适配器。

## 快速开始（Windows / macOS）

1. 复制 `.env.example` 为 `.env`，至少设置 relay token、ESP32 Hub 地址和 Codex 工作目录。token 必须和固件 `secret_config.h` 中的 `HUB_AUTH_TOKEN` 相同。
2. 安装 relay 与跨平台本地 ASR。

   Windows：

   ```powershell
   python -m pip install -e .
   python scripts/install-sensevoice.py
   ```

   macOS：

   ```bash
   ./scripts/install-macos.sh
   ```

   该脚本创建仓库内 `.venv`、安装 SenseVoice 和可选 BLE 依赖，并检查 Codex CLI。Codex CLI 请使用 OpenAI 官方 macOS 安装器安装并先运行一次 `codex` 完成登录。

3. 可选安装 S1-mini 的本机运行时。

   Windows：

   ```powershell
   .\scripts\install-llama-cpp.ps1
   .\scripts\start-s1-mini.ps1
   ```

   macOS：

   ```bash
   brew install llama.cpp
   ./scripts/start-s1-mini.sh
   ```

4. 启动 relay：

   ```powershell
   .\scripts\start-relay.ps1
   ```

   macOS：

   ```bash
   ./scripts/start-relay.sh
   ```

   验证健康后再安装登录自启动：

   ```bash
   ./scripts/install-autostart-macos.sh
   ```

   若也要让 S1-mini 登录后自动运行：

   ```bash
   MUXIVA_S1_AUTOSTART=1 ./scripts/install-autostart-macos.sh
   ```

5. Windows 在防火墙中仅允许 TCP 8765 的“专用网络”入站；macOS 首次启动时允许 Python 接收入站连接。启用 BLE 时还需允许终端或 Python 使用蓝牙；ESP32 通过网络上传 PCM，不需要 Mac 麦克风权限。
6. ESP32 切到 Codex 页，按 BOOT 开始录音；说完停顿，或再次按 BOOT 手动结束。屏幕显示 ASR 清洗结果后，再按 BOOT 确认提交，或按 KEY 取消。

登录自启动：

```powershell
.\scripts\install-autostart.ps1
```

管理员环境会安装带重启策略的计划任务；普通用户环境会自动退回当前用户的登录启动项。两种方式都使用脚本内置的进程守护。

健康检查：`GET http://127.0.0.1:8765/health`。带 Bearer token 的 `GET /v1/status` 还会返回 Codex 子进程是否运行、PID、等待中的 JSON-RPC 请求数、transport 和最近 stderr，便于区分“relay 活着”和“Codex backend 可用”。状态接口和提交接口都使用 Bearer token。两阶段录音协议依次使用 `POST /v1/audio/preview`、`POST /v1/pending/confirm` 或 `POST /v1/pending/cancel`；旧版立即提交接口 `POST /v1/audio` 和文本接口 `POST /v1/transcripts` 继续保留兼容。

待确认文本默认保存 10 分钟，并写入被 Git 忽略的 `runtime/pending-previews.json`。这保证 relay 在“显示识别结果”和“第二次按 BOOT”之间重启时仍能继续确认。确认前不会调用 Codex，也不会占用 Codex 队列；取消后不会创建 turn。

树莓派桥接只保留为可选网络转发方案，不参与 Codex 的录音、ASR、清洗或任务提交。默认部署下 ESP32 直接访问电脑的 8765 端口。

## 按键约定

- KEY 单击：通常在天气 → Codex → 天气之间切换；出现待确认语音时，KEY 只取消本次提交并停留在 Codex 页。
- Codex 页第一次按 BOOT：启用 ESP32 麦克风直传电脑；说完停顿自动结束，录音中再按一次可手动结束。
- 屏幕出现识别结果后：再按 BOOT 才正式提交 Codex；按 KEY 则取消。确认前 Codex 不会收到任务。
- 其他页面按 BOOT：保持原来的小智语音对话行为。

## S1-mini 的边界

`S1-mini by Superwhisper` 是 ASR 文本清洗模型，不是语音识别模型。本仓库先用 SenseVoiceSmall 完成基础识别与 ITN，再在 S1-mini 支持范围内进行语义清洗。S1-mini 不可用时仍会提交 SenseVoice 原文。

推荐使用 Q4_K_M GGUF。启动参数必须包含 `--jinja`、`enable_thinking=false` 和 `--temp 0`。本项目不分发模型权重；使用或重新分发模型时，请遵守其 Apache 2.0 加命名条款，并保留名称 “S1-mini by Superwhisper”。

- [S1-mini by Superwhisper 模型说明](https://huggingface.co/superwhisper/s1-mini)
- [S1-mini GGUF 权重](https://huggingface.co/superwhisper/s1-mini-GGUF)
- [llama.cpp](https://github.com/ggml-org/llama.cpp)

## 安全默认值

- HTTP 服务要求 Bearer token。
- 状态推送显式绕过系统 HTTP 代理，避免 Clash/TUN 影响局域网直连。
- Codex 默认 `workspace-write + never`：只允许工作区写入，且不会在无人值守的 relay 里卡住等待审批。需要更宽权限时通过 `.env` 显式修改。
- 不要把 `.env`、Codex 登录文件或模型缓存提交到 Git。

## 开发与测试

测试可用 pytest：

```powershell
$env:PYTHONPATH = "src"
python -m pytest
```

GitHub Actions 同时在 `windows-latest` 与 `macos-latest`、Python 3.11–3.13 上运行包括预览、确认、取消和重启恢复在内的同一组测试；macOS 任务还会检查所有 shell 脚本语法。两端共用相同的 HTTP 状态机、SenseVoice/S1-mini 处理和官方 Codex app-server 客户端，因此交互效果一致。由于当前开发机是 Windows，发布 macOS 版本前仍应在一台真实 Apple Silicon Mac 上完成 Codex 登录、局域网入站、CoreBluetooth 和休眠唤醒四项硬件验收。

## License

MIT。S1-mini 模型权重有独立许可证，本仓库不包含模型权重。
