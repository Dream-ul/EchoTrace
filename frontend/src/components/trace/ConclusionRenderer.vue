<template>
  <div v-if="blocks.length" class="conclusion-renderer" aria-label="结构化综合结论">
    <template v-for="(block, index) in blocks" :key="`${block.type}-${index}`">
      <component
        :is="`h${block.level + 2}`"
        v-if="block.type === 'heading'"
        class="conclusion-heading"
        :class="`heading-level-${block.level}`"
      >
        {{ block.text }}
      </component>
      <p v-else-if="block.type === 'paragraph'" class="conclusion-paragraph">{{ block.text }}</p>
      <ol v-else-if="block.type === 'list' && block.ordered" class="conclusion-list ordered-list">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex">
          <span class="list-index">{{ itemIndex + 1 }}</span>
          <span>{{ item }}</span>
        </li>
      </ol>
      <ul v-else-if="block.type === 'list'" class="conclusion-list unordered-list">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex">
          <span class="list-bullet" aria-hidden="true" />
          <span>{{ item }}</span>
        </li>
      </ul>
      <div v-else-if="block.type === 'table'" class="conclusion-table-wrap">
        <div v-if="block.title" class="table-caption">{{ block.title }}</div>
        <div class="conclusion-table-scroll">
          <table class="conclusion-table">
            <thead>
              <tr><th v-for="(column, columnIndex) in block.columns" :key="columnIndex">{{ column }}</th></tr>
            </thead>
            <tbody>
              <tr v-for="(row, rowIndex) in block.rows" :key="rowIndex">
                <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ cell }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
  <p v-else class="conclusion-empty">{{ hasSourceText ? '综合结论整理中。' : '暂无可展示的综合结论。' }}</p>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ConclusionBlock, ConclusionIR } from '@/api/investigations'

const props = defineProps<{
  ir?: ConclusionIR | null
  hasSourceText?: boolean
}>()

const blocks = computed<ConclusionBlock[]>(() => {
  const value = props.ir
  if (!value || value.schema_version !== 'conclusion-ir.v1' || !Array.isArray(value.blocks)) return []
  return value.blocks.filter((block) => {
    if (!block || typeof block !== 'object' || typeof block.type !== 'string') return false
    if ((block.type === 'heading' || block.type === 'paragraph') && !String(block.text || '').trim()) return false
    if (block.type === 'list') return Array.isArray(block.items) && block.items.length > 0
    if (block.type === 'table') return Array.isArray(block.columns) && block.columns.length > 1 && Array.isArray(block.rows)
    return block.type === 'heading' || block.type === 'paragraph'
  })
})

const hasSourceText = computed(() => Boolean(props.hasSourceText))
</script>

<style scoped>
.conclusion-renderer { display: grid; gap: 13px; color: #334841; }
.conclusion-empty { margin: 0; color: #687972; font-size: 13px; line-height: 1.85; }
.conclusion-heading { margin: 5px 0 0; padding: 9px 13px; border-left: 3px solid #4f8a72; border-radius: 0 6px 6px 0; background: #eff7f2; color: #284d3e; line-height: 1.55; }
.heading-level-1 { font-size: 17px; }
.heading-level-2 { font-size: 15px; }
.heading-level-3 { font-size: 13px; }
.conclusion-paragraph { margin: 0; color: #40534b; font-size: 13px; line-height: 1.9; white-space: pre-line; }
.conclusion-list { display: grid; gap: 8px; margin: 0; padding: 13px 15px; border: 1px solid #e0e9e3; border-radius: 7px; background: #fbfdfb; list-style: none; }
.conclusion-list li { display: grid; grid-template-columns: 24px minmax(0, 1fr); gap: 9px; align-items: start; color: #40534b; font-size: 13px; line-height: 1.75; }
.list-index { display: grid; width: 23px; height: 23px; place-items: center; border: 1px solid #b8d0c2; border-radius: 50%; background: #f0f7f3; color: #397860; font-size: 10px; font-weight: 700; }
.list-bullet { width: 7px; height: 7px; margin: 9px 0 0 8px; border-radius: 50%; background: #78a497; box-shadow: 0 0 0 4px #edf5f1; }
.conclusion-table-wrap { overflow: hidden; border: 1px solid #dce7e1; border-radius: 7px; background: #fff; }
.table-caption { padding: 10px 13px; border-bottom: 1px solid #e1e9e4; background: #f2f7f4; color: #426b5c; font-size: 12px; font-weight: 700; }
.conclusion-table-scroll { overflow-x: auto; }
.conclusion-table { width: 100%; min-width: 560px; border-collapse: collapse; table-layout: fixed; }
.conclusion-table th, .conclusion-table td { padding: 10px 11px; border-right: 1px solid #e5ece8; border-bottom: 1px solid #e5ece8; text-align: left; vertical-align: top; color: #465851; font-size: 12px; line-height: 1.65; overflow-wrap: anywhere; }
.conclusion-table th { background: #f7faf8; color: #315948; font-size: 11px; font-weight: 700; }
.conclusion-table tr:last-child td { border-bottom: 0; }
.conclusion-table th:last-child, .conclusion-table td:last-child { border-right: 0; }
@media (max-width: 560px) {
  .conclusion-paragraph, .conclusion-list li { font-size: 12px; line-height: 1.8; }
  .conclusion-table { min-width: 500px; }
}
</style>
