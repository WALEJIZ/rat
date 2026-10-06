// Author identity management (.mailmap + manual merges) and per-author metrics.
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { useApp } from '../ctx'
import { buildBody } from '../filters'
import { EmptyState, ErrorBanner, Info, Modal, Skeleton, Th } from '../ui'
import { classNames, fmtInt } from '../util'

export default function Authors() {
  const { repo, filter, setFilter, toast, openTab } = useApp()
  const [rows, setRows] = useState(null)
  const [inH, setInH] = useState(null)
  const [error, setError] = useState(null)
  const [q, setQ] = useState('')
  const [sort, setSort] = useState('churn')
  const [order, setOrder] = useState('desc')
  const [selected, setSelected] = useState(() => new Set())
  const [expanded, setExpanded] = useState(() => new Set())
  const [mergeInto, setMergeInto] = useState(null)
  const [confirmMerge, setConfirmMerge] = useState(false)
  const [confirmUnmerge, setConfirmUnmerge] = useState(false)
  const [busy, setBusy] = useState(false)

  const bodyInH = useMemo(
    () => buildBody({ ...filter, object: '', objectType: 'dir', authors: [] }),
    [filter],
  )

  const load = () => {
    setError(null)
    Promise.all([
      api.authors(repo.id).then((d) => setRows(d.authors)),
      api.metrics(repo.id, { ...bodyInH, with_timeline: false, with_breakdown: true })
        .then((d) => setInH(d.author_breakdown)),
    ]).catch((e) => setError(e.message))
  }
  useEffect(load, [repo.id, bodyInH]) // eslint-disable-line react-hooks/exhaustive-deps

  const merged = useMemo(() => {
    const map = new Map((inH || []).map((b) => [b.author_id, b]))
    return (rows || []).map((r) => {
      const b = map.get(r.author_id)
      return {
        ...r,
        h_modifications: b?.modifications ?? 0,
        h_churn: b?.churn ?? 0,
        h_ownership: b?.ownership ?? 0,
      }
    })
  }, [rows, inH])

  const visible = useMemo(() => {
    const ql = q.trim().toLowerCase()
    const filtered = merged.filter(
      (r) => !ql || r.name.toLowerCase().includes(ql) || r.email.toLowerCase().includes(ql)
        || r.identities.some((i) => i.name.toLowerCase().includes(ql) || i.email.toLowerCase().includes(ql)),
    )
    const dir = order === 'desc' ? -1 : 1
    const key = {
      name: (r) => r.name.toLowerCase(),
      commits: (r) => r.commits,
      added: (r) => r.added,
      churn: (r) => r.churn,
      modifications: (r) => r.modifications,
      h_churn: (r) => r.h_churn,
      h_modifications: (r) => r.h_modifications,
      h_ownership: (r) => r.h_ownership,
      ownership: (r) => r.ownership,
    }[sort] || ((r) => r.churn)
    return [...filtered].sort((a, b) => {
      const ka = key(a); const kb = key(b)
      if (ka < kb) return -dir
      if (ka > kb) return dir
      return 0
    })
  }, [merged, q, sort, order])

  const onSort = (k) => {
    if (sort === k) setOrder(order === 'desc' ? 'asc' : 'desc')
    else { setSort(k); setOrder('desc') }
  }

  const toggle = (id) => setSelected((prev) => {
    const n = new Set(prev)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    return n
  })

  const selectedRows = merged.filter((r) => selected.has(r.author_id))
  const defaultCanonical = useMemo(
    () => selectedRows.slice().sort((a, b) => b.churn - a.churn)[0]?.author_id ?? null,
    [selectedRows],
  )

  const doMerge = async () => {
    setBusy(true)
    try {
      const into = mergeInto ?? defaultCanonical
      const members = selectedRows.map((r) => r.author_id)
      const res = await api.mergeAuthors(repo.id, members, into)
      toast.success(`Merged ${res.merged} identities into author #${res.canonical_id}.`)
      setSelected(new Set())
      setConfirmMerge(false)
      load()
    } catch (e) {
      toast.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  const doUnmerge = async () => {
    setBusy(true)
    try {
      const res = await api.unmergeAuthors(repo.id, [...selected])
      toast.success(`Unmerged ${res.unmerged} author row(s).`)
      setSelected(new Set())
      setConfirmUnmerge(false)
      load()
    } catch (e) {
      toast.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  const doMailmap = async () => {
    setBusy(true)
    try {
      const res = await api.applyMailmap(repo.id)
      toast.success(res.merged
        ? `.mailmap applied: merged ${res.merged} identities across ${res.groups} group(s).`
        : 'No new .mailmap merges were needed.')
      setSelected(new Set())
      load()
    } catch (e) {
      toast.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  const filterToAuthor = (id) => {
    const already = filter.authors.includes(id)
    setFilter({ ...filter, authors: already ? filter.authors.filter((a) => a !== id) : [id] })
    if (!already) {
      toast.info('Author filter applied — showing their commits and metrics.')
      openTab('overview')
    }
  }

  if (error) return <div className="page"><ErrorBanner error={error} onRetry={load} /></div>
  if (!rows) return <div className="page"><Skeleton height={400} /></div>

  const groups = rows.filter((r) => r.identities.length).length
  const identities = rows.reduce((s, r) => s + r.identities.length, 0)

  return (
    <div className="page col" style={{ gap: 14 }}>
      <div className="row wrap">
        <div>
          <h1 style={{ fontSize: 19 }}>Authors</h1>
          <div className="muted small" style={{ marginTop: 4 }}>
            {fmtInt(rows.length)} canonical authors · {fmtInt(groups)} merge group{groups === 1 ? '' : 's'} ·{' '}
            {fmtInt(identities)} merged identities
          </div>
        </div>
        <div className="right row" style={{ gap: 6 }}>
          <button className="sm" onClick={doMailmap} disabled={busy} title="Merge identities sharing the same .mailmap-resolved name+email">
            🧬 Apply .mailmap
          </button>
        </div>
      </div>

      <div className="banner">
        <Info>
          <b>How merging works.</b> Every distinct raw (name, email) pair is an author row.
          Merging picks a canonical identity; the brief's author metrics (n, λ and ownership ω)
          then aggregate the whole group. Emails in the repository's .mailmap are applied by git
          during analysis, so “Apply .mailmap” merges exactly the identities the file unifies.
          Merges survive re-analysis.
        </Info>
        <div className="small muted">
          Select two or more rows to merge them, or unmerge a group. Metrics below recompute over the
          current commit-set filter H; full-history figures come from the whole analysis.
        </div>
      </div>

      <div className="card">
        <div className="row wrap">
          <h3>Identity table</h3>
          <div className="card-actions">
            <input
              type="search" placeholder="Search name / email / alias…" value={q}
              onChange={(e) => setQ(e.target.value)} style={{ width: 240 }}
            />
          </div>
        </div>

        {selectedRows.length > 0 && (
          <div className="card tight" style={{ marginTop: 10, borderColor: 'var(--accent)' }}>
            <div className="row wrap">
              <b className="small">{selectedRows.length} selected</b>
              <div className="row small" style={{ gap: 6 }}>
                merge into
                <select value={mergeInto ?? defaultCanonical ?? ''} onChange={(e) => setMergeInto(Number(e.target.value))}>
                  {selectedRows.map((r) => (
                    <option key={r.author_id} value={r.author_id}>{r.name} &lt;{r.email}&gt;</option>
                  ))}
                </select>
              </div>
              <div className="right row" style={{ gap: 6 }}>
                <button className="ghost sm" onClick={() => setSelected(new Set())}>Clear</button>
                <button className="sm" onClick={() => setConfirmUnmerge(true)} disabled={busy}>Unmerge selected</button>
                <button className="primary sm" onClick={() => setConfirmMerge(true)} disabled={selectedRows.length < 2 || busy}>
                  Merge selected
                </button>
              </div>
            </div>
          </div>
        )}

        <div className="card-body scroll-x">
          <table className="data">
            <thead>
              <tr>
                <th className="nosort" style={{ width: 34 }}>
                  <input
                    type="checkbox"
                    checked={visible.length > 0 && visible.every((r) => selected.has(r.author_id))}
                    onChange={() => {
                      setSelected((prev) => {
                        const n = new Set(prev)
                        const all = visible.every((r) => n.has(r.author_id))
                        visible.forEach((r) => (all ? n.delete(r.author_id) : n.add(r.author_id)))
                        return n
                      })
                    }}
                    aria-label="Select all visible"
                  />
                </th>
                <Th label="Author" k="name" sort={sort} order={order} onSort={onSort} />
                <Th label="Commits" k="commits" sort={sort} order={order} onSort={onSort} num info="Commits authored over the full history." />
                <Th label="l⁺" k="added" sort={sort} order={order} onSort={onSort} num info="Lines added by this author across the repository (full history)." />
                <Th label="Churn (all)" k="churn" sort={sort} order={order} onSort={onSort} num info="Full-history churn across the repository root." />
                <Th label="Ownership (all)" k="ownership" sort={sort} order={order} onSort={onSort} num info="Full-history ownership ω over the repository root." />
                <Th label="n(H,root,a)" k="h_modifications" sort={sort} order={order} onSort={onSort} num info="Modifications of the repository root by this author within H." />
                <Th label="λ(H,root,a)" k="h_churn" sort={sort} order={order} onSort={onSort} num info="Author churn within the current commit set H." />
                <Th label="ω in H" k="h_ownership" sort={sort} order={order} onSort={onSort} num info="Ownership within H: λ(H,root,a) / λ(H,root)." />
                <th className="nosort" />
              </tr>
            </thead>
            <tbody>
              {visible.map((r) => (
                <FragmentRow
                  key={r.author_id}
                  row={r}
                  selected={selected.has(r.author_id)}
                  expanded={expanded.has(r.author_id)}
                  inFilter={filter.authors.includes(r.author_id)}
                  onToggle={() => toggle(r.author_id)}
                  onExpand={() => setExpanded((prev) => {
                    const n = new Set(prev)
                    if (n.has(r.author_id)) n.delete(r.author_id)
                    else n.add(r.author_id)
                    return n
                  })}
                  onFilter={() => filterToAuthor(r.author_id)}
                />
              ))}
              {!visible.length && (
                <tr><td colSpan={10}><EmptyState title="No authors match">Try a different search.</EmptyState></td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {confirmMerge && (
        <Modal
          title="Merge author identities"
          onClose={() => setConfirmMerge(false)}
          footer={(
            <>
              <button className="ghost" onClick={() => setConfirmMerge(false)}>Cancel</button>
              <button className="primary" onClick={doMerge} disabled={busy}>{busy ? 'Merging…' : 'Merge'}</button>
            </>
          )}
        >
          <p className="muted" style={{ marginTop: 0 }}>
            The following identities will be treated as one author, with metrics aggregated under{' '}
            <b>{selectedRows.find((r) => r.author_id === (mergeInto ?? defaultCanonical))?.name}</b>:
          </p>
          <ul className="small">
            {selectedRows.map((r) => (
              <li key={r.author_id}>
                {r.name} &lt;{r.email}&gt; — {fmtInt(r.commits)} commits
                {r.identities.length > 0 && <span className="faint"> (+{r.identities.length} alias{r.identities.length > 1 ? 'es' : ''})</span>}
              </li>
            ))}
          </ul>
          <p className="tiny faint">Merges can be undone with “Unmerge selected”.</p>
        </Modal>
      )}

      {confirmUnmerge && (
        <Modal
          title="Unmerge author groups"
          onClose={() => setConfirmUnmerge(false)}
          footer={(
            <>
              <button className="ghost" onClick={() => setConfirmUnmerge(false)}>Cancel</button>
              <button className="danger" onClick={doUnmerge} disabled={busy}>{busy ? 'Working…' : 'Unmerge'}</button>
            </>
          )}
        >
          <p className="muted" style={{ marginTop: 0 }}>
            This dissolves the merge groups of the {selectedRows.length} selected author(s); each
            identity becomes independent again. Metrics recompute immediately.
          </p>
        </Modal>
      )}
    </div>
  )
}

function FragmentRow({ row, selected, expanded, inFilter, onToggle, onExpand, onFilter }) {
  return (
    <>
      <tr className={classNames('clickable', selected && 'selected')} onClick={onToggle}>
        <td onClick={(e) => e.stopPropagation()}>
          <input type="checkbox" checked={selected} onChange={onToggle} aria-label={`Select ${row.name}`} />
        </td>
        <td>
          <div className="row" style={{ gap: 6 }}>
            <span>{row.name}</span>
            {row.identities.length > 0 && (
              <button
                className="badge"
                style={{ cursor: 'pointer' }}
                onClick={(e) => { e.stopPropagation(); onExpand() }}
                title="Show merged identities"
              >
                {row.identities.length} merged {expanded ? '▾' : '▸'}
              </button>
            )}
          </div>
          <div className="tiny faint">{row.email}</div>
        </td>
        <td className="num">{fmtInt(row.commits)}</td>
        <td className="num num-pos">+{fmtInt(row.added)}</td>
        <td className="num">{fmtInt(row.churn)}</td>
        <td className="num">{(row.ownership * 100).toFixed(1)}%</td>
        <td className="num">{fmtInt(row.h_modifications)}</td>
        <td className="num">{fmtInt(row.h_churn)}</td>
        <td className="num">{(row.h_ownership * 100).toFixed(1)}%</td>
        <td className="num">
          <button className="sm ghost" onClick={(e) => { e.stopPropagation(); onFilter() }}>
            {inFilter ? 'Remove filter' : 'Filter'}
          </button>
        </td>
      </tr>
      {expanded && row.identities.map((i) => (
        <tr key={`${row.author_id}-${i.email}-${i.name}`}>
          <td />
          <td className="small">
            <span className="faint">↳ alias:</span> {i.name} <span className="faint">&lt;{i.email}&gt;</span>
          </td>
          <td colSpan={8} />
        </tr>
      ))}
    </>
  )
}
