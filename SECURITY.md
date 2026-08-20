# CodeRook 安全策略

CodeRook 会执行模型生成的文件、Git 和 shell 操作。即使启用了权限与沙箱，也应把模型、Prompt、
MCP server、Skill、Hook 和外部网页视为不受信任输入。

## 支持状态

| 版本 | 安全支持 |
|---|---|
| `main` | 接受修复，不保证稳定 API |
| `0.1.x` | Beta 前开发版本，尽力修复 |
| `<0.1` | 不支持 |

在发布评分卡达到 GO 之前，CodeRook 不宣称生产就绪或适用于多租户环境。

## 私下报告漏洞

优先使用仓库的
[GitHub Private Vulnerability Reporting](https://github.com/kyletser/coderook/security/advisories/new)。
如果该入口不可用，请创建一个不含利用细节、密钥、日志正文或个人数据的最小 Issue，请求维护者
建立私下沟通渠道。不要在公开 Issue 中发布可直接利用的 PoC。

报告请包含：

- 受影响版本或 commit；
- 操作系统、sandbox backend 与权限模式；
- 最小复现步骤和预期边界；
- 实际影响、所需前置条件和可能缓解方式；
- 已完成脱敏的诊断信息。

维护者确认后会在安全公告中协调修复、回归测试、受影响版本和披露时间。项目目前没有承诺固定
响应 SLA。

## 公开安全边界

- Linux/macOS 只有在能力探测显示真实后端可用且 `enforced=true` 时才宣称 OS 强制隔离。
- Windows 当前没有文件系统/网络强制 sandbox；AUTO_REVIEW shell 会明确降级到 ASK。
- 工作区边界不是 shell 的 OS 安全边界，ProcessSupervisor/Job Object 也只负责进程治理。
- 域名出站白名单没有可接受的强制后端时会 fail closed，不会静默扩大为全网访问。
- 第三方 MCP/Skill/Hook 具有其声明能力范围内的风险，只安装可信来源并审查自动批准规则。
- API key 应存放在凭据存储或环境变量中，不应写入项目 TOML、Prompt、trace 或 Issue。

完整资产、信任边界、攻击面、非目标和事件响应见[威胁模型](docs/reference/THREAT_MODEL.md)。
