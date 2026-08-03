# 🧬 Self-Evolving Quant Agent System

> 生产级 · 真实资金运行 · 数据驱动进化的 AI Agent 架构

[![Python](https://img.shields.io/badge/Python-3.13-3572A5?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![AI Agent](https://img.shields.io/badge/AI-Agent-22d3ee.svg)](#)
[![Quant Trading](https://img.shields.io/badge/Quant-Trading-34d399.svg)](#)
[![Multi-Agent](https://img.shields.io/badge/Multi-Agent-a78bfa.svg)](#)
[![Memory System](https://img.shields.io/badge/Memory-System-fbbf24.svg)](#)
[![Self-Evolving](https://img.shields.io/badge/Self-Evolving-fb7185.svg)](#)
[![GitHub stars](https://img.shields.io/github/stars/Honglouqaz/self-evolving-quant-agent.svg?style=social)](https://github.com/Honglouqaz/self-evolving-quant-agent)

一套**自进化的多智能体交易系统**，以三大量化交易子系统为业务载体，构建了"采集 → 分析 → 决策 → 执行 → 复盘 → 进化"的完整闭环。系统从静态规则出发，通过**决策记忆 + 监督层 + 参数自适应**逐步实现数据驱动进化。

![架构](docs/architecture.html)

## ✨ 核心特性

| 特性 | 说明 |
|:----|:----|
| 🎯 **三维市场监督层** | 市场阶段 × 资金炒作节奏 × 情绪周期 → 多系统协同指导 |
| 🧠 **决策记忆闭环** | 平仓反思 → 历史注入 → 仓位自适应（借鉴 TradingAgents 设计） |
| 📈 **4h 周期分析** | 加密货币市场特性适配：节奏快+无涨跌幅限制 → 4h 最小合理复盘周期 |
| 🔄 **自进化执行层** | 每日生态扫描 → 小优化自动 / 大优化人工决策 |
| 🛡️ **生产级保障** | 四层健康体系（进程/cron/数据源/市场）+ 降级安全 + 备份回滚 |

## 📦 模块架构

```
┌─────────────────────────────────────────────────┐
│                   进化层 Evolve                   │
│  self_evolve_collect.py (生态扫描 → 分类决策)     │
└──────────────────┬──────────────────────────────┘
                   ↓ 优化回写
┌─────────────────────────────────────────────────┐
│                   监督层 Supervise                │
│  market_supervisor.py → directive.json           │
│  (阶段×资金×情绪 → 方向/仓位/频率)                 │
└───────┬──────────────────────┬──────────────────┘
        ↓ directive            ↓ 记忆注入
┌─────────────────────────────────────────────────┐
│                   业务载体 (三系统)                │
│  K1(15m主流) · S1(5m山寨) · T1(15m TradFi)       │
└───────┬──────────────────────┬──────────────────┘
        ↓ 平仓记录              ↓ 成交反馈
┌─────────────────────────────────────────────────┐
│  🧠 决策记忆        │    📈 复盘层                │
│  decision_reflector│  profit_effect_analyzer    │
│  记录→反思→注入     │  4h赚钱效应→覆盖率→捕获率   │
└─────────────────────────────────────────────────┘
```

## 🗂️ 项目结构

```
self_evolving_agent/
├── core/                          # 核心模块 (全部脱敏)
│   ├── decision_reflector.py      # 决策记忆闭环: 记录→反思→注入→参数自适应
│   ├── market_supervisor.py       # 三维监督层: 生成 directive.json
│   ├── supervisor_client.py       # 三系统统一读取监督指令
│   ├── market_4h.py               # 4h 周期市场数据 (加密市场特性)
│   ├── profit_effect_analyzer.py  # 赚钱效应复盘: 覆盖率/利润捕获
│   ├── self_evolve_collect.py     # 生态扫描: GitHub/HN 动态采集
│   ├── cron_health_check.py       # Cron 任务健康检查
│   └── data_source_health.py      # 数据源健康监控与切换
└── docs/
    ├── WHITEPAPER.md              # 架构白皮书
    └── architecture.html          # 架构图
```

## 🧠 决策记忆闭环（核心创新）

```
平仓 → record_close() 记录盈亏+退出原因
  → 规则引擎归因反思 (入场失败/趋势正确/追高失败/震荡粘滞)
  → 下次开仓 get_symbol_adjustment() 查询同标的近期表现
  → 胜率<40% → 降仓×0.7 | 连亏≥2 → 降仓×0.7 | 胜率≥70% → 加仓×1.15
  → 参数自适应: 平均亏损>止损×1.5 → 自动放宽止损
```

- **零 LLM 依赖**：纯规则引擎，确定性、可审计
- **数据校验**：pnl_pct 超 ±200% 自动丢弃（防污染）
- **增量学习**：样本≥3 笔才生效，防噪声

## 📈 监督层（Meta-Supervisor）

三维输入 → 统一指令：

| 维度 | 数据源 | 档位 |
|:----|:------|:----|
| 市场阶段 | phase_current.json | launch/main_up/bottom_building(强) / top_building/retreat(弱) |
| 资金节奏 | mainstream_flow + sector_heat | inflow / neutral / outflow |
| 情绪周期 | market_sentiment | safe / cautious_safe / caution / danger |

**镜像方向设计**：强势市场做空=逆势微仓；弱势市场做空=顺势正常仓；极强多头逼空 → 跳过开空。

**自适应调优**：基于决策记忆胜率动态微调——胜率<40% 收紧 ×0.85，≥70% 放宽 ×1.1。

## 🚀 快速开始

```bash
# 1. 配置环境
export BINANCE_PROXY="https://your-binance-proxy.example.com"
export PHASE_FILE="./data/phase_current.json"

# 2. 运行监督层 (每15分钟)
python3 core/market_supervisor.py

# 3. 运行赚钱效应复盘 (每日)
python3 core/profit_effect_analyzer.py --top 15

# 4. 生态扫描 (每日)
python3 core/self_evolve_collect.py --days 7

# 5. 健康检查
python3 core/cron_health_check.py
python3 core/data_source_health.py --json
```

## 📊 可观测性

- **DataHub 看板**：决策记忆可视化 `/memory`（记录→反思→注入闭环演示）
- **架构图**：`/architecture`（SVG 暗色架构图）
- **四层健康**：进程自愈(30s) / cron健康(6h) / 数据源监控(5min) / 市场监控(15min)

## 🛡️ 设计哲学

1. **规则 + Agent 混合**：确定性系统与 LLM 分析融合，量价只做验证不做预测
2. **先保本再增值**：所有自进化以风险可控为前提（降级安全、大优化人工决策）
3. **数据驱动进化**：静态规则 → 决策记忆 → 胜率自适应，逐步升级
4. **务实增量**：先核心后完善，够好就行

## ⚠️ 免责声明

本项目仅用于技术研究。涉及真实资金的操作需自行承担风险。代码中所有密钥、内部域名、真实路径均已脱敏替换为占位符。

## 📄 License

MIT

---

*由生产环境真实运行系统脱敏整理 · 2026-08-03*
