---
template_id: product_safety.report
version: "1.2"
investigation_type: product_safety
trace_template_id: product_safety.trace
trace_template_version: trace-template.v1
language: zh-CN
datasets:
  - id: affected_scope
    purpose: 按产品、批次或样本范围整理有明确分母的影响观察
    source_nodes: [affected_scope, investigation_findings]
    columns: [category, count, unit, denominator, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 同一产品和统计范围内的分类观测
      minimum_samples: 2
      guidance: 提取监管/企业调查、检测或投诉材料明确给出的分类数量，逐行说明产品型号/批次、分母和统计范围；投诉、检测样本和总体估计分开记录。
  - id: remediation_progress
    purpose: 展示召回、维修、替换和整改的阶段状态
    source_nodes: [remediation_recall, retest_closure]
    columns: [stage, status, date, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 有来源依据的整改阶段记录
      minimum_samples: 2
      merge_strategy: event_rows
      guidance: 从企业或监管原始材料整理措施阶段、当前状态和明确日期；区分计划、启动、完成和效果确认，未知日期不得补造。
visuals:
  - id: affected-scope
    type: bar
    title: 受影响范围
    dataset: affected_scope
    x_field: category
    y_field: count
    fallback: table
  - id: remediation-progress
    type: timeline
    title: 整改进度
    dataset: remediation_progress
    fallback: table
chapters:
  - id: summary
    title: 调查摘要
    purpose: 概括产品问题、当前确认范围、处置和剩余风险。
    node_slots: [product_scope, first_anomaly, affected_scope, investigation_findings, remediation_recall, retest_closure]
    recommended_blocks: [paragraph, metric_grid]
    data_needs: [metrics]
    empty_data_behavior: 没有同口径数量时用定性范围描述，不推算影响规模。
  - id: product_anomaly
    title: 产品范围与首次异常
    purpose: 识别产品版本、批次、使用条件和最早异常材料。
    node_slots: [product_scope, first_anomaly]
    recommended_blocks: [paragraph, table, timeline, image]
    data_needs: [media, sources]
    empty_data_behavior: 无法确认型号、批次或使用条件时逐项标为未知。
  - id: scope_findings
    title: 受影响范围与调查发现
    purpose: 解释样本数量口径、调查方法、检测结果及原因边界。
    node_slots: [affected_scope, investigation_findings]
    recommended_blocks: [paragraph, chart, table, evidence_card]
    data_needs: [metrics, sources]
    empty_data_behavior: 区分投诉、个案记录、检测样本和总体估计，缺少数据时不外推。
  - id: remediation
    title: 处置、召回与整改
    purpose: 按企业和监管主体展示措施、覆盖范围和执行状态。
    node_slots: [remediation_recall]
    recommended_blocks: [paragraph, claim_table, timeline, image, video]
    data_needs: [metrics, media, sources]
    empty_data_behavior: 将计划、启动、完成及完成效果分别标识。
  - id: retest
    title: 复测结果与当前风险
    purpose: 说明复测、结案状态和仍需关注的风险。
    node_slots: [retest_closure]
    recommended_blocks: [paragraph, callout, table]
    data_needs: [metrics, sources]
    empty_data_behavior: 没有复测材料时明确说明不能据此确认风险已消除。
---
# 产品安全调查报告

始终区分消费者反馈、监管或企业调查、实验室检测和已确认结论。产品型号、版本、批次、使用条件和统计分母不同的材料不能合并成一个总量。

## 调查摘要
回答涉及什么产品、确认了什么问题、采取了什么措施、当前还剩什么风险。重要数字要附单位、时间、范围、分母和来源。
### 产品与问题概况
说明产品范围、已确认异常和调查时点下的风险状态。
### 已采取措施与结果
概括召回、整改、检测或复测结果，并区分计划与完成。
### 尚存风险与结论边界
点明仍需验证的部分及其证据限制。

## 产品范围与首次异常
先锁定型号、版本、批次和适用范围，再按时间说明最早异常。用户反馈只能写成反馈，不能直接提升为已证实缺陷。
### 产品型号、版本与批次
明确调查覆盖的产品及不能合并的版本、批次或使用条件。
### 最早异常与用户反馈
按时间说明首次异常；将用户反馈与已确认故障分开。
### 调查启动与范围界定
说明企业、监管或检测机构启动调查的时间和实际覆盖范围。

## 受影响范围与调查发现
分别呈现投诉数、调查样本、检测样本和监管公布范围。说明检测对象、方法和结果能够支持到什么程度；相关性不能单独证明原因。
### 受影响范围与样本口径
分开展示投诉、故障报告、抽检和监管数据，并保留各自分母。
### 检测方法与主要发现
说明检测对象、方法、结果和来源支持的结论范围。
### 原因判断与不确定性
区分已查明原因、相关线索和仍待验证的假设。

## 处置、召回与整改
按责任主体组织召回、下架、维修、替换和整改。写明措施覆盖范围及其状态，现场图像或视频只用于解释节点材料已经记录的情况。
### 企业及监管措施
按主体说明召回、下架、通报或其他处置措施。
### 措施覆盖范围与执行进度
说明覆盖型号、批次、地区和已经完成的执行环节。
### 整改验证材料
呈现可定位的整改或现场材料及其能说明的内容。

## 复测结果与当前风险
说明复测依据、结案情况和遗留风险。没有复测或持续监测材料时，不把“已采取措施”写成“风险已消除”。
### 复测与结案依据
说明复测对象、方法、时间及结案材料。
### 调查时点下的风险状态
区分已排除风险、持续关注风险和没有材料判断的事项。
### 后续验证需求
列出仍需的监测、检测或信息披露。
