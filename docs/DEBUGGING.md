# 排障手册（DEBUGGING）

> 目标：看到症状后，5 分钟内知道先看哪个文件、哪条日志、哪段调用链。
> 先读 `docs/ARCHITECTURE.md` 的模块表和线程模型，再使用本文。

## 1. 快速命令

在项目根目录执行：

```bash
# 语法检查
python -m compileall -q app tests main.py

# 全量单元测试（本项目使用 unittest，不使用 pytest）
python -m unittest discover -s tests -v

# 单个测试
python -m unittest tests.test_core.SafetyTests.test_critical_battery_warns_but_keeps_control -v

# 无显示器环境下的 UI 冒烟（Git Bash）
QT_QPA_PLATFORM=offscreen python -c "from PySide6.QtWidgets import QApplication; from app.ui.main_window import MainWindow; app=QApplication([]); w=MainWindow(); print('UI_OK'); w.close()"

# 查看媒体索引
python -c "import sqlite3; c=sqlite3.connect('media.db'); [print(r) for r in c.execute('select id,kind,path,size_bytes,duration_s,started_at,note from media order by id desc limit 10')]; c.close()"

# 列出今天的录像/照片
ls -la recordings/$(date +%Y-%m-%d) 2>/dev/null
```

环境基线：Python 3.12、Windows 11、PySide6、PyAV、OpenCV、NumPy；版本见 `requirements.lock.txt`。

## 2. 先收集证据

排障前固定收集这五项，避免“猜”：

1. **版本**：`app/config.py` 的 `APP_VERSION`；如果是 git 仓库，记录 `git rev-parse --short HEAD`。
2. **现象**：时间、操作步骤、期望、实际、是否可复现。
3. **日志**：界面“运行日志”页的全部内容（当前不落盘，见第 8 节）。
4. **状态**：界面诊断面板（链路、视频、接管、电池、`can_move`）。
5. **产物**：相关照片/视频路径、`media.db` 行、磁盘剩余空间。

## 3. 症状 → 模块索引

| 症状 | 首先看 | 关键证据 | 常见原因 |
|---|---|---|---|
| 完全连不上 | `app/link.py`：`_run/_try_connect` | 链路状态、SSID、本机 IP、日志“未连接热点/连接超时” | 没连热点、无人机未上电、防火墙 |
| 能连但没遥测 | `link._read_loop` → `backend._on_topic` → `telemetry.decode_telemetry` | “遥测超时”、16 字节校验 | 数据格式变化、丢包、解析异常 |
| 预览卡死/花屏 | `app/video.py`：`_udp_loop/_decode_loop`、`backend.open_stream` | 视频状态、UDP 包数、丢帧、解码失败数 | 端口 2224 被占用、关键帧丢失、解码器异常 |
| 预览有画面但延迟大 | `video.py` 队列/统计；`backend._on_frame` | `frames_dropped`、fps、Mbps | 解码跟不上、UI 回调阻塞、录像占用 CPU |
| 接管失败/超时 | `backend.start_takeover/_on_takeover_timeout` | “接管超时：未收到飞控确认”、接管状态 | 遥控器仍在控制、飞控未放行、链路问题 |
| 摇杆/键盘不动 | `safety.snapshot().can_move` → `backend.set_sticks` → `control.set_gate/set_sticks` | 接管状态、`armed/flying`、`auto_reason`、`conflict` | 未接管、未解锁、未飞行、断链/冲突门控 |
| 键盘长按只动一下 | `main_window.eventFilter` | `isAutoRepeat` 分支、`_keys_down` | 自动重复 KeyRelease 误删按键 |
| 低电时控制被锁 | `safety.update_telemetry/_recompute_battery/snapshot` | `battery_warning/critical/lockout`、`auto_reason` | 是否又出现 `auto_reason="battery"` |
| 低电后无法起飞 | `safety.py` 落地锁定 | `battery_lockout` | 换电/点“已更换电池”后应解除 |
| 录像没开始 | `recorder.start` | “磁盘空间不足”“无法创建录像文件”“没有可用画面” | 空间 < 1 GB、路径不可写、没有解码帧 |
| 录像中途停止 | `recorder._run/_finish_segment` | “录像编码失败”“录像收尾失败” | 编码异常、磁盘满、路径被移除 |
| 视频没水印 | `backend.watermark_enabled` → `recorder.start(timestamp_enabled)` → `watermark.draw_timestamp` | 水印开关、文件首帧 | 开关为关、录制开始时状态未传入 |
| 照片没水印 | `storage.snapshot(timestamp=)` | 设置 JSON、日志 | 开关为关、绘制异常被吞 |
| 录像文件名重复/覆盖 | `recorder._allocate_segment_path` | 文件名是否含毫秒、是否有 `_2` | 是否回退成秒级命名 |
| 设置不生效/重启丢失 | `settings.AppSettings` | `config/ui_settings.json` 内容 | 路径被改、旧字段缺失、写权限 |
| 录制中按钮/状态错乱 | `main_window._on_recording` | 信号 `sig_recording`、UI 文案 | 异常停止回调未触发、信号未连接 |
| 界面控件不置灰 | `app/ui/style.py` | QSS 是否有 `:disabled` | 自定义样式覆盖系统禁用色 |
| 控件提示不显示 | 对应控件 tooltip | 控件是否 disabled | 禁用控件的 tooltip 不可靠，改用可见文字 |

## 4. 关键调用链

### 4.1 键盘长按

```text
QApplication eventFilter
 → MainWindow.eventFilter
 → _keys_down.add/discard（只处理非 isAutoRepeat）
 → _keyboard_tick（50 ms）
 → Backend.set_sticks
 → SafetySnapshot.can_move
 → ControlFrameScheduler.set_sticks/set_gate
 → LinkManager.send_raw → TCP
```

### 4.2 录像水印

```text
chk_watermark.toggled
 → Backend.set_watermark_enabled
 → settings.save()
 → 录制开始时 Backend.start_recording
 → recorder.start(timestamp_enabled=watermark_enabled)
 → _run: draw_timestamp(frame, time.time())
 → libx264 → MP4
```

### 4.3 预览卡死

```text
video.state/stats（waiting/recovering/stalled/live）
 → btn_stream.clicked
 → Backend.open_stream
 → CLOSE_RT_STREAM → 50 ms → OPEN_RT_STREAM
 → video.set_active(True) → _reset_requested/_wait_keyframe
```

### 4.4 低电门控

```text
decode_telemetry
 → SafetyController.update_telemetry
 → _recompute_battery
 → snapshot.can_move/can_takeoff
 → Backend._sync_gate（inhibit = 未接管 或 auto_reason 或 conflict）
 → ControlFrameScheduler
```

## 5. 日志关键字 → 代码位置

在界面日志中搜索这些词，可直接定位：

| 关键字 | 位置 |
|---|---|
| `未连接热点` | `app/link.py:_run` |
| `连接超时` | `app/link.py:_run` / `backend._on_link_state` |
| `遥测超时` | `app/link.py:_read_loop` |
| `遥测超时，正在确认链路状态` | `app/safety.py:_warnings` |
| `接管超时` | `app/backend.py:_on_takeover_timeout` |
| `接管被拒绝` / `接管失败` | `app/backend.py:start_takeover/confirm_takeover/release_control` |
| `电机切换被拒绝` | `app/backend.py:_motor_ready` |
| `没有可用画面` | `app/backend.py:start_recording/snapshot` |
| `无法创建录像文件` | `app/recorder.py:_open_segment` |
| `录像编码失败` | `app/recorder.py:_run` |
| `录像收尾失败` | `app/recorder.py:_finish_segment` |
| `磁盘空间不足` | `app/recorder.py:start` |
| `视频端口异常` | `app/video.py:_udp_loop` |
| `H.264 解码失败` | `app/video.py:_decode_loop` |
| `目录不可写` | `app/settings.py:prepare_media_root` |
| `保存照片失败` | `app/backend.py:snapshot` |
| `保存水印设置失败` | `app/backend.py:set_watermark_enabled` |

## 6. 测试与功能对照

| 测试类 | 保护的行为 |
|---|---|
| `ProtocolTests` | CTP 分片/校验、遥测解码、控制帧位、ACK、调度器禁用、摇杆映射 |
| `StorageTests` | 媒体目录、照片水印开关 |
| `RecorderTests` | 毫秒命名、O_EXCL 唯一分配、同秒连续录制不覆盖 |
| `WatermarkTests` | 本地时间格式、右上角绘制、关闭时不修改原帧 |
| `SettingsTests` | 水印开关持久化 |
| `SafetyTests` | 断链缓降、低电仅告警、重连不自动接管、冲突、断开清理 |
| `LinkTests` | 连接超时、无热点、SSID 判定、手动断开、假服务器重连 |

修改任何核心行为时，先找到对应测试；没有测试就先写一个失败测试。

## 7. 复现与定位流程

1. **分类**：UI / 链路 / 遥测 / 视频 / 控制 / 安全 / 录制 / 存储 / 设置。
2. **查证据**：按第 2 节收集五项。
3. **定位入口**：用第 3、4 节找到第一条可疑调用链。
4. **缩小范围**：
   - 有单测覆盖 → 先跑单测；
   - 无单测 → 用 offscreen UI 或假 CTP 服务器复现；
   - 线程问题 → 看日志顺序、状态快照、是否跨线程操作 UI。
5. **写失败测试**（能自动化就自动化）。
6. **小步修复**，不要顺手重构安全逻辑。
7. **回归**：`compileall` + 全量 29 个测试 + 相关人工验证。

## 8. 当前日志限制（重要）

`Backend._log()` 只发送 `sig_log` 到界面，**没有写入文件**；`config.LOGS_DIR` 目前未被使用。

影响：

- 程序崩溃后日志消失；
- 远程/无界面排障看不到历史；
- 只能靠复现。

临时办法：出问题时立即复制“运行日志”页内容，连同版本、时间、操作步骤一起保存。

建议改进（尚未实施）：日志同时写 `logs/gcs_YYYYMMDD.log`，带时间、级别、线程名；增加“导出诊断包”按钮。

## 9. Bug 报告模板

```text
版本：v0.2.0 / commit xxxxxxx
环境：Windows 11 / Python 3.12.9 / PySide6 6.12.0 / av 19.0.2
时间：2026-11-27 14:30
操作步骤：
  1. ...
  2. ...
期望：
实际：
复现频率：必现 / 偶发（x/y）
日志：（粘贴运行日志页）
状态：链路 / 视频 / 接管 / 电池 / can_move
产物：照片或录像路径、media.db 行
备注：是否卸桨、是否真机、是否多开
```

## 10. 相关文档

- `README.md`：运行、操作、安全策略
- `docs/ARCHITECTURE.md`：模块、数据流、线程、状态机
- `AGENTS.md`：智能体操作规则
- `CHANGELOG.md`：版本变化
