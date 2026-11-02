---
template_id: policy_regulation.report
version: "1.2"
investigation_type: policy_regulation
trace_template_id: policy_regulation.trace
trace_template_version: trace-template.v1
language: zh-CN
datasets:
  - id: policy_versions
    purpose: 追踪政策发布、生效、修订和当前版本
    source_nodes: [policy_signal, formal_release, effective_scope, revision_withdrawal]
    columns: [version, published_at, effective_at, status, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 可定位政策文件或正式版本
      minimum_samples: 2
      merge_strategy: event_rows
      guidance: 从政府原始文件提取版本、发布日期、生效日期和状态；提议、征求意见稿、正式发布、施行、修订和废止不能混为同一状态。
  - id: implementation_outcomes
    purpose: 整理执行案例或反馈中的可比较观察值
    source_nodes: [implementation, impact_feedback]
    columns: [period, subject, observed_value, unit, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 同口径执行观察值
      minimum_samples: 2
      guidance: 提取正式执行统计或可定位案例中的观察值、期间和执行主体；只有同单位、同定义、可比期间的数据才能用于柱状或趋势图，个案不能外推为总体效果。
visuals:
  - id: policy-versions
    type: timeline
    title: 政策版本时间线
    dataset: policy_versions
    fallback: table
  - id: implementation-outcomes
    type: bar
    title: 执行观察值
    dataset: implementation_outcomes
    x_field: period
    y_field: observed_value
    fallback: table
chapters:
  - id: summary
    title: 政策影响摘要
    purpose: 概括政策当前状态、执行进展、可观察影响和版本边界。
    node_slots: [policy_signal, formal_release, effective_scope, implementation, impact_feedback, revision_withdrawal]
    recommended_blocks: [paragraph, metric_grid]
    data_needs: [metrics]
    empty_data_behavior: 缺少可比较的效果指标时只总结政策状态和材料记录，不推断效果。
  - id: signal_formal
    title: 政策信号与正式发布
    purpose: 区分议题信号、提议讨论和正式政策文本及发布主体。
    node_slots: [policy_signal, formal_release]
    recommended_blocks: [paragraph, timeline, evidence_card]
    data_needs: [media, sources]
    empty_data_behavior: 找不到正式文本时明确区分非正式信号和正式政策。
  - id: effective_scope
    title: 生效时间与适用范围
    purpose: 说明生效时间、适用对象、地域、例外和过渡安排。
    node_slots: [effective_scope]
    recommended_blocks: [table, timeline, callout]
    data_needs: [metrics, sources]
    empty_data_behavior: 分开标注发布日期和生效日期，缺少正式依据时不补推范围。
  - id: implementation_impact
    title: 执行情况与影响反馈
    purpose: 对照执行口径、实际案例、反馈和可观察影响。
    node_slots: [implementation, impact_feedback]
    recommended_blocks: [paragraph, chart, table, claim_table]
    data_needs: [metrics, sentiment, stance, topic, sources]
    empty_data_behavior: 没有基线或可比较样本时不声称因果效果，个案不外推为总体。
  - id: revisions
    title: 修订、延期与当前版本
    purpose: 追踪后续变化并确认调查时点适用的版本及未解决问题。
    node_slots: [revision_withdrawal]
    recommended_blocks: [timeline, table, callout]
    data_needs: [sources]
    empty_data_behavior: 没有后续文件时说明本次材料未发现版本变化，并标注检索范围。
---
# 政策与监管影响调查报告

沿政策从信号到正式文本、生效、执行和版本变化展开。严格区分发布与生效、政策目标与实际效果、执行个案与总体结果；指标必须保留统计口径和时间范围。

## 政策影响摘要
先说明政策当前有效状态、已观察到的执行情况和反馈。若没有可比基线、样本或指标，不对政策效果作因果判断。
### 政策当前状态
说明调查时点下政策是否有效、暂停或已被替代。
### 执行与反馈概况
概括可核实的执行情况和受影响主体反馈。
### 效果结论的边界
说明数据和研究设计能否支持效果判断。

## 政策信号与正式发布
区分早期政策信号、讨论稿、提议和正式发布文本。说明发布主体、版本及原始文件定位。
### 早期信号与讨论稿
列出可定位的政策讨论、征求意见或提议材料。
### 正式发布文本
说明发布主体、日期、文号和权威文本来源。
### 从信号到正式政策的变化
仅说明材料可证实的内容变化，不推测未公开的决策原因。

## 生效时间与适用范围
清晰列出发布日期、生效日期、适用对象、地域范围、例外和过渡规则。不同对象或版本有差异时用表格呈现。
### 发布与生效时间
分别标出发布日期、生效日期和过渡期。
### 适用对象与地域范围
明确政策约束对象、地区和适用条件。
### 例外、过渡与配套规则
整理豁免、过渡安排和配套要求。

## 执行情况与影响反馈
对照正式规定、执行机构口径和可定位案例，再整理受影响主体的反馈。数据不足时说明覆盖范围；不能仅凭前后同时发生推导政策造成了结果。
### 执行机构与执行动作
呈现执行部门、实施步骤和可定位案例。
### 受影响主体与反馈
区分个案陈述、机构记录和汇总样本。
### 可观察影响及因果边界
说明可观察结果、基线和样本范围，不超出证据推断政策效果。

## 修订、延期与当前版本
按时间展示修订、延期、暂停或废止材料，并说明调查时点适用的版本。未检索到变化不等于证明不存在变化，应交代材料范围。
### 修订、延期或暂停记录
按时间呈现已确认的版本调整和政策状态变化。
### 调查时点适用版本
指出当前适用文本及其来源。
### 版本检索范围与未决事项
说明检索到的最后更新时间，以及无法确认的后续变化。
