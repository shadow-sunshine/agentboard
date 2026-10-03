# 多 Agent 编排报告

- run_id: `ma_ccab32b3023b1c7e`
- 结局: **completed**
- 决策机制: `orchestrator.v1:cli.v1:structured-plan`
- 验证结论: {'verify_report': 'passed'}

## 最终报告（仅汇总验证通过的结论）

- final_report_id: `mafr_d695c4dcdd76545b`

最终报告（仅汇总验证通过的结论）：verify_report → rp_4b666803a174656e (passed)

## 完整研究报告 1

# 研究报告

- report_id: `rp_4b666803a174656e`
- 目标: 基于显式来源计划研究候选方案
- 来源: `orchestrator/research_candidates`

## 推荐（打分排名）

| 排名 | 候选 | 得分 | 置信依据 |
|---|---|---|---|
| 1 | pgvector | 0.5000 | pgvector：综合效用 0.500（排名 1）；达标维度 5/6；可信证据支撑 5 条；缺失维度：query_latency |
| 2 | qdrant_local | 0.4500 | qdrant_local：综合效用 0.450（排名 2）；达标维度 4/6；可信证据支撑 4 条；缺失维度：query_latency, scale_ceiling |

## 完整性校验

| 检查 | 结果 | 说明 |
|---|---|---|
| I1 | ✅ 通过 | 2 个 evidence_id 各自对应唯一内容 |
| I2 | ✅ 通过 | 2 条索引项全部可解析 |
| I3 | ✅ 通过 | 所有 supported finding 都有非 legacy 证据 |
| I4 | ✅ 通过 | 每个候选都有 fixture 证据支撑 |
| I5 | ✅ 通过 | 2 × 6 组合完整覆盖 |
| I6 | ✅ 通过 | 2 条推荐的 rank 与 score 单调一致 |
| I7 | ✅ 通过 | 推荐引用的 finding / risk 全部可解析 |
| I8 | ✅ 通过 | 维度权重之和为 1.0 |
| I9 | ✅ 通过 | 证据来源类型：['fixture_snapshot'] |

**完整性结论：通过（failed_codes: []）**

## 发现

- **pgvector / deployment_complexity = medium** — pgvector installation requires compiling from source or using package managers, with platform-specific steps and tuning options for indexes.（证据: `fixture:pgvector:/sections/0/text`）
- **pgvector / ecosystem_maturity = mature** — pgvector has broad, production-ready support across PostgreSQL versions, multiple installation methods, extensive language bindings, and active maintenance with documented scaling, monitoring, and troubleshooting.（证据: `fixture:pgvector:/sections/0/text`）
- **pgvector / managed_service_needed = false** — pgvector is a PostgreSQL extension that users install and manage themselves; it does not require or imply a managed service.（证据: `fixture:pgvector:/sections/0/text`）
- **pgvector / ops_skill_floor = high** — pgvector provides robust, production-ready vector search operations with ACID compliance, multiple indexing strategies (HNSW/IVFFlat), and extensive tuning knobs for operational control.（证据: `fixture:pgvector:/sections/0/text`）
- **pgvector / scale_ceiling = large** — pgvector supports up to 64,000 dimensions via binary vectors and 16,000 dimensions for vector/halfvec/sparsevec types.（证据: `fixture:pgvector:/sections/0/text`）
- **qdrant_local / deployment_complexity = low** — Qdrant can be deployed locally with a single Docker command, requiring minimal setup for basic use.（证据: `fixture:qdrant_local:/sections/0/text`）
- **qdrant_local / ecosystem_maturity = mature** — Qdrant has official and community client libraries for multiple languages, extensive documentation, integrations, cloud offering, active Discord, and a clear 2025 roadmap.（证据: `fixture:qdrant_local:/sections/0/text`）
- **qdrant_local / managed_service_needed = false** — Qdrant can be run locally via Docker without requiring a managed service.（证据: `fixture:qdrant_local:/sections/0/text`）
- **qdrant_local / ops_skill_floor = high** — Qdrant is written in Rust, supports distributed deployment, sharding, replication, zero-downtime updates, observability, and production-grade security guidance.（证据: `fixture:qdrant_local:/sections/0/text`）
- 未支持维度 3 条（显式 unsupported，不推断补齐）

## 风险

- [high] pgvector 在权重 0.25 的维度 query_latency 上没有资料依据
- [high] qdrant_local 在权重 0.15 的维度 scale_ceiling 上没有资料依据
- [high] qdrant_local 在权重 0.25 的维度 query_latency 上没有资料依据
- [medium] 前两名综合效用分差仅 0.0500（阈值 0.05），排名不稳健
- [medium] qdrant_local 的 4 条结论全部出自同一段原文（/sections/0/text），结论独立性不足
- [medium] pgvector 的 5 条结论全部出自同一段原文（/sections/0/text），结论独立性不足

## 覆盖

- pgvector: 5/6 维度有结论
- qdrant_local: 4/6 维度有结论

