# 架构说明（ARCHITECTURE）

> 本文回答三个问题：程序整体怎么工作、每个模块负责什么、哪些约束不能破坏。
> 配合 `README.md`（操作手册）、`docs/DEBUGGING.md`（排障索引）、`AGENTS.md`（智能体守则）使用。

## 1. 程序定位

Windows 端无人机地面站（APEX G149 / GD240）：

- 通过无人机 Wi-Fi 热点建立 TCP/CTP 链路，接收遥测和视频；
- **默认只读**，不发送控制帧；必须人工点击“接管控制”并二次确认后才可能发送；
- 支持摇杆/键盘控制、一键起飞/降落、解锁/加锁电机、紧急停桨；
- 本地拍照、录像（H.264 MP4）、时间戳水印、媒体索引；
- 对断链、低电、控制冲突做状态机判断，只做告警和门控，不替代飞控。

## 2. 系统上下文

```text
无人机（APEX G149 / GD240）
   │  Wi-Fi 热点（SSID 前缀 Tspeed7 / Tspeed / AT66）
   │
   ├── TCP 192.168.1.1:3333 ── CTP 帧（遥测、控制、ACK、状态）
   │
   └── UDP 0.0.0.0:2224 ───── 视频分片（H.264 / JPEG）
```

程序不负责切换 Windows Wi-Fi，也不修改防火墙。

## 3. 核心数据流

### 3.1 遥测

```text
socket recv
 → CTPParser.feed()
 → Backend._on_topic("GENERIC_CMD")
 → decode_telemetry()            # 16 字节、0xCC/0x33/0xAA 帧
 → SafetyController.update_telemetry()
 → Backend._sync_gate()          # 更新控制帧门控
 → sig_telemetry / sig_safety    # 跨线程发到 UI
```

`BAT_STATUS`（电量格）、`SD_STATUS`、`AP_SSID_INFO`、`APP_ACCESS` 也由 `_on_topic` 处理。

### 3.2 视频

```text
UDP :2224
 → iter_jl_chunks()              # 一包可能含多个分片
 → FrameReassembler.push_packet()
 → queue（maxsize=12）
 → video-decode 线程
 → H.264（PyAV）或 JPEG（OpenCV）解码
 → BGR ndarray
 → VideoPipeline._publish()
 → sig_frame → VideoWidget 预览
```

录像开启时，`Backend._on_frame` 还会把最新帧交给 `Recorder.submit()`。

### 3.3 控制（从 UI 到无人机）

```text
摇杆 / 键盘
 → MainWindow._sticks_changed / _keyboard_tick
 → Backend.set_sticks()
 → SafetyController.snapshot().can_move
 → ControlFrameScheduler.set_sticks()
 → control-tx 线程（每 25 ms，约 40 Hz）
 → LinkManager.send_raw()
 → TCP CTP GENERIC_CMD
```

控制帧要同时满足：

1. `scheduler.enabled` 为真（已接管流程）；
2. `scheduler` 的 `inhibit` 为假（`Backend._sync_gate`）；
3. `armed and flying` 为真（否则摇杆值被清零）；
4. `SafetySnapshot.can_move` 为真（UI 层还会再检查一次）。

### 3.4 录像

```text
Backend.start_recording()
 → Recorder.start(timestamp_enabled=当前水印开关)
 → 每帧 Recorder.submit(frame)  # 入队时记录 time.time()
 → mp4-recorder 线程
 → 可选 draw_timestamp(frame, 帧到达时间)
 → PyAV libx264（8 Mbps）
 → 2 GB 分段 MP4
 → MediaIndex.add/finish（media.db）
```

异常停止时 `Recorder._on_stopped` → `Backend.sig_recording(False, 错误)` → UI 自动恢复。

### 3.5 拍照 / 设置

```text
拍照：最新解码帧 → draw_timestamp（按开关）→ JPEG + MediaIndex
设置：UI → Backend → AppSettings.save() → config/ui_settings.json
```

## 4. 模块职责

| 文件 | 职责 | 运行位置 | 关键入口 | 风险 |
|---|---|---|---|---|
| `main.py` | 创建 QApplication、加载主窗口 | Qt 主线程 | `main()` | 低 |
| `app/config.py` | 常量、默认安全配置、合并 `safety_profile.json` | 导入时 | `PROFILE = load_profile()` | 中 |
| `app/settings.py` | 存储目录、水印开关的读写与校验 | UI 调用 | `AppSettings.load/save` | 低 |
| `app/link.py` | 热点检测、TCP 连接、重连、CTP 收发 | `link-tcp` 线程 | `start/connect/disconnect/send_ctp` | **高** |
| `app/protocol/ctp.py` | CTP 帧封装、分片、校验 | 链路线程 | `build_ctp/CTPParser` | **高** |
| `app/protocol/telemetry.py` | 16 字节遥测解码、bit 语义、派生属性 | 链路回调 | `decode_telemetry` | **高** |
| `app/control.py` | 40 Hz 控制帧调度、门控、脉冲、摇杆映射 | `control-tx` 线程 | `set_gate/set_sticks/pulse_*` | **高** |
| `app/video.py` | UDP 收包、分片重组、H.264/JPEG 解码、统计 | `video-udp` + `video-decode` | `set_active/reset` | 中 |
| `app/safety.py` | 安全状态机、低电/断链/冲突判断、`can_*` | 链路/UI 调用，内部加锁 | `update_telemetry/snapshot` | **极高** |
| `app/backend.py` | 总编排、信号总线、接管/电机/录制/拍照 | Qt 主线程 + 回调 | 各类 `start_*/set_*` | **高** |
| `app/recorder.py` | 帧队列、水印、编码、分段、异常回调 | `mp4-recorder` 线程 | `start/submit/stop` | 中 |
| `app/storage.py` | 媒体目录、`media.db`、照片保存 | UI/录制线程 | `snapshot/MediaIndex` | 中 |
| `app/watermark.py` | 时间戳格式化与绘制 | 编码/保存线程 | `draw_timestamp` | 低 |
| `app/ui/main_window.py` | 界面组装、定时器、键盘、信号连接 | Qt 主线程 | `MainWindow` | 中 |
| `app/ui/widgets.py` | 视频控件、摇杆、REC 徽标 | Qt 主线程 | `VideoWidget/Joystick` | 低 |
| `app/ui/style.py` | QSS 主题（含禁用态） | 导入时 | `APP_QSS` | 低 |
| `tests/test_core.py` | 单元测试 + 假 CTP 服务器 | 测试进程 | 29 个用例 | — |

## 5. 线程模型

| 线程/定时器 | 所属 | 职责 | 注意事项 |
|---|---|---|---|
| Qt 主线程 | `MainWindow` / `Backend` | UI、信号槽、`Backend` 100 ms tick、接管超时定时器 | 不能执行阻塞 socket/编码 |
| `link-tcp` | `LinkManager._run` | 连接、重连、读循环、keepalive | 回调进入 `Backend`，再通过信号回 UI |
| `video-udp` | `VideoPipeline._udp_loop` | 绑定 UDP 2224、重组分片 | 只读 `_active` 标志 |
| `video-decode` | `VideoPipeline._decode_loop` | 解码、发布帧、统计 | 不直接操作 QWidget |
| `control-tx` | `ControlFrameScheduler._loop` | 40 Hz 发送控制帧 | 所有状态由 `RLock` 保护 |
| `mp4-recorder` | `Recorder._run` | 队列取帧、水印、编码、分段 | 异常通过回调上报 |
| `_clock` | QTimer 1000 ms | 顶部时钟 | UI 线程 |
| `_kb_timer` | QTimer 50 ms | 键盘摇杆平滑逼近 | UI 线程 |
| `_rec_timer` | QTimer 500 ms | REC 时长/红点闪烁 | UI 线程 |
| `Backend._timer` | QTimer 100 ms | 周期性安全/门控刷新 | UI 线程 |
| `_takeover_timer` | QTimer 单次 2 s | 接管确认超时 | UI 线程 |

**线程边界规则**：链路、视频、录像线程不得直接操作 QWidget；只通过 `Backend` 的 Qt 信号回主线程。共享状态用各自锁保护，UI 侧只读快照。

## 6. 状态机

### 6.1 链路（`LinkManager` / `Backend`）

`disconnected → connecting → connected`
异常分支：`no_hotspot`、`connect_timeout`、`link_lost`、`reconnecting`
重连后：`reconnected_observing`（只观察，不自动恢复控制）

关键常量（`app/config.py`）：

- keepalive 1.5 s；遥测空闲超时 2 s；
- 断链提前提示 3 s，预计自动缓降 5 s；
- 首次连接总窗口 20 s；重连退避 0.5–2 s。

### 6.2 接管（`Backend` + UI）

`idle → pending → acked → confirmed`
分支：`timeout`、`released`

- `pending`：已开始发控制帧，等待飞控 `status=1`；
- `acked`：收到飞控确认，弹出人工确认框；
- `confirmed`：人工确认控制权；
- 任何失败/交还：停止控制帧，必要时先加锁，再回到只读。

**语义**：`status=1` 只表示飞控收到控制帧，不等于真正取得控制权。

### 6.3 安全（`SafetyController`）

- 正常；
- 低电告警（`battery_warning`）：仅提示；
- 电池临界（`battery_critical`）：**仅告警，不锁飞行控制**；
- 落地锁定（`battery_lockout`）：落地后禁止起飞，换电或手动解除；
- 断链自动缓降（`auto_reason="link"`）：抑制控制帧，避免和飞控竞争；
- 连接冲突（`conflict`）：阻止接管和起飞。

`auto_reason="battery"` 已不再产生，低电不会自动锁控制（方案 A）。

### 6.4 录制（`Recorder`）

`idle → recording → (2 GB) 分段 → ... → stopped`
异常：编码失败 → `_active=False` → `_on_stopped` → UI 恢复。

同一秒内重复开始录制时，文件名用毫秒 + `O_EXCL` 原子占位，冲突追加 `_2`，绝不覆盖旧文件。

## 7. 不可破坏的安全不变量

1. **默认只读**：未完成接管流程前不发控制帧。
2. `can_move = armed and flying and auto_reason is None and not conflict and link_state in (connected, reconnected_observing)`。
3. 低电**只警告**；临界时禁止起飞，但飞行中不锁控制。
4. 断链自动缓降期间抑制控制帧；不要在此期间反复发一键降落脉冲。
5. 键盘摇杆：接管确认 + 电机解锁才自动开启；实际输出仍受 `can_move` 限制。
6. 水印使用**本地墙上时钟**；录制计时使用 `time.monotonic()`。
7. 录像文件名毫秒 + `O_EXCL`；照片文件名毫秒。
8. 剩余空间 < 1 GB 拒绝录制；< 5 GB 告警。
9. 录制中禁止切换存储目录、禁止切换水印开关。
10. 链路/视频/录像线程不直接操作 UI。

## 8. 配置与持久化

| 文件 | 内容 | 谁写 |
|---|---|---|
| `app/config.py` | 协议/端口/阈值/默认配置 | 开发者 |
| `config/safety_profile.json` | 可调整的安全阈值（与默认值合并） | 用户/开发者 |
| `config/ui_settings.json` | 存储目录、时间戳水印开关 | 程序（已被 `.gitignore` 排除） |
| `media.db` | 媒体索引（SQLite `media` 表） | 程序（已被排除） |
| `recordings/` | 照片/录像 | 程序（已被排除） |
| `logs/` | 预留日志目录（当前只写 UI 日志，未落盘） | — |

`media.db` 表结构：

```sql
CREATE TABLE media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- photo / video
    path TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    duration_s REAL,
    size_bytes INTEGER,
    note TEXT
);
```

## 9. 扩展指南

### 新增遥测字段

1. `app/protocol/telemetry.py`：解析 raw 字节 → 字段；
2. `app/safety.py`（如参与安全判断）→ `SafetySnapshot`；
3. `app/ui/main_window.py`：显示/诊断；
4. `tests/test_core.py`：构造 payload 断言解码。

### 新增 UI 控件

1. `main_window._build_*` 创建控件；
2. 需要跨线程更新时，在 `Backend` 增加 `Signal`，UI 连接；
3. 样式加在 `app/ui/style.py`，记得补 `:disabled`；
4. 不要在视频/链路线程直接改控件。

### 新增录制逻辑

1. 优先改 `app/recorder.py`，不要阻塞 `submit()`；
2. 时间语义明确：到达时间用 `time.time()`，计时用 `monotonic()`；
3. 失败路径必须关闭容器并回调 `_on_stopped`；
4. 文件命名必须唯一，禁止直接 `av.open(..., mode="w")` 覆盖已有文件。

## 10. 已知限制

- 日志只发 UI，不落盘；程序崩溃后没有文件日志。
- 接管是否真正取得控制权取决于飞控仲裁，`status=1` 语义仍需实验确认。
- 紧急停桨、空中接管等未完成真机验证。
- 录像为解码后重编码，不是原始码流封装，CPU 占用较高。
- 2 GB 分段与时间戳连续性未做长时间实飞验证。
- 当前没有单实例锁；多开时文件名不会冲突，但会共享 `media.db`。

## 11. 文档索引

- 操作与运行：`README.md`
- 交接/排障：`docs/DEBUGGING.md`
- 智能体规则：`AGENTS.md`
- 版本记录：`CHANGELOG.md`
- 依赖锁定：`requirements.lock.txt`
