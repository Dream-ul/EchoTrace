<template>
  <section class="trace-view" aria-labelledby="trace-title">
    <header class="trace-heading">
      <div>
        <p class="eyebrow">已完成的调查脉络</p>
        <h2 id="trace-title">{{ trace.subject || '事件脉络' }}</h2>
        <p>按调查节点呈现关键事实、来源依据和当前认识。</p>
      </div>
      <el-tag type="success" effect="light"><el-icon><CircleCheck /></el-icon> 已审阅</el-tag>
    </header>

    <el-alert v-if="review?.findings?.length" class="editorial-note" type="info" :closable="false" title="跨节点编辑整理">
      <ul><li v-for="(finding, index) in review.findings" :key="index">{{ finding.recommendation || finding.message }}</li></ul>
    </el-alert>

    <el-empty v-if="!trace.nodes?.length" description="暂无已完成节点" :image-size="80" />
    <div v-else class="trace-list">
      <article v-for="(node, index) in trace.nodes" :key="node.node_id" class="trace-node">
        <div class="trace-node-index">{{ String(index + 1).padStart(2, '0') }}</div>
        <div class="trace-node-body">
          <header class="trace-node-heading">
            <div><h3>{{ node.title }}</h3><p>{{ node.goal }}</p></div>
            <span>{{ node.draft?.claims?.length || 0 }} 项主张</span>
          </header>
          <section class="node-conclusion" aria-label="节点综合结论">
            <h4>综合结论</h4>
            <ConclusionRenderer v-if="node.draft?.conclusion" :ir="node.draft.conclusion_ir" :has-source-text="Boolean(node.draft.conclusion)" />
            <el-empty v-else description="该节点暂无综合结论" :image-size="56" />
          </section>
          <details v-if="node.draft?.claims?.length || node.draft?.evidence?.length" class="claim-details">
            <summary>查看主张与依据 <span>{{ node.draft?.claims?.length || 0 }} 项主张 · {{ node.draft?.evidence?.length || 0 }} 条依据</span></summary>
            <div class="claim-list">
              <article v-for="(claim, claimIndex) in node.draft?.claims || []" :key="claim.claim_id" class="claim-item">
                <div class="claim-label"><span>{{ String(claimIndex + 1).padStart(2, '0') }}</span>{{ claimTypeLabel(claim.claim_type) }}</div>
                <p>{{ displayText(claim.text) }}</p>
                <div v-for="evidence in claimEvidence(node, claim)" :key="evidence.evidence_id" class="evidence-item">
                  <blockquote>{{ displayText(evidence.excerpt) }}</blockquote>
                  <a v-if="safeUrl(evidence.locator?.url)" :href="safeUrl(evidence.locator?.url)" target="_blank" rel="noreferrer">
                    {{ sourceHost(evidence.locator?.url) }}<span v-if="evidence.locator?.section"> · {{ evidence.locator.section }}</span>
                    <el-icon><TopRight /></el-icon>
                  </a>
                  <span v-else class="evidence-relation">{{ relationLabel(evidence.relation) }}</span>
                </div>
              </article>
            </div>
          </details>
          <section v-if="node.draft?.media?.length" class="node-media">
            <h4>相关图片与视频</h4>
            <div v-for="media in node.draft.media" :key="media.media_id" class="media-row">
              <a v-if="safeUrl(media.source_url)" :href="safeUrl(media.source_url)" target="_blank" rel="noreferrer">{{ media.caption || '打开相关媒体' }}<el-icon><TopRight /></el-icon></a>
              <span v-else>{{ media.caption || '相关媒体' }}</span>
              <small>{{ media.why_relevant }}</small>
            </div>
          </section>
        </div>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { CircleCheck, TopRight } from '@element-plus/icons-vue'
import type { InvestigationTrace, NodeClaim, NodeEvidence } from '@/api/investigations'
import ConclusionRenderer from './ConclusionRenderer.vue'

const { trace, review } = defineProps<{
  trace: InvestigationTrace
  review?: { findings?: Array<{ kind?: string; message?: string; recommendation?: string }> } | null
}>()

function claimEvidence(node: InvestigationTrace['nodes'][number], claim: NodeClaim): NodeEvidence[] {
  const ids = new Set(claim.evidence_ids || [])
  return (node.draft?.evidence || []).filter(item => ids.has(item.evidence_id))
}

function displayText(value?: unknown) {
  let text = String(value || '')
  for (const node of trace.nodes || []) {
    if (node.slot_id) text = text.replaceAll(node.slot_id, node.title)
  }
  return text
    .replace(/\b(?:n?claim|doc(?:ument)?|evidence|node|run|trace|template|revision|review|event)[_-][A-Za-z0-9][A-Za-z0-9_-]{5,}\b/gi, '')
    .replace(/\s{2,}/g, ' ')
    .replace(/\s+([，。；：、,.!?])/g, '$1')
    .trim()
}

function safeUrl(value?: unknown) {
  const text = String(value || '')
  try { return ['http:', 'https:'].includes(new URL(text).protocol) ? text : undefined } catch { return undefined }
}

function sourceHost(value?: unknown) {
  try { return new URL(String(value)).hostname.replace(/^www\./, '') } catch { return '查看来源' }
}

function relationLabel(value: string) {
  return ({ supports: '支持该主张', contradicts: '与主张存在差异', qualifies: '限定主张范围' } as Record<string, string>)[value] || '相关依据'
}

function claimTypeLabel(value: string) {
  const labels: Record<string, string> = {
    observation: '观察', attribution: '主体归属', uncertainty: '不确定性', fact: '事实', scope: '范围',
    spread: '传播', viewpoint: '观点', metric: '指标', action: '行动', response: '响应', result: '结果',
    status: '状态', statement: '声明', comparison: '对照', boundary: '边界', change: '变化',
  }
  return labels[value] || '研究主张'
}
</script>

<style scoped>
.trace-view { max-width: 1000px; margin: 0 auto; color: #263936; }
.trace-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 17px 0 20px; border-bottom: 1px solid #e2e8e4; }
.trace-heading h2 { margin: 0; color: #263b37; font-size: 21px; line-height: 1.4; }
.trace-heading p:not(.eyebrow) { margin: 6px 0 0; color: #788680; font-size: 12px; }
.eyebrow { margin: 0 0 6px; color: #518077; font-size: 11px; font-weight: 700; }
.trace-heading :deep(.el-tag .el-icon) { margin-right: 4px; vertical-align: -2px; }
.editorial-note { margin-top: 16px; }
.editorial-note ul { margin: 6px 0 0; padding-left: 18px; line-height: 1.6; }
.trace-list { padding-top: 5px; }
.trace-node { display: grid; grid-template-columns: 34px minmax(0, 1fr); gap: 15px; padding: 22px 0; border-bottom: 1px solid #e3e9e5; }
.trace-node-index { width: 29px; height: 29px; display: grid; place-items: center; border: 1px solid #aac9bd; border-radius: 50%; background: #f0f7f3; color: #39765f; font-size: 10px; }
.trace-node-body { min-width: 0; }
.trace-node-heading { display: flex; justify-content: space-between; gap: 12px; }
.trace-node-heading h3 { margin: 0; color: #2a403b; font-size: 16px; }
.trace-node-heading p { margin: 5px 0 0; color: #7b8782; font-size: 12px; line-height: 1.5; }
.trace-node-heading > span { flex-shrink: 0; color: #7c8983; font-size: 11px; }
.node-conclusion { margin-top: 15px; padding: 14px; border: 1px solid #dfe9e3; border-radius: 8px; background: #fbfdfb; }
.node-conclusion h4, .node-media h4 { margin: 0 0 10px; color: #315448; font-size: 13px; }
.claim-details { margin-top: 13px; border-top: 1px solid #edf0ee; }
.claim-details summary { display: flex; align-items: center; gap: 8px; padding: 11px 0 2px; color: #36736a; font-size: 12px; cursor: pointer; list-style-position: inside; }
.claim-details summary span { color: #8b9791; font-size: 11px; }
.claim-list { padding: 4px 0 0 17px; }
.claim-item { padding: 12px 0; border-bottom: 1px solid #edf0ee; }
.claim-label { display: flex; gap: 8px; align-items: center; color: #658077; font-size: 11px; }
.claim-label span { color: #a1aaa5; }
.claim-item > p { margin: 7px 0 0; color: #344841; font-size: 13px; line-height: 1.7; }
.evidence-item { margin: 9px 0 0 12px; padding: 8px 11px; border-left: 2px solid #b7cec3; background: #f6f8f6; }
.evidence-item blockquote { margin: 0; color: #5c6963; font-size: 12px; line-height: 1.65; }
.evidence-item a, .evidence-relation { display: inline-flex; align-items: center; gap: 4px; margin-top: 6px; color: #39776e; font-size: 10px; text-decoration: none; }
.node-media { margin-top: 14px; }
.node-media a { display: flex; justify-content: space-between; gap: 10px; padding: 8px 0; border-top: 1px solid #edf0ee; color: #34766e; font-size: 12px; text-decoration: none; }
.node-media a span { flex: 1; color: #85918b; }
@media (max-width: 560px) {
  .trace-heading h2 { font-size: 18px; }
  .trace-node { grid-template-columns: 27px minmax(0, 1fr); gap: 9px; }
  .trace-node-index { width: 25px; height: 25px; }
  .trace-node-heading { flex-direction: column; gap: 5px; }
  .claim-list { padding-left: 4px; }
  .node-conclusion { padding: 10px; }
  .node-media a { flex-wrap: wrap; }
}
</style>
