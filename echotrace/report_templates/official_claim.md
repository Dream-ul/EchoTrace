---
template_id: official_claim.report
version: "1.2"
investigation_type: official_claim
trace_template_id: official_claim.trace
trace_template_version: trace-template.v1
language: zh-CN
datasets:
  - id: claim_evidence_matrix
    purpose: 对照逐项主张、支持程度和材料边界
    source_nodes: [claim_breakdown, evidence_comparison, current_assessment]
    columns: [claim_id, assessment, evidence_status, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 一项可独立核验的声明主张
      minimum_samples: 2
      guidance: 对每项拆分后的主张分别记录材料支持状态和证据边界；不得转成真伪总分或无来源的比例，citation_ids 必须关联实际比较过的证据。
  - id: statement_versions
    purpose: 展示声明版本、发布时间和文字变化
    source_nodes: [original_statement, clarification_revision]
    columns: [version, published_at, change, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 可定位的声明或澄清版本
      minimum_samples: 2
      merge_strategy: event_rows
      guidance: 搜索原始声明、补充、澄清和修订文本，按可核对发布时间和版本差异逐行记录；转载不算新版本，发布日期不明时标明未知。
visuals:
  - id: statement-versions
    type: timeline
    title: 声明版本变化
    dataset: statement_versions
    fallback: table
chapters:
  - id: summary
    title: 核验摘要
    purpose: 汇总声明的主要内容、材料对照结果和当前准确表述。
    node_slots: [original_statement, claim_breakdown, evidence_comparison, clarification_revision, current_assessment]
    recommended_blocks: [paragraph, callout]
    data_needs: [sources]
    empty_data_behavior: 对无法核验的部分明确写出材料不足，不给出超出材料的判定。
  - id: statement
    title: 声明原文与主张拆分
    purpose: 确认原始声明版本，并拆成边界清晰的可核验主张。
    node_slots: [original_statement, claim_breakdown]
    recommended_blocks: [paragraph, claim_table, evidence_card]
    data_needs: [media, sources]
    empty_data_behavior: 找不到原始版本时注明使用的是转述材料，不冒充原文。
  - id: comparison
    title: 逐条材料对照
    purpose: 对每项主张并列支持、限制或反驳材料及其覆盖范围。
    node_slots: [evidence_comparison]
    recommended_blocks: [claim_table, table, source_list]
    data_needs: [sources]
    empty_data_behavior: 对缺少独立材料的主张，明确标注尚无独立印证。
  - id: revisions
    title: 补充、澄清与版本变化
    purpose: 说明声明后续版本的变化以及澄清覆盖的边界。
    node_slots: [clarification_revision]
    recommended_blocks: [timeline, table, paragraph]
    data_needs: [metrics, sources]
    empty_data_behavior: 没有后续版本材料时说明未发现可核实修订，不推断从未修订。
  - id: current_assessment
    title: 当前可表述结论
    purpose: 给出每项主张现阶段可准确表达的范围和保留条件。
    node_slots: [current_assessment]
    recommended_blocks: [claim_table, callout, source_list]
    data_needs: [sources]
    empty_data_behavior: 将“材料未能确认”与“主张为假”严格区分。
---
# 官方声明核验报告

逐项核验声明的可证伪内容。声明本身只能证明某主体曾作出该表述，不能单独证明表述内容真实；态度、预测和价值判断应与事实主张分开。

## 核验摘要
用简短文字说明声明包含的主要主张、材料整体支持到什么范围，以及读者目前可以采用的准确表述。不要以单一全局标签替代逐项分析。
### 声明主题与核验范围
说明声明针对什么问题、纳入了哪些可核验主张。
### 整体材料结论
概括哪些内容有支持、受到限制或仍无法确认。
### 可准确转述的结论
给出不超出材料边界的简明表述。

## 声明原文与主张拆分
优先展示可定位的原始声明及版本信息，再将复合句拆成可独立核对的主张。保留限定词、时间范围和数量口径。
### 原始声明与版本定位
呈现声明发布主体、时间、载体和可定位版本。
### 可证伪事实主张
将包含主体、行为、时间或数量的事实表述拆为独立主张。
### 态度、预测与价值判断
单独标识不能按事实真伪核验的表述。

## 逐条材料对照
每项主张都并列展示支持、限制、反驳或尚缺的独立材料，并解释材料能支持到哪里。报道转载同一来源不能算作多个独立印证。
### 支持材料及其证明范围
说明直接支持主张的来源及可证明的具体部分。
### 限制、反驳与冲突材料
并列呈现不同材料之间的冲突和来源差异。
### 仍缺少的关键证据
指出尚未取得、但会影响判断的材料。

## 补充、澄清与版本变化
按时间展示声明补充、澄清、修订或撤回的内容。说明变化是文字、范围还是事实状态发生改变。
### 补充与澄清的时间线
按时间列出新增说明及其发布主体。
### 版本差异及影响
比较文字、数据、范围或事实状态发生的变化。
### 来源关系与未确认变化
指出转载依赖，并区分没有检索到与确认不存在。

## 当前可表述结论
逐条给出当前准确且有限定的表达，清楚区分已获材料支持、被材料限定、存在冲突和仍无法确认的部分；结论必须引用节点证据。
### 有材料支持的表述
给出能够直接由材料支撑的限定结论。
### 受到限制或存在冲突的表述
保留反证、来源差异和适用范围。
### 仍无法确认的部分
列明当前证据无法回答的事项和原因。
