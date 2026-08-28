# PaperWeaver 全局策略

状态：**已定稿（2026-08-27）**。本文件是产品形态、协作协议与质量纪律的唯一权威来源；
与其它文档或代码注释冲突时，以本文件为准。修改策略必须先改本文件，并在 commit
message 中说明理由。

## 1. 产品定位与最终形态

产品终点：把一篇学术论文变成**可核查的中文交付物**——全译 Markdown + A4 PDF + 中文摘要。
PDF 尽可能准确地爬取内容并规整为 Markdown，爬不动的交给 LLM 在证据约束下补齐。

最终形态是三个发布物：

| 发布物 | 内容 | 是否含模型 |
| --- | --- | --- |
| `paperweaver` Python 包 | 提取、裁剪、schema、验证器、门禁、QA、exporter，全部确定性逻辑 | **零模型调用**，可离线、可进 CI、可复现 |
| Skill（如 `paperweaver-audit` / `paperweaver-translate`） | 流程编排知识：读工单、按 schema 提案、调 CLI 的顺序 | 不内置模型，驱动用户自己的 agent |
| 项目文件夹格式 | 原件、账本、overlay、交付物的目录规范（事实数据契约） | — |

**模型永远在门外，纪律在门内。** 不做内置模型、不做服务端、不做全 LLM 重建。

## 2. 两层架构与「一个门」协议

```
PDF → [确定性引擎] → 规整 Markdown（已解决部分）
          ├─ 对象账本：每个对象 = rendered / excluded / unresolved，零静默丢失
          └─ unresolved 工单：bbox + 裁剪图 + issue code + 邻近证据
                 ↓
        [audit package 契约]（两层的唯一接口）
                 ↓
        [LLM 审计层（skill）] 逐元素提案
                 ↓
        [同一套确定性 validator 复检] ──通过──→ 合并进 Markdown
                                    └─不通过─→ 保持 unresolved，绝不静默替换
```

一个门（one-door）proposal 协议：**模型的一切贡献从同一扇门进出**——翻译草稿与审计
修复是同一机制的两个实例：

- 一种提交格式：严格 JSONL proposal（target 引用、类型、payload、证据引用、model
  标识、digest）；
- 多个验证器：按 proposal 类型分派（passage 覆盖校验、表格网格/字符全覆盖、LaTeX
  验证器……），验证永远在 CLI 侧；
- 有界重试：拒绝时返回机器可读的结构化拒因，最多一次重试，再失败保持 unresolved
  留给人；
- append-only overlay：每条记录可追溯、可回滚；LLM 永远不直接改内容，只追加提案；
- CLI 面：`audit-export` / `audit-import` / `verify-draft` / `audit-status`（已落地）。

引擎单独就有独立价值（诚实的不完整导入 + 审计包）；skill 是放大器，不是依赖。

## 3. 交付状态三分

- `complete`：纯确定性通过全部门禁；
- `complete-with-repair`：LLM 提案经复检全部通过后合并；
- `reviewable`：仍有 unresolved crop，打包给人定夺。

三种状态都能导出 Markdown；区别只是可信等级与标注。

## 4. 两层 KPI

- **引擎**：coverage + 诚实账本（每个对象三分归属，accounting = 1.0）；
- **审计层**：unresolved burn-down + 提案通过率。

语义度量为 prose/table 双流分区计分（`unicode-token-partitioned-v2`，见
`pdf-import-design.md` §11.3）；`semantic_targets`（0.995/0.995/0.99）只报告差距；
回归门禁用 `pin-floors` 生成的 `tests/corpus/semantic-floors.json`。

## 5. 质量纪律

- `scripts/check.sh`（ruff + pytest + 7 篇 corpus slice）是**提交前唯一入口**；
  红就不提交。`CHECK_SKIP_CORPUS=1` 只允许在明显与语料无关的改动上临时使用。
- floors 只能经 `pdf_corpus.py pin-floors --from <run>` 显式移动，随代码提交并被
  review；`run` 永不自动改写；floors 缺失时 `run` 直接报错。
- CI corpus job 在 push/PR 触发（manifest-hash 缓存）。
- 未验证内容（未复检的提案、猜测的 glyph、未 accounting 的对象）永不进入
  Markdown 与交付物。

## 6. 路线顺序

1. ~~度量口径 v2 + floors/targets 分离 + check.sh + CI 闭环~~（已完成，2026-08-27）
2. ~~audit package 契约 + `audit-export` / `audit-import` / `verify-draft` CLI~~（已完成，2026-08-27；工作单导出、确定性提案复检、append-only 提案账本、burn-down 均已落地）；
3. ~~已接受提案向 `article.md`/render-tree 的确定性物化 + `complete_with_repair`
   交付状态~~（已完成，2026-08-28：`audit-apply`，视图恒等于 base+账本的可重推导，
   base 账本不可变）。剩余：`paperweaver-audit` skill MVP——读裁剪图造网格/写
   LaTeX 的编排知识，首批真实工单 = 语料中无框/跨页表与未决公式；
4. 引擎确定性战线按 ROI 继续：无框表（acl-tables-2024×18、warwick×9）→ 标题层级
   → 断词/跨页续段；一次只开一条结构战线，每条战线 = policy 参数 + 合成 fixture +
   真实语料对照 + design doc 章节；
5. 翻译交付质量随 corpus 每篇 complete/complete-with-repair 顺路跟进；
6. P4 OCR、P5 第三方 backend 最后。

## 7. 边界（明确不做）

- 引擎内置模型调用、托管服务端；
- 整篇交给 LLM 重建（Marker/Nougat 式）——违背证据可溯与零幻觉承诺；
- 以多数投票或模型置信度替代 validator 证据；
- 把 OCR/模型输出自动视为与 born-digital 文本层同等级证据。
