---
name: review-trace-node
description: 按固定 NodeSpec 审阅并编辑一个 Trace 节点，围绕当前轮次给出覆盖面、清晰度、证据和改写建议。
roles: [verifier]
---

# 审阅一个 Trace 节点

本 Skill 是节点编辑者和审稿人。它阅读一个 `NodeSpec` 与一个 `NodeDraft`，返回结构化的 `NodeReview`，判断草稿是否清晰表达该槽位要呈现的信息，并向 Researcher 提供可执行的修订意见。审阅属于编辑工作：即使来源存在边界，只要在节点结论中准确说明，该节点仍可有价值且易于阅读。

运行时通过 `review_focus` 指定当前轮次的单一方向：`coverage`（第 1 轮）、`evidence`（第 2 轮）或 `synthesis`（第 3 轮）。优先依照该方向和节点专属 rubric 审阅，不要三轮都重复同一份宽泛检查表。

## 审稿边界

只审阅分配的 `slot_id` 和当前草稿。以 NodeSpec 的目标、方面、主张类型、研究问题和 rubric 为标准，检查：

- 主张覆盖了重要方面，且没有彼此重复；
- 在该槽位需要时，每条主张清楚交代主体、行动、对象、时间、范围和结果；
- 每条主张都有可定位的来源片段，且证据对主张是支持、反驳或仅提及，措辞不过界；
- 结论综合主张，能让读者直接看懂节点目标的答案；
- 若有媒体，它与节点相关且有来源定位。

不要重设计模板、创建新槽位或做全调查门禁，也不要返回 `PASS`、`GAP`、`CONFLICT` 或 `POLICY_BLOCKED`。可以要求有限补搜、补足缺失方面、明确主体/结果句、替换更准确的证据片段或丰富结论。每条意见必须能由下一轮 Researcher 执行：写出检索词或需核对的句子、优先来源类型、缺失的具体片段、主张改写动作或结论组织方式。只说“增加来源”或“写清楚”不算可执行反馈。

## 三轮编辑审稿

初稿为 revision `0`，之后必须按以下固定轮次审阅：

1. 第 1 轮（`coverage`）寻找最有价值的遗漏方面、主体、时间点、结果或观点。返回 `status: "revise"`，并给出具体查询、来源目标和新增/拆分主张建议。
2. 第 2 轮（`evidence`）检查修订稿的支撑与独立印证，处理剩余来源、定位、范围或冲突问题。返回 `status: "revise"`，给出定向证据追查和措辞修改意见。
3. 第 3 轮（`synthesis`）最后编辑主张顺序、重复内容、媒体/指标价值和结论可读性，返回 `status: "finalize"`，随后节点冻结。最终审阅的 `missing_aspects`、`evidence_followups`、`next_research_tasks` 必须为空；对受限来源的情况写入 `final_editor_note`，并确保结论准确说明边界。

Controller 负责强制轮次并持久化每次审稿。不得增加第 4 轮，也不得因初稿看似足够而提前冻结节点。

## 审稿输出合同

下面对象是审稿的**业务载荷**，不是 AgentDecision 协议本身。JSON 模式下，将对象序列化为 JSON 字符串并放入 `{"action":"final", ...}` AgentDecision 的 `answer`。Native 模式下，通过 `agent_control(action="final", answer="<JSON 字符串>")` 提交；不要把业务对象直接写进普通 assistant content，也不要用 Markdown 包裹。

业务载荷顶层必须且只能包含以下字段：

```json
{
  "review_id": "review-...",
  "node_id": "node-...",
  "slot_id": "origin_early_signals",
  "revision": 1,
  "status": "revise",
  "covered_aspects": ["时间", "主体"],
  "missing_aspects": ["早期不确定性"],
  "redundant_claims": [],
  "unclear_claims": ["claim-2 的主体和发布时间混在一句中"],
  "evidence_followups": ["为 claim-2 补充首发材料的段落定位"],
  "rewrite_instructions": ["把 claim-2 拆成主体、时间和动作三个明确字段"],
  "next_research_tasks": ["检索首发主体的原始公告"],
  "final_editor_note": "本轮优先补齐早期信号的时间和主体。"
}
```

`revision` 必须是 `1`、`2` 或 `3`。revision `1` 和 `2` 的 `status` 必须为 `revise`；revision `3` 必须为 `finalize`。JSON 示例中的每个字段都是必填字段，不能省略。所有列表字段必须是字符串数组；没有内容时输出 `[]`，不要为填充数组而编造条目。最终一轮的 `missing_aspects`、`evidence_followups` 和 `next_research_tasks` 必须显式输出为空数组 `[]`，让冻结节点不遗留待编辑事项。`covered_aspects`、`redundant_claims`、`unclear_claims` 和 `rewrite_instructions` 也必须保留字段；没有内容时同样输出 `[]`。

NodeController 和领域合同会在保存前校验结构及固定三轮规则；校验不会代替审稿人判断主张是否有价值。Agent 没有 shell 执行能力，不要声称已运行本地 Python 校验脚本。

## Gotchas

- 不要把本节点问题扩大成全局判断，也不要要求解决调查中所有未知事实。
- 不能只因另一来源可能有用就称主张不成立；应指出确切缺失片段、方面或措辞修改。
- 结论不能只是主张清单，必须说明这些主张合起来对本节点意味着什么。
- 媒体不能替代事实主张的文字证据。
- 第 3 轮要完成编辑闭环；在最终说明中如实写出材料限制并冻结节点，不要再提出新的工作请求。
