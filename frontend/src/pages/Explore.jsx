// Explorer: browse the tree, sort children by any metric, inspect a selection.
import { useEffect, useMemo, useState } from 'react'
import { api, downloadExport } from '../api'
import { useApp } from '../ctx'
import { buildBody } from '../filters'
import { TimelineChart } from '../charts'
import { EmptyState, ErrorBanner, Info, Skeleton, Th, useDebounced } from '../ui'
import { classNames, fmtCompact, fmtInt, parentPath } from '../util'

const COLUMNS = [
  ['added', 'l⁺', 'Lines added (sum over H).'],
  ['removed', 'l⁻', 'Lines removed (sum over H).'],
  ['growth', 'δ', 'Growth = l⁺ − l⁻.'],
  ['churn', 'λ', 'Churn = l⁺ + l⁻.'],
  ['modifications', 'n', 'Commits in H that modified the object (churn > 0).'],
  ['touches', 'touches', 'Total change records in H (a commit touching a path counts once).'],
]

export default function Explore() {
  const { repo, filter, setFilter, toast } = useApp()
  const [q, setQ] = useState('')
  const dq = useDebounced(q)
  const [sort, setSort] = useState('churn')
  const [order, setOrder] = useState('desc')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [detail, setDetail] = useState(null)

  // When the selected object is a file we keep browsing its parent directory.
  const dirPath = filter.objectType === 'file' ? (parentPath(filter.object) ?? '') : filter.object
  const selectedFile = filter.objectType === 'file' ? filter.object : null

  const body = useMemo(() => buildBody(filter), [filter])

  useEffect(() => {
    let alive = true
    setData(null)
    setError(null)
    api.objects(repo.id, body, { parent: dirPath, q: dq || undefined, sort, order, limit: 1000 })
      .then((d) => { if (alive) setData(d) })
      .catch((e) => { if (alive) setError(e.message) })
    return () => { alive = false }
  }, [repo.id, body, dirPath, dq, sort, order])

  useEffect(() => {
    let alive = true
    setDetail(null)
    if (!filter.object) return undefined
    api.metrics(repo.id, buildBody(filter, { with_timeline: true, with_breakdown: true }))
      .then((d) => { if (alive) setDetail(d) })
      .catch(() => {})
    return () => { alive = false }
  }, [repo.id, filter.object, filter.objectType, filter, body])

  const onSort = (k) => {
    if (sort === k) setOrder(order === 'desc' ? 'asc' : 'desc')
    else { setSort(k); setOrder(k === 'name' || k === 'path' ? 'asc' : 'desc') }
  }

  const go = (path, isFile) => {
    setFilter({ ...filter, object: path, objectType: isFile ? 'file' : 'dir' })
  }

  const segments = dirPath ? dirPath.split('/') : []
  const crumbs = [
    { label: 'repository', path: '', isFile: false },
    ...segments.map((s, i) => ({ label: s, path: segments.slice(0, i + 1).join('/'), isFile: false })),
  ]

  const exportChildren = async () => {
    try {
      await downloadExport(repo.id, { dataset: 'children', fmt: 'csv', body: buildBody({ ...filter, object: dirPath, objectType: 'dir' }) })
      toast.success('Exported the listing as CSV.')
    } catch (e) { toast.error(e.message) }
  }

  return (
    <div className="page col" style={{ gap: 14 }}>
      <div className="row wrap">
        <div className="breadcrumb">
          {crumbs.map((c, i) => (
            <span key={c.path || 'root'} className="row" style={{ gap: 2 }}>
              {i > 0 && <span className="sep">/</span>}
              <button onClick={() => go(c.path, false)}>{c.label}</button>
            </span>
          ))}
          {selectedFile && (
            <span className="row" style={{ gap: 2 }}>
              <span className="sep">/</span>
              <button className="active">📄 {selectedFile.slice(selectedFile.lastIndexOf('/') + 1)}</button>
            </span>
          )}
        </div>
        <div className="right row" style={{ gap: 6 }}>
          <button className="sm ghost" onClick={exportChildren}>⬇ children.csv</button>
        </div>
      </div>

      <div className="split">
        <div className="card">
          <div className="row">
            <h3>Contents of <span className="mono">{dirPath || 'repository (root)'}</span></h3>
            <div className="card-actions">
              <input
                type="search" placeholder="Filter children…" value={q}
                onChange={(e) => setQ(e.target.value)} style={{ width: 220 }}
              />
            </div>
          </div>
          <div className="card-body">
            {error ? <ErrorBanner error={error} /> : !data ? <Skeleton height={320} /> : (
              <>
                <div className="scroll-x">
                  <table className="data">
                    <thead>
                      <tr>
                        <Th label="Name" k="name" sort={sort} order={order} onSort={onSort} />
                        <Th label="Type" k="path" sort={sort} order={order} onSort={onSort} />
                        {COLUMNS.map(([k, label, info]) => (
                          <Th key={k} label={label} k={k} sort={sort} order={order} onSort={onSort} num info={<>{info}</>} />
                        ))}
                        <th className="nosort" />
                      </tr>
                    </thead>
                    <tbody>
                      {data.children.map((c) => {
                        const isSelected = filter.object === c.path
                        return (
                          <tr
                            key={c.path}
                            className={classNames('clickable', isSelected && 'selected')}
                            onClick={() => go(c.path, c.is_file)}
                            title={c.path}
                          >
                            <td style={{ maxWidth: 420 }}>
                              <span style={{ marginRight: 6 }}>{c.is_file ? (c.binary ? '🔢' : '📄') : '📁'}</span>
                              <span className="mono">{c.name}</span>
                              {c.is_file && c.binary && <span className="badge" style={{ marginLeft: 8 }}>binary</span>}
                            </td>
                            <td className="tiny faint">{c.is_file ? 'file' : 'directory'}</td>
                            <td className="num num-pos">+{fmtCompact(c.added)}</td>
                            <td className="num num-neg">−{fmtCompact(c.removed)}</td>
                            <td className={classNames('num', c.growth >= 0 ? 'num-pos' : 'num-neg')}>
                              {c.growth >= 0 ? '+' : ''}{fmtCompact(c.growth)}
                            </td>
                            <td className="num">{fmtInt(c.churn)}</td>
                            <td className="num">{fmtInt(c.modifications)}</td>
                            <td className="num faint">{fmtInt(c.touches)}</td>
                            <td className="num">
                              <span className="tiny faint">{c.is_file ? 'inspect' : 'open'} ›</span>
                            </td>
                          </tr>
                        )
                      })}
                      {!data.children.length && (
                        <tr><td colSpan={9}>
                          <EmptyState title="Nothing here for this commit set">
                            {dq ? 'No child matches the search.' : 'This directory has no recorded changes in H.'}
                          </EmptyState>
                        </td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
                <div className="tiny faint" style={{ marginTop: 8 }}>
                  {fmtInt(data.total)} child objects · metrics computed over H · “touches” counts change records
                </div>
              </>
            )}
          </div>
        </div>

        <div className="col" style={{ gap: 14 }}>
          {!filter.object ? (
            <EmptyState title="Select a file or directory" icon="👆">
              Click a directory to descend into it, or a file to inspect its metrics. The selection
              also drives the Overview dashboard.
            </EmptyState>
          ) : !detail ? <Skeleton height={300} /> : (
            <>
              <div className="card">
                <h3>
                  {filter.objectType === 'file' ? '📄 File' : '📁 Directory'} selection
                  <Info>The same metrics the Overview shows — restricted to the current H and author filters.</Info>
                </h3>
                <div className="card-body col" style={{ gap: 10 }}>
                  <div className="path">{filter.object}</div>
                  <div className="stat-strip">
                    <div><span className="v">{fmtInt(detail.metrics.churn)}</span><span className="k">churn λ</span></div>
                    <div><span className="v">{fmtInt(detail.metrics.modifications)}</span><span className="k">modifications n</span></div>
                    <div><span className="v num-pos">+{fmtInt(detail.metrics.added)}</span><span className="k">added</span></div>
                    <div><span className="v num-neg">−{fmtInt(detail.metrics.removed)}</span><span className="k">removed</span></div>
                    <div><span className="v">{fmtInt(detail.metrics.growth)}</span><span className="k">growth δ</span></div>
                  </div>
                  <div className="row" style={{ gap: 6 }}>
                    <button className="sm" onClick={() => go(parentPath(filter.object) ?? '', false)}>↑ parent</button>
                    <button className="sm ghost" onClick={() => setFilter({ ...filter, object: '', objectType: 'dir' })}>root</button>
                  </div>
                </div>
              </div>

              <div className="card">
                <h3>Activity in H</h3>
                <div className="card-body">
                  {detail.timeline?.length
                    ? <TimelineChart data={detail.timeline} />
                    : <EmptyState title="No activity">Nothing changed here in H.</EmptyState>}
                </div>
              </div>

              <div className="card">
                <h3>Top authors on this selection</h3>
                <div className="card-body">
                  {detail.author_breakdown?.length ? (
                    <table className="data">
                      <tbody>
                        {detail.author_breakdown.slice(0, 6).map((b) => (
                          <tr key={b.author_id}>
                            <td>{b.name}</td>
                            <td className="num">{fmtInt(b.churn)}</td>
                            <td className="num">{(b.ownership * 100).toFixed(1)}%</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : <EmptyState title="No churn" />}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
