<template>
  <section class="page" data-module="forecast_model">
    <header class="page-head">
      <div>
        <h2>预测模型</h2>
        <p class="page-desc">
          维护模型配置与预测结果留档：筛选先锁定一个完整训练批次，再读取该批次的预测点，
          刷新不会换一组随机值，训练批次号与指标口径始终随表展示。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出预测结果</button>
        <button class="btn" type="button" data-testid="refresh" @click="reload">刷新留档</button>
      </div>
    </header>

    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span class="trend-up" style="font-size:12px">V019 留档</span>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>批次口径</span>
        <select v-model="policy" data-testid="filter-policy">
          <option value="latest_complete">最近完成批次</option>
          <option value="earliest_complete">最早完整批次</option>
        </select>
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <div class="batch-banner" data-testid="batch-context">
      <span class="batch-item">训练批次：<strong data-testid="active-batch">{{ batchContext.trainingBatch || '—' }}</strong></span>
      <span class="batch-item">指标编码：<strong data-testid="active-metric">{{ batchContext.metricCode || '—' }}</strong></span>
      <span class="batch-item">精度位数：<strong>{{ batchContext.precision ?? '—' }}</strong></span>
      <span class="batch-item">指标口径：<strong data-testid="active-calibre">{{ batchContext.calibre || '—' }}</strong></span>
      <span class="batch-item">批次口径：<strong>{{ payload?.policyLabel || '最近完成批次' }}</strong></span>
      <span v-if="batchContext.batch?.完成时间" class="batch-item">完成时间：{{ batchContext.batch.完成时间 }}</span>
    </div>

    <div v-if="payload?.conflict" class="conflict-notice" data-testid="conflict-notice">
      <strong>版本冲突：</strong>同一业务周期存在多个完整训练批次。当前按「{{ payload.policyLabel }}」返回，
      <template v-if="payload.comparison">
        旧批次 <code>{{ payload.comparison.训练批次 }}</code> 平均指标值 {{ payload.comparison.平均展示值 }}，
        与当前批次差异 {{ payload.comparison.差异展示值 }}。
        <button class="link" type="button" data-testid="toggle-comparison" @click="showComparison = !showComparison">
          {{ showComparison ? '收起旧批次对照' : '查看旧批次对照' }}
        </button>
      </template>
      <template v-else>该口径固定返回最早完成批次，不附旧批次对照。</template>
      <div v-if="showComparison && payload.comparison" class="comparison-list" data-testid="comparison-list">
        <div v-for="point in payload.comparison.预测点" :key="`compare-${point.id}`" class="comparison-item">
          {{ point.预测期 }} · {{ point.展示值 }}（{{ payload.comparison.训练批次 }} / {{ payload.comparison.指标口径 }}）
        </div>
      </div>
    </div>
    <p v-else-if="payload?.message" class="empty-state" data-testid="prediction-message">{{ payload.message }}</p>

    <table class="data-table" data-testid="prediction-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>详情</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="`${row.训练批次}-${row.id}`"
          :data-testid="`prediction-row-${row.id}`"
          :data-batch="row.训练批次"
          :class="{ selected: detail?.point?.id === row.id }"
        >
          <td>{{ row.预测期 }}</td>
          <td>{{ row.模型编号 }}</td>
          <td>{{ row.预测对象 }}</td>
          <td>{{ row.训练批次 }}</td>
          <td>{{ row.指标编码 }}</td>
          <td>{{ row.展示值 }}</td>
          <td>{{ formatValue(row.对比基准, row.精度位数) }}</td>
          <td>{{ formatValue(row.差异, row.精度位数) }}</td>
          <td>{{ row.指标口径 }}</td>
          <td><button class="link" type="button" :data-testid="`detail-${row.id}`" @click="openDetail(row)">查看</button></td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state" data-testid="prediction-empty">
            {{ payload?.message || '当前筛选条件下没有完整批次的预测点' }}
          </td>
        </tr>
      </tbody>
    </table>
    <footer class="page-foot">
      <span>共 {{ payload?.total ?? 0 }} 个预测点 · 训练批次 {{ batchContext.trainingBatch || '—' }} · 指标口径 {{ batchContext.calibre || '—' }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="detail" class="detail-panel" data-testid="prediction-detail">
      <div class="detail-head">
        <strong>预测点详情 · {{ detail.point.预测期 }}</strong>
        <button class="link" type="button" data-testid="close-detail" @click="detail = null">关闭</button>
      </div>
      <dl class="detail-grid">
        <div><dt>训练批次</dt><dd>{{ detail.batch.训练批次 }}</dd></div>
        <div><dt>指标编码</dt><dd>{{ detail.point.指标编码 }}</dd></div>
        <div><dt>指标值</dt><dd>{{ detail.point.展示值 }}</dd></div>
        <div><dt>精度位数</dt><dd>{{ detail.point.精度位数 }}</dd></div>
        <div><dt>指标口径</dt><dd>{{ detail.point.指标口径 }}</dd></div>
        <div><dt>对比基准</dt><dd>{{ formatValue(detail.point.对比基准, detail.point.精度位数) }}</dd></div>
        <div><dt>差异</dt><dd>{{ formatValue(detail.point.差异, detail.point.精度位数) }}</dd></div>
        <div><dt>口径核对</dt><dd>{{ detail.displayCheck.ok ? '展示值一致' : `展示值不一致（应为 ${detail.displayCheck.expected}）` }}</dd></div>
      </dl>
      <p class="detail-note">
        详情读取的是同一训练批次的留档结果（{{ detail.batch.状态 }} / {{ detail.batch.完成时间 || '未完成' }}），不会重新生成。
      </p>
    </div>

    <form class="analysis-form" @submit.prevent="retrain">
      <label><span>模型id</span><input v-model="retrainForm.modelId" data-testid="retrain-model" placeholder="例如 1" /></label>
      <label><span>业务周期</span><input v-model="retrainForm.period" data-testid="retrain-period" placeholder="例如 2026-11" /></label>
      <label><span>预测点数</span><input v-model="retrainForm.count" data-testid="retrain-count" placeholder="例如 6" /></label>
      <label style="justify-content:flex-end">
        <span>失败演练</span>
        <span><input v-model="retrainForm.simulateFailure" data-testid="retrain-fail" type="checkbox" /> 模拟训练失败</span>
      </label>
      <button class="btn primary" type="submit" data-testid="retrain">重新训练</button>
      <span v-if="retrainMessage" data-testid="retrain-message" :class="retrainOk ? 'trend-up' : 'error-text'">{{ retrainMessage }}</span>
    </form>

    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <select v-if="field.includes('方法')||field.includes('策略')||field.includes('粒度')" v-model="params[field]"><option v-for="o in options[field]" :key="o" :value="o">{{ o }}</option></select>
        <input v-else v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>
    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { request } from '@/api/client'

type Point = {
  id: number
  预测期: string
  模型编号: string
  预测对象: string
  训练批次: string
  指标编码: string
  指标值: number
  对比基准: number
  差异: number
  展示值: string
  精度位数: number
  指标口径: string
}

type PredictionPayload = {
  items: Point[]
  total: number
  trainingBatch?: string
  metricCode?: string
  precision?: number | null
  calibre?: string
  policyLabel?: string
  message?: string
  conflict?: boolean
  comparison?: { 训练批次: string; 指标口径: string; 平均展示值: string; 差异展示值: string; 预测点: Point[] } | null
  batch?: { 完成时间?: string; 状态?: string } | null
}

const ENDPOINT = '/api/forecast_model'
const route = useRoute()
const columns = ['预测期', '模型编号', '预测对象', '训练批次', '指标编码', '展示值', '对比基准', '差异', '指标口径']
const filterFields = ['模型编号', '预测对象', '业务周期', '训练批次', '指标编码']
const paramFields = ['模型编号', '预测对象', '历史窗口', '预测周期']
const options: Record<string, string[]> = { '统计粒度': ['按日', '按周', '按月'], '分组字段': ['全量', '按类型', '按区域'], '比较方法': ['环比', '同比', '绝对值'] }

const payload = ref<PredictionPayload | null>(null)
const summaryMetrics = ref<{ label: string; value: number }[]>([])
const filters = ref<Record<string, string>>({})
const policy = ref('latest_complete')
const showComparison = ref(false)
const detail = ref<any>(null)
const errorMessage = ref('')
const loading = ref(false)
const params = ref<Record<string, string>>({})
const chartData = ref<{ label: string; value: number }[]>([])
const retrainForm = ref({ modelId: '1', period: '', count: '6', simulateFailure: false })
const retrainMessage = ref('')
const retrainOk = ref(false)

const rows = computed(() => payload.value?.items ?? [])
const batchContext = computed<Partial<PredictionPayload>>(() => payload.value ?? {})

function formatValue(value: unknown, precision: number | null | undefined) {
  const digits = Number.isInteger(precision) ? Number(precision) : 3
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : '—'
}

function filterQuery() {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(filters.value)) {
    if (value?.trim()) query.set(key, value.trim())
  }
  query.set('policy', policy.value)
  return query
}

function resetFilters() {
  filters.value = {}
  detail.value = null
  showComparison.value = false
  void reload()
}

async function fetchSummary() {
  try {
    const res = await request(`${ENDPOINT}/summary`)
    if (!res.ok) return
    summaryMetrics.value = (await res.json()).metrics || []
  } catch {
    summaryMetrics.value = []
  }
}

async function reload() {
  loading.value = true
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/predictions?${filterQuery().toString()}`)
    if (!res.ok) throw new Error('预测结果读取失败')
    payload.value = await res.json()
    // 切换筛选后旧详情必须失效，避免详情与列表使用不同批次。
    detail.value = null
    showComparison.value = false
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预测结果读取失败'
    payload.value = null
  } finally {
    loading.value = false
  }
}

async function openDetail(row: Point) {
  try {
    const query = new URLSearchParams({ batch: row.训练批次, policy: policy.value })
    const res = await request(`${ENDPOINT}/predictions/${row.id}?${query.toString()}`)
    if (!res.ok) throw new Error('预测点详情读取失败')
    detail.value = await res.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预测点详情读取失败'
    detail.value = null
  }
}

async function exportRows() {
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/export?${filterQuery().toString()}`)
    if (!res.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await res.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'forecast_model.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

async function retrain() {
  retrainMessage.value = ''
  const values: Record<string, string> = { 预测点数: retrainForm.value.count || '6' }
  if (retrainForm.value.period.trim()) values['业务周期'] = retrainForm.value.period.trim()
  if (retrainForm.value.simulateFailure) values['模拟失败'] = 'true'
  try {
    const res = await request(`${ENDPOINT}/${retrainForm.value.modelId}/retrain`, {
      method: 'POST', body: JSON.stringify({ values }),
    })
    const data = await res.json()
    retrainOk.value = Boolean(data.ok)
    retrainMessage.value = data.message
  } catch (error) {
    retrainOk.value = false
    retrainMessage.value = error instanceof Error ? error.message : '重新训练失败'
  }
  // 无论成功失败都重新读取列表：失败时这里仍然是最近一次完整批次。
  await reload()
}

async function runAnalysis() {
  try {
    const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) })
    const data = await res.json()
    chartData.value = (data.items || [])
      .map((row: any) => ({ label: String(row.分组 ?? row.id), value: Number(row.指标值) || 0 }))
      .slice(0, 6)
  } catch {
    chartData.value = []
  }
}

function barHeight(value: number) {
  const max = Math.max(...chartData.value.map((item) => item.value), 1)
  return Math.max(10, (value / max) * 140)
}

onMounted(async () => {
  // 支持从其他模块带查询条件跳转（?训练批次=…&模型编号=…），进来即定位同一批次。
  for (const field of filterFields) {
    const value = route.query[field]
    if (typeof value === 'string' && value.trim()) filters.value[field] = value.trim()
  }
  const requestedPolicy = route.query.policy
  if (typeof requestedPolicy === 'string' && requestedPolicy.trim()) policy.value = requestedPolicy.trim()
  await fetchSummary()
  await reload()
})
</script>

<style scoped>
.batch-banner {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
  font-size: 13px;
}
.batch-item strong { color: #1f2937; }
.conflict-notice {
  border: 1px solid #f59e0b;
  background: #fffbeb;
  color: #92400e;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
  font-size: 13px;
}
.comparison-list {
  margin-top: 8px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 4px 12px;
  font-size: 12px;
  color: #78350f;
}
.data-table tr.selected { background: #eff6ff; }
.detail-panel {
  margin-top: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
  padding: 12px 14px;
}
.detail-head { display: flex; justify-content: space-between; align-items: center; }
.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 8px;
  margin: 10px 0 0;
}
.detail-grid dt { font-size: 12px; color: var(--muted); }
.detail-grid dd { margin: 2px 0 0; font-size: 13px; }
.detail-note { font-size: 12px; color: var(--muted); margin: 10px 0 0; }
.filter-item select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; }
</style>
