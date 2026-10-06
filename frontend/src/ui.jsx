// Shared UI building blocks: KPIs, modals, toasts, badges, formulas.
import { useEffect, useRef, useState } from 'react'
import { classNames } from './util'

// ---------- formula reference (straight from the brief) ----------

export const DEFINITIONS = [
  {
    title: 'File metrics',
    rows: [
      ['l⁺(h,f) / l⁻(h,f)', 'lines added / removed by commit h in file f'],
      ['growth δ', 'δ = l⁺ − l⁻'],
      ['churn λ', 'λ = l⁺ + l⁻'],
    ],
  },
  {
    title: 'Directory metrics',
    rows: [
      ['metric(d)', 'For a directory d, its metric is the sum over its immediate children (files and sub-directories), which recurses to every descendant.'],
      ['repository', 'The repository metric is the metric of the root directory "".'],
    ],
  },
  {
    title: 'Commit-set metrics (object o, commit set H)',
    rows: [
      ['added / removed / growth / churn', 'sums of the per-commit values over H'],
      ['modifications n(H,o)', 'number of commits in H in which o was modified (churn > 0)'],
      ['modification frequency η', 'η = n(H,o) / |H|'],
      ['churn rate ρ', 'ρ = λ(H,o) / |H|'],
      ['|H|', 'number of commits in the set'],
    ],
  },
  {
    title: 'Author metrics (author a, object o, commit set H)',
    rows: [
      ['author modifications n(H,o,a)', 'commits of a in H in which o was modified'],
      ['author churn λ(H,o,a)', 'churn of a on o over H'],
      ['ownership ω', 'ω = λ(H,o,a) / λ(H,o) — the share of churn on o attributable to a'],
    ],
  },
  {
    title: 'Commit semantics',
    rows: [
      ['H̄', 'non-merge commits reachable from HEAD'],
      ['H(i,j)', 'commits whose committer date t satisfies i ≤ t < j'],
      ['manual list', 'an explicit set of commits chosen in the UI'],
      ['renames', 'detected at the 50% similarity threshold (git -M50%); a rename with edits is attributed to the new path'],
      ['deletions', 'a deleted file records its previous line count as removed lines on that path'],
      ['binaries', 'binary files are not measured (git reports "-" for them)'],
    ],
  },
]

export function DefinitionsModal({ onClose }) {
  return (
    <Modal title="Metric definitions" onClose={onClose} width={760}>
      <p className="muted small" style={{ marginTop: 0 }}>
        Every number in this dashboard is computed with the formulas below. Filters always
        act on the commit set H (non-merge commits reachable from HEAD).
      </p>
      <div className="grid cols-2">
        {DEFINITIONS.map((sec) => (
          <div className="card tight" key={sec.title}>
            <h3>{sec.title}</h3>
            <div className="card-body">
              {sec.rows.map(([k, v]) => (
                <div key={k} style={{ marginBottom: 6 }}>
                  <code className="mono">{k}</code>{' '}
                  <span className="small muted">{v}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Modal>
  )
}

// ---------- small pieces ----------

export function Badge({ tone = '', children }) {
  return <span className={classNames('badge', tone)}>{children}</span>
}

export function StatusBadge({ status }) {
  const map = {
    ready: ['ok', 'ready'],
    error: ['err', 'error'],
    pending: ['warn', 'queued'],
    cloning: ['warn', 'cloning'],
    fetching: ['warn', 'fetching'],
    analyzing: ['warn', 'analysing'],
  }
  const [tone, label] = map[status] || ['', status]
  return <Badge tone={tone}>{label}</Badge>
}

export function Skeleton({ height = 16, width = '100%', style }) {
  return <div className="skeleton" style={{ height, width, ...style }} />
}

export function ProgressBar({ value }) {
  const pct = Math.max(0, Math.min(1, value || 0)) * 100
  return (
    <div className="progress" role="progressbar" aria-valuenow={pct}>
      <div style={{ width: `${pct}%` }} />
    </div>
  )
}

export function Info({ children }) {
  const [open, setOpen] = useState(false)
  return (
    <span
      className="info"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
    >
      <span className="icon">?</span>
      {open && <span className="pop" onMouseLeave={() => setOpen(false)}>{children}</span>}
    </span>
  )
}

export function Kpi({ label, value, sub, tone, info, title }) {
  return (
    <div className="card tight">
      <div className="kpi">
        <span className="label">
          {label}
          {info && <Info>{info}</Info>}
        </span>
        <span className={classNames('value', tone)} title={title}>{value}</span>
        {sub && <span className="sub">{sub}</span>}
      </div>
    </div>
  )
}

export function EmptyState({ title, children, icon = '◍' }) {
  return (
    <div className="empty">
      <div style={{ fontSize: 26, marginBottom: 6 }}>{icon}</div>
      {title && <div style={{ color: 'var(--text)', fontWeight: 600, marginBottom: 4 }}>{title}</div>}
      <div className="small">{children}</div>
    </div>
  )
}

export function ErrorBanner({ error, onRetry, children }) {
  return (
    <div className="banner error">
      <div>
        <div style={{ fontWeight: 600 }}>{error}</div>
        {children && <div className="small muted" style={{ marginTop: 4 }}>{children}</div>}
      </div>
      {onRetry && <button className="sm right" onClick={onRetry}>Retry</button>}
    </div>
  )
}

export function Modal({ title, onClose, children, footer, width }) {
  const ref = useRef(null)
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose?.() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose?.() }}
    >
      <div className="modal" ref={ref} style={width ? { maxWidth: width } : undefined}>
        <div className="row" style={{ marginBottom: 12 }}>
          <h2>{title}</h2>
          <button className="ghost sm right" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {children}
        {footer && <div className="row" style={{ marginTop: 14, justifyContent: 'flex-end' }}>{footer}</div>}
      </div>
    </div>
  )
}

export function Toasts({ toasts, onDismiss }) {
  return (
    <div className="toasts">
      {toasts.map((t) => (
        <div key={t.id} className={classNames('toast', t.kind)} onClick={() => onDismiss(t.id)}>
          {t.message}
        </div>
      ))}
    </div>
  )
}

// Sortable table header cell.
export function Th({ label, k, sort, order, onSort, num, info, nosort, style }) {
  const active = sort === k
  return (
    <th
      className={classNames(num && 'num', nosort && 'nosort')}
      style={style}
      onClick={nosort ? undefined : () => onSort(k)}
      title={nosort ? undefined : 'Click to sort'}
    >
      {label}
      {info && <Info>{info}</Info>}
      {active && <span className="arrow">{order === 'desc' ? ' ↓' : ' ↑'}</span>}
    </th>
  )
}

export function useDebounced(value, ms = 250) {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}
