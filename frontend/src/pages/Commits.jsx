// Commit browser: the raw commits behind H, with manual commit-set selection.
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { useApp } from '../ctx'
import { emptyFilter, objectLabel } from '../filters'
import { EmptyState, ErrorBanner, Info, Skeleton, Th, useDebounced } from '../ui'
import { classNames, dayStartUnix, fmtCompact, fmtDate, fmtInt } from '../util'

const SELECT_ALL_CAP = 2000

export default function Commits() {
  const { repo, filter, setFilter, toast, openTab } = useApp()
  const [q, setQ] = useState('')
  const dq = useDebounced(q)
  const [page, setPage] = useState(1)
  const [perPage, setPerPage] = useState(50)
  const [sort, setSort] = useState('date_desc')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const [focused, setFocused] = useState(null)
  const [picking, setPicking] = useState(false)

  // The commit table reflects the global H (time range + author filter).
  const range = useMemo(() => ({
    from_ct: filter.mode === 'range' && filter.from ? dayStartUnix(filter.from) : undefined,
    to_ct: filter.mode === 'range' && filter.to ? dayStartUnix(filter.to) + 86400 : undefined,
    authors: filter.authors.length ? filter.authors.join(',') : undefined,
  }), [filter])

  useEffect(() => { setPage(1) }, [dq, perPage, sort, range])

  useEffect(() => {
    let alive = true
    setData(null)
    setError(null)
    api.commits(repo.id, { page, per_page: perPage, q: dq || undefined, sort, ...range })
      .then((d) => { if (alive) setData(d) })
      .catch((e) => { if (alive) setError(e.message) })
    return () => { alive = false }
  }, [repo.id, page, perPage, dq, sort, range])

  const toggle = (hash) => setSelected((prev) => {
    const n = new Set(prev)
    if (n.has(hash)) n.delete(hash)
    else n.add(hash)
    return n
  })

  const applyAsCommitSet = () => {
    if (!selected.size) { toast.error('Select commits first.'); return }
    setFilter({ ...filter, mode: 'list', commits: [...selected] })
    toast.success(`${fmtInt(selected.size)} commits applied as the commit set H.`)
    openTab('overview')
  }

  const selectAllMatching = async () => {
    setPicking(true)
    try {
      const collected = new Set(selected)
      const total = data?.total ?? 0
      const pages = Math.min(Math.ceil(Math.min(total, SELECT_ALL_CAP) / 500), 4)
      for (let p = 1; p <= pages; p += 1) {
        // eslint-disable-next-line no-await-in-loop
        const d = await api.commits(repo.id, { page: p, per_page: 500, q: dq || undefined, sort, ...range })
        d.commits.forEach((c) => collected.add(c.hash))
        if (d.commits.length < 500) break
      }
      setSelected(collected)
      toast.info(total > SELECT_ALL_CAP
        ? `Selected the first ${fmtInt(Math.min(total, collected.size))} of ${fmtInt(total)} matches.`
        : `Selected all ${fmtInt(collected.size)} matching commits.`)
    } catch (e) {
      toast.error(e.message)
    } finally {
      setPicking(false)
    }
  }

  if (error) return <div className="page"><ErrorBanner error={error} /></div>

  return (
    <div className="page col" style={{ gap: 14 }}>
      <div className="row wrap">
        <div>
          <h1 style={{ fontSize: 19 }}>Commits</h1>
          <div className="muted small" style={{ marginTop: 4 }}>
            Object scope: <span className="mono">{objectLabel(filter)}</span> ·{' '}
            {filter.mode === 'list'
              ? <>manual commit set ({fmtInt(filter.commits.length)} commits)</>
              : <>time range {filter.from || 'start'} → {filter.to || 'now'}</>}
            {filter.authors.length ? <> · {filter.authors.length} author filter(s)</> : null}
          </div>
        </div>
        <div className="right row" style={{ gap: 6 }}>
          {filter.mode === 'list' && (
            <button className="ghost sm" onClick={() => setFilter({ ...filter, mode: 'range', commits: [] })}>
              Back to time range
            </button>
          )}
          <button className="sm" onClick={() => setFilter(emptyFilter())}>Reset all filters</button>
        </div>
      </div>

      <div className="banner">
        <Info>
          Tick commits to build a <b>manual commit set</b>, then apply it: every metric, chart and
          export in the dashboard is recomputed over exactly those commits (H = your selection).
        </Info>
        <div className="small muted">
          Merge commits are excluded from H by definition — only non-merge commits reachable from
          HEAD appear here.
        </div>
      </div>

      <div className="card">
        <div className="row wrap">
          <input
            type="search" placeholder="Search subject, hash, author…" value={q}
            onChange={(e) => setQ(e.target.value)} style={{ minWidth: 260, flex: 1, maxWidth: 420 }}
          />
          <select value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="date_desc">Newest first</option>
            <option value="date_asc">Oldest first</option>
          </select>
          <select value={perPage} onChange={(e) => setPerPage(Number(e.target.value))}>
            {[25, 50, 100, 250, 500].map((n) => <option key={n} value={n}>{n} / page</option>)}
          </select>
          <div className="right row" style={{ gap: 6 }}>
            <button className="sm" onClick={selectAllMatching} disabled={picking || !data}>
              {picking ? 'Selecting…' : `Select all matching (≤${fmtInt(SELECT_ALL_CAP)})`}
            </button>
            <button className="primary sm" onClick={applyAsCommitSet} disabled={!selected.size}>
              Use {fmtInt(selected.size)} as commit set H
            </button>
          </div>
        </div>

        {(selected.size > 0 || filter.mode === 'list') && (
          <div className="row small" style={{ marginTop: 8 }}>
            <span className="chip active">{fmtInt(selected.size)} ticked in this view</span>
            {filter.mode === 'list' && <span className="chip">current H: {fmtInt(filter.commits.length)} commits</span>}
            <button className="ghost sm" onClick={() => setSelected(new Set())}>Clear ticks</button>
            {filter.mode === 'list' && (
              <button className="ghost sm" onClick={() => setSelected(new Set(filter.commits))}>Load current H into the ticks</button>
            )}
          </div>
        )}

        <div className="card-body">
          {!data ? <Skeleton height={360} /> : (
            <>
              <div className="scroll-x">
                <table className="data">
                  <thead>
                    <tr>
                      <th className="nosort" style={{ width: 34 }}>
                        <input
                          type="checkbox"
                          checked={data.commits.length > 0 && data.commits.every((c) => selected.has(c.hash))}
                          onChange={() => setSelected((prev) => {
                            const n = new Set(prev)
                            const all = data.commits.every((c) => n.has(c.hash))
                            data.commits.forEach((c) => (all ? n.delete(c.hash) : n.add(c.hash)))
                            return n
                          })}
                          aria-label="Select page"
                        />
                      </th>
                      <Th label="Hash" nosort />
                      <Th label="Date" k="date" sort={sort} order={sort === 'date_desc' ? 'desc' : 'asc'} onSort={() => setSort(sort === 'date_desc' ? 'date_asc' : 'date_desc')} />
                      <th className="nosort">Author</th>
                      <th className="nosort">Subject</th>
                      <th className="nosort num">l⁺</th>
                      <th className="nosort num">l⁻</th>
                      <th className="nosort num">churn</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.commits.map((c) => (
                      <tr
                        key={c.hash}
                        className={classNames('clickable', selected.has(c.hash) && 'selected')}
                        onClick={() => toggle(c.hash)}
                      >
                        <td onClick={(e) => e.stopPropagation()}>
                          <input type="checkbox" checked={selected.has(c.hash)} onChange={() => toggle(c.hash)} aria-label={`Select ${c.hash.slice(0, 8)}`} />
                        </td>
                        <td className="mono small">
                          <button
                            className="ghost sm mono"
                            onClick={(e) => { e.stopPropagation(); setFocused(c) }}
                            title="Show commit details"
                          >{c.hash.slice(0, 9)}</button>
                        </td>
                        <td className="small" title={c.ct_iso}>{fmtDate(c.ct_iso, true)}</td>
                        <td className="small">
                          {c.author_name}
                          <div className="tiny faint">{c.author_email}</div>
                        </td>
                        <td className="small" style={{ maxWidth: 460 }}>{c.subject}</td>
                        <td className="num num-pos">+{fmtCompact(c.added)}</td>
                        <td className="num num-neg">−{fmtCompact(c.removed)}</td>
                        <td className="num">{fmtInt(c.churn)}</td>
                      </tr>
                    ))}
                    {!data.commits.length && (
                      <tr><td colSpan={8}>
                        <EmptyState title="No commits match">
                          Adjust the search or widen the time / author filters.
                        </EmptyState>
                      </td></tr>
                    )}
                  </tbody>
                </table>
              </div>

              <div className="row" style={{ marginTop: 10 }}>
                <span className="small muted">
                  {fmtInt(data.total)} commits in this view · page {data.page} / {data.pages}
                </span>
                <div className="right row" style={{ gap: 6 }}>
                  <button className="sm" disabled={page <= 1} onClick={() => setPage(1)}>« First</button>
                  <button className="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>‹ Prev</button>
                  <button className="sm" disabled={page >= data.pages} onClick={() => setPage((p) => p + 1)}>Next ›</button>
                  <button className="sm" disabled={page >= data.pages} onClick={() => setPage(data.pages)}>Last »</button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      {focused && (
        <div className="card">
          <div className="row">
            <h3>Commit <span className="mono">{focused.hash.slice(0, 12)}</span></h3>
            <div className="card-actions">
              <button className="ghost sm" onClick={() => setFocused(null)}>✕ close</button>
            </div>
          </div>
          <div className="card-body col" style={{ gap: 8 }}>
            <div className="small">{focused.subject}</div>
            <div className="stat-strip">
              <div><span className="v tiny mono">{focused.hash}</span><span className="k">full hash</span></div>
              <div><span className="v">{fmtDate(focused.ct_iso, true)}</span><span className="k">committer date</span></div>
              <div><span className="v">{focused.author_name}</span><span className="k">author</span></div>
              <div><span className="v num-pos">+{fmtInt(focused.added)}</span><span className="k">added</span></div>
              <div><span className="v num-neg">−{fmtInt(focused.removed)}</span><span className="k">removed</span></div>
            </div>
            <div className="row" style={{ gap: 6 }}>
              <button className="sm" onClick={() => { setSelected(new Set([focused.hash])); setFilter({ ...filter, mode: 'list', commits: [focused.hash] }); toast.success('Commit set H now contains this single commit.'); openTab('overview') }}>
                Analyse only this commit
              </button>
              <span className="tiny faint">Handy for spot-checking a single commit's churn against git.</span>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
