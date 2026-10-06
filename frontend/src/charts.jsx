// ECharts wrappers + the visual vocabulary of the dashboard.
import { useEffect, useMemo, useRef } from 'react'
import * as echarts from 'echarts'
import { fmtCompact, fmtDate, fmtInt, fmtPct } from './util'

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name)
  return (v || '').trim() || fallback
}

export function chartPalette() {
  return {
    text: cssVar('--text', '#e8edf7'),
    dim: cssVar('--text-dim', '#9aa7c0'),
    faint: cssVar('--text-faint', '#64708a'),
    border: cssVar('--border', '#23304a'),
    panel: cssVar('--panel', '#131a29'),
    accent: cssVar('--accent', '#6d8cff'),
    accent2: cssVar('--accent-2', '#28d7b0'),
    green: cssVar('--green', '#37d39b'),
    red: cssVar('--red', '#ff6b7d'),
    series: ['#6d8cff', '#28d7b0', '#ffc861', '#ff8fb1', '#8f7bff', '#4ec3ff', '#ffa07a', '#9ad36a', '#f2789f', '#5bd6d6'],
  }
}

export function Chart({ option, height = 260, onEvent, className = '' }) {
  const ref = useRef(null)
  const inst = useRef(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return undefined
    inst.current = echarts.init(el, null, { renderer: 'canvas' })
    const ro = new ResizeObserver(() => inst.current?.resize())
    ro.observe(el)
    return () => { ro.disconnect(); inst.current?.dispose(); inst.current = null }
  }, [])

  useEffect(() => {
    if (!inst.current || !option) return
    inst.current.setOption(option, true)
  }, [option])

  useEffect(() => {
    const inst0 = inst.current
    if (!inst0 || !onEvent) return undefined
    const handler = (params) => onEvent(params)
    inst0.on('click', handler)
    return () => inst0.off('click', handler)
  }, [onEvent])

  return <div ref={ref} className={className} style={{ width: '100%', height }} />
}

// ---------- hierarchy helpers ----------

export function buildTree(dirs, files, valueKey = 'churn') {
  const root = { name: 'repository', path: '', value: 0, children: [], metrics: null, isRoot: true }
  const nodes = new Map([['', root]])
  const sorted = [...(dirs || [])].sort((a, b) => a.path.split('/').length - b.path.split('/').length)
  for (const d of sorted) {
    if (d.path === '') { root.value = d[valueKey] ?? 0; root.metrics = d; continue }
    const p = d.path.slice(0, d.path.lastIndexOf('/'))
    const parent = nodes.get(p) || root
    const node = {
      name: d.path.slice(d.path.lastIndexOf('/') + 1),
      path: d.path, value: d[valueKey] ?? 0, children: [], metrics: d,
    }
    nodes.set(d.path, node)
    parent.children.push(node)
  }
  if (files) {
    for (const f of files) {
      const parent = nodes.get(f.path.slice(0, f.path.lastIndexOf('/')))
      if (!parent) continue
      parent.children.push({
        name: f.path.slice(f.path.lastIndexOf('/') + 1),
        path: f.path, value: f[valueKey] ?? 0, isFile: true, metrics: f,
      })
    }
  }
  return root
}

function metricsTooltip(metrics, extra = '') {
  if (!metrics) return extra
  return [
    `<b>${fmtInt(metrics.churn)}</b> churn · <b>${fmtInt(metrics.modifications)}</b> modifications`,
    `+${fmtInt(metrics.added)} / −${fmtInt(metrics.removed)} lines (growth ${metrics.growth >= 0 ? '+' : ''}${fmtInt(metrics.growth)})`,
    extra,
  ].filter(Boolean).join('<br/>')
}

// ---------- charts ----------

export function TimelineChart({ data, onZoomChange }) {
  const t = chartPalette()
  const option = useMemo(() => {
    const xs = data.map(d => d.t_iso?.slice(0, 10) ?? String(d.t))
    return {
      animation: false,
      grid: { left: 56, right: 56, top: 30, bottom: 46 },
      legend: { top: 0, textStyle: { color: t.dim }, icon: 'roundRect', itemWidth: 10, itemHeight: 10 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: t.panel, borderColor: t.border, textStyle: { color: t.text, fontSize: 12 },
        formatter(params) {
          const i = params[0].dataIndex
          const d = data[i]
          return `<b>${fmtDate(d.t_iso)}</b><br/>` +
            `commits: <b>${fmtInt(d.commits)}</b><br/>` +
            `added: <b style="color:${t.green}">+${fmtInt(d.added)}</b><br/>` +
            `removed: <b style="color:${t.red}">−${fmtInt(d.removed)}</b><br/>` +
            `growth: <b>${d.growth >= 0 ? '+' : ''}${fmtInt(d.growth)}</b><br/>` +
            `churn: <b>${fmtInt(d.churn)}</b> · modifications: <b>${fmtInt(d.modifications)}</b>`
        },
      },
      xAxis: {
        type: 'category', data: xs,
        axisLine: { lineStyle: { color: t.border } },
        axisLabel: { color: t.faint, fontSize: 10.5 },
      },
      yAxis: [
        {
          type: 'value', name: 'lines', nameTextStyle: { color: t.faint, fontSize: 10 },
          axisLabel: { color: t.faint, fontSize: 10.5, formatter: (v) => fmtCompact(v) },
          splitLine: { lineStyle: { color: t.border, opacity: 0.5 } },
        },
        {
          type: 'value', name: 'commits', nameTextStyle: { color: t.faint, fontSize: 10 },
          axisLabel: { color: t.faint, fontSize: 10.5, formatter: (v) => fmtCompact(v) },
          splitLine: { show: false },
        },
      ],
      dataZoom: [
        { type: 'inside', throttle: 50 },
        { type: 'slider', height: 16, bottom: 8, borderColor: t.border, textStyle: { color: t.faint, fontSize: 10 } },
      ],
      series: [
        {
          name: 'added', type: 'bar', stack: 'lines', data: data.map(d => d.added),
          itemStyle: { color: t.green, opacity: 0.85 }, barMaxWidth: 22,
        },
        {
          name: 'removed', type: 'bar', stack: 'lines', data: data.map(d => -d.removed),
          itemStyle: { color: t.red, opacity: 0.85 }, barMaxWidth: 22,
        },
        {
          name: 'commits', type: 'line', yAxisIndex: 1, data: data.map(d => d.commits),
          smooth: 0.25, symbol: 'none', lineStyle: { color: t.accent, width: 2 },
          areaStyle: { color: t.accent, opacity: 0.06 },
        },
      ],
    }
  }, [data, t])
  return <Chart option={option} height={300} className="chart-timeline" />
}

export function TreemapChart({ dirs, files, valueKey = 'churn', onSelect }) {
  const t = chartPalette()
  const option = useMemo(() => {
    const root = buildTree(dirs, files, valueKey)
    const max = Math.max(1, ...(dirs || []).map(d => d[valueKey] ?? 0))
    const colorFor = (m) => {
      if (!m) return t.accent
      const intensity = Math.min(1, Math.log10(1 + (m[valueKey] ?? 0)) / Math.log10(1 + max))
      const golden = m.growth >= 0
      const base = golden ? [55, 211, 155] : [255, 107, 125]
      const mix = 0.25 + 0.75 * intensity
      return `rgba(${base.map(c => Math.round(c * mix + 40 * (1 - mix))).join(',')},${0.28 + 0.5 * intensity})`
    }
    return {
      animation: false,
      tooltip: {
        backgroundColor: t.panel, borderColor: t.border, textStyle: { color: t.text, fontSize: 12 },
        formatter: (info) => {
          const m = info.data?.metrics
          const path = info.data?.path ?? ''
          return `<b>${path || 'repository (root)'}</b><br/>${metricsTooltip(m, 'click to drill down')}`
        },
      },
      series: [{
        type: 'treemap',
        roam: false,
        nodeClick: 'zoomToNode',
        breadcrumb: { show: true, top: 'bottom', itemStyle: { color: t.panel, borderColor: t.border, textStyle: { color: t.dim } } },
        upperLabel: { show: true, height: 22, color: t.text, fontSize: 11.5 },
        itemStyle: { borderColor: t.panel, gapWidth: 2, borderWidth: 1 },
        label: { color: t.text, fontSize: 11.5, formatter: (p) => `${p.name}` },
        levels: [
          { itemStyle: { borderWidth: 0, gapWidth: 3 } },
          { itemStyle: { borderWidth: 2, gapWidth: 2 }, upperLabel: { show: false } },
          { itemStyle: { borderWidth: 1, gapWidth: 1 } },
        ],
        data: [root],
        color: (node) => colorFor(node.data?.metrics),
      }],
    }
  }, [dirs, files, valueKey, t])
  const handle = (params) => {
    if (onSelect && params?.data?.path !== undefined && params.data.path !== '') onSelect(params.data.path, false)
  }
  return <Chart option={option} height={380} onEvent={handle} />
}

export function SunburstChart({ dirs, files, valueKey = 'churn', onSelect }) {
  const t = chartPalette()
  const option = useMemo(() => {
    const root = buildTree(dirs, files, valueKey)
    return {
      animation: false,
      tooltip: {
        backgroundColor: t.panel, borderColor: t.border, textStyle: { color: t.text, fontSize: 12 },
        formatter: (info) => `<b>${info.data?.path || 'repository (root)'}</b><br/>${metricsTooltip(info.data?.metrics)}`,
      },
      series: [{
        type: 'sunburst',
        data: [root],
        radius: ['12%', '96%'],
        sort: (a, b) => (b.value ?? 0) - (a.value ?? 0),
        emphasis: { focus: 'ancestor' },
        itemStyle: { borderColor: t.panel, borderWidth: 1.5 },
        label: { minAngle: 8, color: t.text, fontSize: 11 },
        levels: [
          {},
          { r0: '12%', r: '42%', label: { rotate: 'tangential', fontSize: 11.5 }, itemStyle: { borderWidth: 2 } },
          { r0: '42%', r: '72%', label: { rotate: 'tangential', fontSize: 10 } },
          { r0: '72%', r: '96%', label: { position: 'outside', padding: 2, fontSize: 9.5, color: t.dim } },
        ],
        color: (() => {
          let i = -1
          return () => { i += 1; return t.series[i % t.series.length] }
        })(),
      }],
    }
  }, [dirs, files, valueKey, t])
  const handle = (params) => {
    if (onSelect && params?.data?.path) onSelect(params.data.path, !!params.data.isFile)
  }
  return <Chart option={option} height={380} onEvent={handle} />
}

export function OwnershipDonut({ breakdown, onSelect, height = 240 }) {
  const t = chartPalette()
  const option = useMemo(() => {
    const top = (breakdown || []).slice(0, 9).map(b => ({
      name: b.name || b.email, value: b.churn, author_id: b.author_id, ownership: b.ownership,
    }))
    const rest = (breakdown || []).slice(9)
    if (rest.length) {
      top.push({
        name: `others (${rest.length})`,
        value: rest.reduce((s, b) => s + b.churn, 0),
        ownership: rest.reduce((s, b) => s + b.ownership, 0),
      })
    }
    return {
      animation: false,
      tooltip: {
        backgroundColor: t.panel, borderColor: t.border, textStyle: { color: t.text, fontSize: 12 },
        formatter: (p) => `<b>${p.name}</b><br/>churn: <b>${fmtInt(p.value)}</b><br/>ownership: <b>${fmtPct(p.data.ownership)}</b>`,
      },
      legend: { show: false },
      series: [{
        type: 'pie',
        radius: ['52%', '84%'],
        center: ['50%', '50%'],
        itemStyle: { borderColor: t.panel, borderWidth: 2, borderRadius: 4 },
        label: { color: t.dim, fontSize: 10.5, formatter: '{b}\n{d}%' },
        labelLine: { length: 8, length2: 8, lineStyle: { color: t.border } },
        data: top,
        color: t.series,
      }],
    }
  }, [breakdown, t])
  const handle = (params) => {
    if (onSelect && params?.data?.author_id) onSelect(params.data.author_id)
  }
  return <Chart option={option} height={height} onEvent={handle} />
}

export function AuthorBars({ authors, onSelect, metric = 'churn', limit = 12, height = 300 }) {
  const t = chartPalette()
  const option = useMemo(() => {
    const rows = (authors || []).slice(0, limit).slice().reverse()
    return {
      animation: false,
      grid: { left: 8, right: 40, top: 8, bottom: 8, containLabel: true },
      tooltip: {
        backgroundColor: t.panel, borderColor: t.border, textStyle: { color: t.text, fontSize: 12 },
        formatter: (p) => {
          const a = rows[p.dataIndex]
          return `<b>${a.name}</b> <span style="opacity:.7">${a.email}</span><br/>` +
            `commits: <b>${fmtInt(a.commits)}</b><br/>` +
            `churn: <b>${fmtInt(a.churn)}</b> (ownership ${fmtPct(a.ownership)})<br/>` +
            `+${fmtInt(a.added)} / −${fmtInt(a.removed)} · modifications: <b>${fmtInt(a.modifications)}</b>`
        },
      },
      xAxis: { type: 'value', axisLabel: { color: t.faint, fontSize: 10.5, formatter: fmtCompact }, splitLine: { lineStyle: { color: t.border, opacity: 0.5 } } },
      yAxis: {
        type: 'category', data: rows.map(a => a.name.length > 22 ? a.name.slice(0, 21) + '…' : a.name),
        axisLabel: { color: t.dim, fontSize: 11 }, axisLine: { lineStyle: { color: t.border } },
      },
      series: [{
        type: 'bar',
        data: rows.map(a => a[metric] ?? 0),
        itemStyle: { color: t.accent, borderRadius: [0, 4, 4, 0] },
        barMaxWidth: 16,
      }],
    }
  }, [authors, metric, limit, t])
  const handle = (params) => {
    if (onSelect) onSelect(authRow(authors, params, limit))
  }
  return <Chart option={option} height={height} onEvent={handle} />
}

function authRow(authors, params, limit) {
  const rows = (authors || []).slice(0, limit).slice().reverse()
  return rows[params.dataIndex]?.author_id
}

export function MiniBars({ values, color }) {
  const t = chartPalette()
  const option = useMemo(() => ({
    animation: false,
    grid: { left: 0, right: 0, top: 2, bottom: 2 },
    xAxis: { type: 'category', show: false, data: values.map((_, i) => i) },
    yAxis: { type: 'value', show: false },
    series: [{ type: 'bar', data: values, itemStyle: { color: color || t.accent }, barCategoryGap: '35%' }],
  }), [values, color, t])
  return <Chart option={option} height={46} />
}
