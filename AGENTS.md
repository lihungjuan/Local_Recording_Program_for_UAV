# AGENTS.md — 智能体操作手册

本文件面向接手本项目的 AI 智能体或开发者。开始改代码前，按顺序阅读：

1. `README.md`（运行与操作）
2. `docs/ARCHITECTURE.md`（模块、数据流、线程、状态机）
3. `docs/DEBUGGING.md`（症状 → 文件 → 日志）

## 项目是什么

Windows 端无人机地面站（APEX G149 / GD240）：通过无人机 Wi-Fi 热点走 TCP/CTP 接收遥测、UDP 接收视频；**默认只读**，人工接管后才可能发送控制帧；支持摇杆/键盘控制、解锁/加锁、起降、拍照、录像、时间戳水印。

## 环境与命令

- Windows 11 + Python 3.12
- 依赖：`PySide6-Essentials`、`av`、`opencv-python`、`numpy`（精确版本见 `requirements.lock.txt`）
- 安装：`install.bat` 或 `python -m pip install -r requirements.txt`
- 运行：`python main.py` 或 `run.bat`

**验证命令（每次改代码后至少执行前两条）：**

```bash
python -m compileall -q app tests main.py
python -m unittest discover -s tests -v

# UI 冒烟（无界面环境）
QT_QPA_PLATFORM=offscreen python -c "from PySide6.QtWidgets import QApplication; from app.ui.main_window import MainWindow; app=QApplication([]); w=MainWindow(); print('UI_OK'); w.close()"
```

注意：本项目使用 `unittest`，**没有安装 pytest**，不要用 `pytest` 命令。

## 安全红线（不可自行放宽）

1. 未完成“接管控制 + 人工确认”前，不得发送控制帧。
2. 不得修改 `app/safety.py`、`app/control.py` 的门控条件来“让功能更容易用”，除非任务明确要求并说明风险。
3. 低电策略是**只告警、不锁飞行控制**；不要把 `auto_reason="battery"` 加回来。
4. 不得在链路/视频/录像线程直接操作 QWidget；跨线程只走 `Backend` 的 Qt 信号。
5. 不得删除或弱化“飞行中禁止加锁”“断链抑制控制帧”等保护。
6. 真机操作默认要求卸桨；不要建议在未验证状态下起飞。

## 当前仓库状态

- 这是一个**本地 git 仓库**，分支 `main`，已提交基线并打标签 `v0.2.0`。
- **是否推送到 GitHub 由项目所有者决定**；智能体不要自行 `git push`、不要添加远程、不要修改仓库可见性。
- 远程地址（所有者自行添加）：`https://github.com/lihungjuan/Local_Recording_Program_for_UAV.git`

## 禁止提交的内容

`.gitignore` 已排除：

- `recordings/`（真实照片/录像）
- `media.db`（媒体索引）
- `logs/`
- `config/ui_settings.json`（个人存储路径/开关）
- `__pycache__/`、`*.pyc`
- `video_review/`（测试截图，9.4 MB）

不要把测试录像、个人路径、token、密码提交进仓库。

## 代码约定

- 保留现有中文 UI 文案和安全提示；不要擅自改措辞。
- 新增公共函数/类加类型注解；核心模块顶部保持简短职责说明。
- 日志使用 `self._log(level, message)`，级别：`INFO/WARN/ERROR`。
- 新增 Qt 控件样式写在 `app/ui/style.py`；自定义 QSS 时必须补 `:disabled`，否则禁用态不会变灰。
- 不要在禁用控件上只依赖 tooltip 做关键提示；用可见文字或独立标签。
- 时间语义：
  - 水印/文件名使用本地墙上时钟；
  - 录制时长/超时使用 `time.monotonic()`；
  - 录像帧的时间戳取“入队到达时间”，不是录制开始时间。
- 录像文件名必须用 `_allocate_segment_path()`（毫秒 + `O_EXCL`），禁止直接拼秒级文件名覆盖旧文件。

## 改动流程

1. 先读相关模块与对应测试。
2. 能写测试就先写失败测试；至少确认现有 29 个测试全绿。
3. 小步修改，不顺手重构安全逻辑。
4. 运行 `compileall` + 全量单测 + 相关 UI/离屏验证。
5. 在提交说明中写清：改了什么、为什么、怎么验证、影响哪些功能。
6. 涉及安全行为、实机操作、文件格式的变化，必须明确告知项目所有者。

## “完成”的定义

- `python -m compileall -q app tests main.py` 通过；
- `python -m unittest discover -s tests -v` 全部通过（当前 29 个）；
- 新增行为有测试或明确的人工验证步骤；
- 文档（README / ARCHITECTURE / DEBUGGING / CHANGELOG）同步更新；
- 没有把运行时产物提交进 git。

## 已知坑

1. **QSS 覆盖禁用态**：自定义颜色后，`setEnabled(False)` 不会自动变灰，必须写 `:disabled`。
2. **禁用控件 tooltip 不可靠**：Windows/Qt 下可能不弹；关键提示用可见文字。
3. **日志不落盘**：`Backend._log()` 只发 UI；崩溃后无文件日志。排障先复制界面日志。
4. **水印与计时时钟不同**：水印是墙钟，计时是 monotonic；系统时间调整时两者会分开变化。
5. **同秒重复录制**：靠毫秒 + `O_EXCL` 保证唯一，冲突追加 `_2`。
6. **视频与链路是两条通道**：预览卡死不一定链路断；先看视频状态，再试“重开视频”。
7. **`status=1` 不等于控制权**：只是飞控收到控制帧，仍需人工确认。
8. **主窗口较大**（`main_window.py` 约 1400 行）：改 UI 前先用 `grep` 找对应构建函数和信号连接。

## 第一次接手建议做的自检

1. 找到 `draw_timestamp()` 的调用链，说明照片/录像水印各在何时绘制。
2. 说明控制帧从键盘到 TCP 要经过哪些门控。
3. 找到录像文件名唯一性实现，说明同秒连续录制的行为。
4. 跑通 29 个单元测试和一次 offscreen UI 冒烟。
5. 按 `docs/DEBUGGING.md` 的症状表，任选 3 个症状说出“先看哪个文件”。

## 文档索引

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/DEBUGGING.md`
- `CHANGELOG.md`
- `requirements.lock.txt`
