// Commit-set / author / object filters: shared bar and the picker modals.
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import { useApp } from './ctx'
import { EmptyState, Modal, ProgressBar, Skeleton, Info, useDebounced } from './ui'
import { classNames, dayStartUnix, fmtDate, fmtInt, parentPath } from './util'

export function emptyFilter() {
  return { mode: 'range', from: '', to: '', commits: [], authors: [], object: '', objectType: 'dir' }
}

// Convert the UI filter into the API FilterBody.
export function buildBody(filter, extra = {}) {
  const body = {
    object: filter.object || '',
    object_type: filter.objectType || 'dir',
    mode: filter.mode === 'list' ? 'list' : 'range',
    from_ct: null,
    to_ct: null,
    commits: null,
    authors: null,
  }
  if (body.mode === 'range') {
    if (filter.from) body.from_ct = dayStartUnix(filter.from)
    if (filter.to) body.to_ct = dayStartUnix(filter.to) + 86400
  } else {
    body.commits = filter.commits
  }
  if (filter.authors && filter.authors.length) body.authors = filter.authors
  return { ...body, ...extra }
}

export function filterIsDefault(filter) {
  return (
    !filter.from && !filter.to && !filter.commits.length && !filter.authors.length &&
    !filter.object && filter.mode === 'range'
  )
}

export function objectLabel(filter) {
  if (!filter.object) return 'repository (root)'
  return filter.object + (filter.objectType === 'file' ? '' : '/')
}

// ---------- the filter bar ----------

export function FilterBar({ compact = false }) {
  const { repo, authors, filter, setFilter } = useApp()
  const [showCommits, setShowCommits] = useState(false)
  const [showAuthors, setShowAuthors] = useState(false)
  const [showObject, setShowObject] = useState(false)

  const patch = (p) => setFilter({ ...filter, ...p })
  const authorNames = useMemo(() => {
    const m = new Map((authors || []).map((a) => [a.author_id, a]))
    return filter.authors.map((id) => m.get(id)?.name || `#${id}`)
  }, [authors, filter.authors])

  return (
    <div className="card tight" style={{ position: 'relative' }}>
      <div className="row wrap">
        <div className="chips">
          <button
            className={classNames('chip', filter.mode === 'range' && !filter.from && !filter.to && 'active')}
            onClick={() => patch({ mode: 'range', from: '', to: '' })}
          >All history</button>
          <button
            className={classNames('chip', filter.mode === 'range' && (filter.from || filter.to) && 'active')}
            onClick={() => patch({ mode: 'range' })}
          >Time range</button>
          <button
            className={classNames('chip', filter.mode === 'list' && 'active')}
            onClick={() => patch({ mode: 'list' })}
          >Manual commit list</button>
        </div>

        {filter.mode === 'range' && (
          <div className="row" style={{ gap: 6 }}>
            <label className="row small muted" style={{ gap: 6 }}>
              from
              <input
                type="date" value={filter.from}
                onChange={(e) => patch({ from: e.target.value })}
                aria-label="From date"
              />
            </label>
            <label className="row small muted" style={{ gap: 6 }}>
              to
              <input
                type="date" value={filter.to}
                onChange={(e) => patch({ to: e.target.value })}
                aria-label="To date"
              />
            </label>
          </div>
        )}

        {filter.mode === 'list' && (
          <button onClick={() => setShowCommits(true)}>
            {filter.commits.length ? `☑ ${fmtInt(filter.commits.length)} commits selected` : 'Select commits…'}
          </button>
        )}

        <button onClick={() => setShowAuthors(true)}>
          {filter.authors.length ? `👤 ${filter.authors.length} author${filter.authors.length > 1 ? 's' : ''}` : 'Filter authors…'}
        </button>

        <button onClick={() => setShowObject(true)} className="mono" style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {filter.object ? `📁 ${objectLabel(filter)}` : '📁 repository (root)'}
        </button>

        <div className="right row" style={{ gap: 8 }}>
          {!compact && (
            <span className="tiny faint">
              H = {filter.mode === 'list' ? `manual list (${filter.commits.length})` : 'non-merge commits reachable from HEAD'}
            </span>
          )}
          {!filterIsDefault(filter) && (
            <button className="ghost sm" onClick={() => setFilter({ ...emptyFilter() })}>Reset</button>
          )}
        </div>
      </div>

      {(filter.authors.length > 0 || filter.object) && (
        <div className="chips" style={{ marginTop: 8 }}>
          {filter.object && (
            <span className="chip active">
              <span className="dot" />
              {filter.objectType === 'file' ? '📄' : '📁'} <span className="mono">{filter.object}</span>
              <button title="Clear object filter" onClick={() => patch({ object: '', objectType: 'dir' })}>✕</button>
            </span>
          )}
          {authorNames.map((name, i) => (
            <span className="chip active" key={filter.authors[i]}>
              👤 {name}
              <button
                title="Remove author"
                onClick={() => patch({ authors: filter.authors.filter((a) => a !== filter.authors[i]) })}
              >✕</button>
            </span>
          ))}
        </div>
      )}

      {showCommits && <CommitPickerModal onClose={() => setShowCommits(false)} />}
      {showAuthors && <AuthorFilterModal onClose={() => setShowAuthors(false)} />}
      {showObject && <ObjectPickerModal onClose={() => setShowObject(false)} />}
    </div>
  )
}

// ---------- commit picker ----------

export function CommitPickerModal({ onClose, applyLabel = 'Use this commit set' }) {
  const { repo, filter, setFilter, toast } = useApp()
  const [q, setQ] = useState('')
  const dq = useDebounced(q)
  const [page, setPage] = useState(1)
  const [perPage] = useState(100)
  const [sort, setSort] = useState('date_desc')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [data, setData] = useState(null)
  const [selected, setSelected] = useState(() => new Set(filter.commits))
  const [pasteMode, setPasteMode] = useState(false)
  const [pasted, setPasted] = useState('')

  useEffect(() => {
    let alive = true
    setData(null)
    api.commits(repo.id, {
      page, per_page: perPage, q: dq || undefined, sort,
      from_ct: from ? dayStartUnix(from) : undefined,
      to_ct: to ? dayStartUnix(to) + 86400 : undefined,
    })
      .then((d) => { if (alive) setData(d) })
      .catch((e) => { if (alive) toast.error(e.message) })
    return () => { alive = false }
  }, [repo.id, page, perPage, dq, sort, from, to, toast])

  const rows = data?.commits || []
  const toggle = (hash) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(hash)) next.delete(hash)
      else next.add(hash)
      return next
    })
  }
  const togglePage = () => {
    setSelected((prev) => {
      const next = new Set(prev)
      const allIn = rows.every((r) => next.has(r.hash))
      rows.forEach((r) => (allIn ? next.delete(r.hash) : next.add(r.hash)))
      return next
    })
  }
  const apply = () => {
    const hashes = [...selected].map((h) => h.toLowerCase())
    if (!hashes.length) { toast.error('Select at least one commit.'); return }
    setFilter({ ...filter, mode: 'list', commits: hashes })
    toast.success(`${fmtInt(hashes.length)} commits applied as the commit set H.`)
    onClose()
  }
  const applyPasted = () => {
    const hashes = pasted.split(/[\s,;]+/).map((s) => s.trim().toLowerCase()).filter(Boolean)
    if (!hashes.length) { toast.error('Paste at least one commit hash.'); return }
    setSelected(new Set(hashes))
    setFilter({ ...filter, mode: 'list', commits: hashes })
    toast.success(`${fmtInt(hashes.length)} pasted commits applied.`)
    onClose()
  }

  const someInPage = rows.some((r) => selected.has(r.hash))

  return (
    <Modal
      title={`Select commits — ${repo.name}`}
      onClose={onClose}
      width={980}
      footer={(
        <>
          <span className="small muted right" style={{ marginRight: 'auto' }}>
            {fmtInt(selected.size)} selected
          </span>
          <button className="ghost" onClick={() => setSelected(new Set())}>Clear</button>
          <button className="primary" onClick={apply} disabled={!selected.size}>{applyLabel}</button>
        </>
      )}
    >
      <div className="row wrap" style={{ marginBottom: 10 }}>
        <input
          type="search" placeholder="Search subject / hash / author…" value={q}
          onChange={(e) => { setQ(e.target.value); setPage(1) }} style={{ flex: 1, minWidth: 220 }}
        />
        <input type="date" value={from} onChange={(e) => { setFrom(e.target.value); setPage(1) }} aria-label="From" />
        <input type="date" value={to} onChange={(e) => { setTo(e.target.value); setPage(1) }} aria-label="To" />
        <select value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="date_desc">Newest first</option>
          <option value="date_asc">Oldest first</option>
        </select>
        <button className="ghost sm" onClick={() => setPasteMode((v) => !v)}>
          {pasteMode ? 'Hide paste box' : 'Paste hashes…'}
        </button>
      </div>

      {pasteMode && (
        <div className="col" style={{ marginBottom: 10 }}>
          <textarea
            rows={4} value={pasted} onChange={(e) => setPasted(e.target.value)}
            placeholder="Paste full or abbreviated commit hashes, separated by whitespace/newlines"
            className="mono"
          />
          <div className="row">
            <span className="tiny faint">Full hashes and unique abbreviations are both accepted.</span>
            <button className="primary sm right" onClick={applyPasted}>Apply pasted list</button>
          </div>
        </div>
      )}

      {!data ? <Skeleton height={220} /> : (
        <>
          <div className="scroll-y" style={{ maxHeight: 380, border: '1px solid var(--border-soft)', borderRadius: 10 }}>
            <table className="data">
              <thead>
                <tr>
                  <th className="nosort" style={{ width: 34 }}>
                    <input type="checkbox" checked={rows.length > 0 && someInPage && rows.every((r) => selected.has(r.hash))} onChange={togglePage} aria-label="Select page" />
                  </th>
                  <th className="nosort">Date</th>
                  <th className="nosort">Author</th>
                  <th className="nosort">Subject</th>
                  <th className="nosort num">+</th>
                  <th className="nosort num">−</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.hash} className="clickable" onClick={() => toggle(c.hash)}>
                    <td onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox" checked={selected.has(c.hash)} onChange={() => toggle(c.hash)} aria-label={`Select ${c.hash.slice(0, 8)}`} />
                    </td>
                    <td className="mono small" title={c.ct_iso}>{fmtDate(c.ct_iso)}</td>
                    <td className="small">{c.author_name}</td>
                    <td className="small" style={{ maxWidth: 420 }}>
                      <span className="mono faint">{c.hash.slice(0, 8)}</span> {c.subject}
                    </td>
                    <td className="num num-pos">+{fmtInt(c.added)}</td>
                    <td className="num num-neg">−{fmtInt(c.removed)}</td>
                  </tr>
                ))}
                {!rows.length && <tr><td colSpan={6}><EmptyState title="No commits match">Adjust the search or date range.</EmptyState></td></tr>}
              </tbody>
            </table>
          </div>
          <div className="row" style={{ marginTop: 10 }}>
            <span className="small muted">
              {fmtInt(data.total)} commits · page {data.page} / {data.pages}
            </span>
            <div className="right row" style={{ gap: 6 }}>
              <button className="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>‹ Prev</button>
              <button className="sm" disabled={page >= data.pages} onClick={() => setPage((p) => p + 1)}>Next ›</button>
            </div>
          </div>
        </>
      )}
    </Modal>
  )
}

// ---------- author filter ----------

export function AuthorFilterModal({ onClose }) {
  const { authors, filter, setFilter } = useApp()
  const [q, setQ] = useState('')
  const [selected, setSelected] = useState(() => new Set(filter.authors))
  const ql = q.trim().toLowerCase()
  const list = (authors || []).filter(
    (a) => !ql || a.name.toLowerCase().includes(ql) || a.email.toLowerCase().includes(ql),
  )
  const toggle = (id) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  const apply = () => {
    setFilter({ ...filter, authors: [...selected] })
    onClose()
  }
  return (
    <Modal
      title="Filter by author"
      onClose={onClose}
      width={620}
      footer={(
        <>
          <span className="small muted right" style={{ marginRight: 'auto' }}>{selected.size} selected</span>
          <button className="ghost" onClick={() => setSelected(new Set())}>Clear</button>
          <button className="primary" onClick={apply}>Apply author filter</button>
        </>
      )}
    >
      <input
        type="search" autoFocus placeholder="Search name or email…" value={q}
        onChange={(e) => setQ(e.target.value)} style={{ width: '100%', marginBottom: 10 }}
      />
      <div className="scroll-y" style={{ maxHeight: 380, border: '1px solid var(--border-soft)', borderRadius: 10 }}>
        <table className="data">
          <tbody>
            {list.map((a) => (
              <tr key={a.author_id} className="clickable" onClick={() => toggle(a.author_id)}>
                <td style={{ width: 34 }} onClick={(e) => e.stopPropagation()}>
                  <input type="checkbox" checked={selected.has(a.author_id)} onChange={() => toggle(a.author_id)} />
                </td>
                <td>
                  <div>{a.name}</div>
                  <div className="tiny faint">{a.email}</div>
                </td>
                <td className="num small muted">{fmtInt(a.commits)} commits</td>
                <td className="num small mono">{fmtInt(a.churn)} churn</td>
              </tr>
            ))}
            {!list.length && <tr><td><EmptyState title="No authors match">Try a different search.</EmptyState></td></tr>}
          </tbody>
        </table>
      </div>
    </Modal>
  )
}

// ---------- object picker ----------

export function ObjectPickerModal({ onClose }) {
  const { repo, filter, setFilter, toast } = useApp()
  const [q, setQ] = useState('')
  const dq = useDebounced(q)
  const [results, setResults] = useState(null)
  const inputRef = useRef(null)

  useEffect(() => { inputRef.current?.focus() }, [])
  useEffect(() => {
    let alive = true
    if (!dq) { setResults([]); return undefined }
    api.search(repo.id, dq)
      .then((d) => { if (alive) setResults(d.results) })
      .catch((e) => { if (alive) toast.error(e.message) })
    return () => { alive = false }
  }, [repo.id, dq, toast])

  const pick = (path, isFile) => {
    setFilter({ ...filter, object: path, objectType: isFile ? 'file' : 'dir' })
    onClose()
  }
  const parent = filter.object ? parentPath(filter.object) : null

  return (
    <Modal title="Choose a file or directory" onClose={onClose} width={640}>
      <input
        ref={inputRef} type="search" placeholder="Type to search every file and directory…"
        value={q} onChange={(e) => setQ(e.target.value)} style={{ width: '100%' }}
      />
      <div className="small muted" style={{ marginTop: 8, marginBottom: 4 }}>
        Quick picks:
      </div>
      <div className="chips" style={{ marginBottom: 8 }}>
        <button className="chip" onClick={() => pick('', false)}>📁 repository (root)</button>
        {parent !== null && filter.object && (
          <button className="chip" onClick={() => pick(parent, false)}>📁 {parent || 'repository (root)'} (parent)</button>
        )}
        <span className="tooltip-hint">
          <Info>
            Directories show aggregated metrics of <b>all descendants</b>; files show their own
            deltas. Rename-with-edits is attributed to the new path.
          </Info>
        </span>
      </div>
      <div className="scroll-y" style={{ maxHeight: 300, border: '1px solid var(--border-soft)', borderRadius: 10 }}>
        {results === null ? (
          <div style={{ padding: 10 }}><Skeleton height={40} /></div>
        ) : (
          <table className="data">
            <tbody>
              {results.map((r) => (
                <tr key={`${r.path}:${r.is_file}`} className="clickable" onClick={() => pick(r.path, r.is_file)}>
                  <td style={{ width: 30 }}>{r.is_file ? (r.binary ? '🔢' : '📄') : '📁'}</td>
                  <td className="path">{r.path}{!r.is_file && '/'}</td>
                  <td className="num tiny faint">{r.is_file ? (r.binary ? 'binary · not measured' : 'file') : 'directory'}</td>
                </tr>
              ))}
              {!results.length && (
                <tr><td>
                  <EmptyState title={dq ? 'No objects match' : 'Start typing to search'}>
                    {dq ? 'Try a different fragment of the path.' : 'Search covers every path that ever existed in the analysed history.'}
                  </EmptyState>
                </td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </Modal>
  )
}

// A tiny inline progress used by pages while data loads.
export function LoadProgress({ loading }) {
  if (!loading) return null
  return <ProgressBar value={0.6} />
}
