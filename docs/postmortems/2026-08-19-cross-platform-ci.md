# 复盘：三平台 CI #31 失败

- **日期**：2026-08-19
- **类型**：质量门禁失败，无用户数据或公开发行受影响
- **状态**：本地修复已验证，远端三平台复验仍待新 run

## 摘要与影响

本地完整测试通过后，远端 CI #31 仍在 Ubuntu/macOS 各失败 3 项、Windows 失败 1 项。失败阻断了
“三平台 CI 全绿”与公开 Beta；因为 release scorecard 保持 NO-GO，没有 tag、镜像或安装包被错误发布。

## 暴露的问题

失败不是单一业务逻辑错误，而是三类环境假设叠加：

1. POSIX shell 测试与命令包装对引号/转义和具体 shell 行为作了本机化假设；
2. sandbox 单测把“宿主机是否真的安装/允许 bwrap 或 Seatbelt”与“planner 合同”耦合，导致测试环境
   能力变化影响本应确定的断言；
3. Windows 子进程输出假定 UTF-8，而原生命令可能使用 OEM code page，诊断路径因此产生平台差异。

根因是把跨平台支持理解为“同一套 Python 测试能在本机通过”，而没有把 shell、能力探测和编码当成
显式输入。测试 fixture 对宿主环境隔离不足，错误分类也没有在第一次失败时把三类假设分别标出。

## 纠正措施

- POSIX 路径使用可复现的 argv/quote 合同，测试不依赖交互 shell 启动状态；
- planner/sandbox 单测注入能力对象，真实 bwrap/Seatbelt 边界留给专用负例脚本与平台 CI；
- 子进程输出统一采用 UTF-8 优先、Windows OEM 回退和 `errors="replace"` 的脱敏诊断策略；
- 把 Windows 本机 Mypy 与 `mypy --platform linux` 同时列为推送前硬门禁；
- 评分卡同时记录“本地已修”和“远端待复验”，不把候选修复写成远端成功。

相关实现集中在 `src/code_rook/core/persistent_shell.py`、`core/sandbox/`、`core/processes.py` 及其单测；
生产就绪批次为 commit `1a93b3b`。

## 验证与未解决项

修复后的本地 unit/full gate 已通过，仓库也新增稳定 `Required CI gate` 汇总三平台矩阵。但复盘的完成
证据必须包含新远端 run URL；在那之前，`docs/RELEASE_SCORECARD.md` 继续写“修复待远端复验”。

尚未解决的产品限制也不能与本次测试修复混淆：Windows 仍没有受支持的文件系统/网络强制 sandbox，
正确行为是 degraded + ASK；Linux/macOS 真实安全负例还需要远端 runner 产出报告。

## 防复发规则

- 平台能力必须通过依赖注入进入单测，真实能力只在标记清楚的平台测试中探测；
- 子进程边界必须显式声明 shell、argv、cwd、encoding、timeout 和进程树终止语义；
- 本地通过只记为本地证据，远端矩阵、干净机和公开产物分别登记；
- 任一 required gate 失败都阻断发布，不能手工把评分卡改为 GO 绕过。
