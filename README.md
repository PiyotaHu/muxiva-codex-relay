# muxiva-codex-relay

让同一局域网里的 ESP32 使用电脑上的 Codex，而不要求 ESP32 直接访问 Codex 云端。

## 它解决什么问题

```text
ESP32 麦克风
  -> 现有 Xiaozhi ASR
  -> ESP32 拦截本轮 STT 文本
  -> 本机 relay
       -> 英文：S1-mini by Superwhisper 清洗
       -> 中文：保留 ASR 原文（S1-mini v1 不支持中文）
  -> Codex app-server
  -> Codex 会话状态
  -> ESP32 Codex 页面
```

relay 使用 Codex CLI 自带的本机 `app-server` JSONL 协议来列出、恢复和启动任务，不写 Codex 的 SQLite 数据库，也不模拟桌面 UI。默认恢复最近一个空闲会话；找不到可恢复会话时创建新会话。

电脑端 relay 与 S1-mini 启动脚本都带异常退出后的自动重启循环；安装自启动后，它们会在用户登录 Windows 时自动运行。

## 快速开始（Windows）

1. 复制 `.env.example` 为 `.env`，至少设置 relay token、ESP32 Hub 地址和 Codex 工作目录。token 必须和固件 `secret_config.h` 中的 `HUB_AUTH_TOKEN` 相同。
2. 安装 S1-mini 的本机运行时：

   ```powershell
   .\scripts\install-llama-cpp.ps1
   .\scripts\start-s1-mini.ps1
   ```

3. 启动 relay：

   ```powershell
   .\scripts\start-relay.ps1
   ```

4. 在 Windows 防火墙中仅允许 TCP 8765 的“专用网络”入站连接。
5. ESP32 切到 Codex 页，按 BOOT，说完任务后等待屏幕显示“任务已提交”。

登录自启动：

```powershell
.\scripts\install-autostart.ps1
```

管理员环境会安装带重启策略的计划任务；普通用户环境会自动退回当前用户的登录启动项。两种方式都使用脚本内置的进程守护。

健康检查：`GET http://127.0.0.1:8765/health`。状态接口和提交接口都使用 Bearer token；提交接口是 `POST /v1/transcripts`。

## 按键约定

- KEY 单击：天气 → 音乐 → Codex → 天气。
- Codex 页按 BOOT：开始一次语音任务。ASR 返回后自动停止普通对话链路，避免小智和 Codex 同时回答。
- 其他页面按 BOOT：保持原来的小智语音对话行为。

## S1-mini 的边界

`S1-mini by Superwhisper` 是 ASR 文本清洗模型，不是语音识别模型。v1 只覆盖英文，因此 relay 只把英文原始转写交给它；中文直接使用现有 ASR 结果。模型服务不可用时也会自动退回 ASR 原文，提交任务不会因此中断。

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

项目运行时只用 Python 标准库。测试可用 pytest：

```powershell
$env:PYTHONPATH = "src"
python -m pytest
```

## License

MIT。S1-mini 模型权重有独立许可证，本仓库不包含模型权重。
