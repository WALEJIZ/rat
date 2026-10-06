// Metrics dashboard for the current object (file / directory / repository).
import { useEffect, useMemo, useState } from 'react'
import { api, downloadExport } from '../api'
import { useApp } from '../ctx'
import { buildBody, objectLabel } from '../filters'
import { AuthorBars, OwnershipDonut, SunburstChart, TimelineChart, TreemapChart } from '../charts'
import { EmptyState, ErrorBanner, Info, Kpi, Skeleton } from '../ui'
import { classNames, fmtCompact, fmtDate, fmtFloat, fmtInt } from '../util'

function BarList({ rows, onPick, valueKey = 'churn', max }) {
  const m = max || Math.max(1, ...rows.map((r) => r[valueKey] || 0))
  return (
    <div className="bar-list">
      {rows.map((r) => (
        <div className="bar-row" key={`${r.path}:${r.is_file}`} onClick={() => onPick(r)}>
          <div className="bar-name mono" title={r.path}>
            {r.is_file ? '📄' : '📁'} {r.path || 'repository (root)'}
          </div>
          <div className="bar-track">
            <div className="bar-fill" style={{ width: `${Math.max(2, (100 * (r[valueKey] || 0)) / m)}%` }} />
          </div>
          <div className="bar-val" title={`+${fmtInt(r.added)} / −${fmtInt(r.removed)}`}>{fmtCompact(r[valueKey] || 0)}</div>
        </div>
      ))}
    </div>
  )
}

export default function Overview() {
  const { repo, filter, setFilter, toast } = useApp()
  const [data, setData] = useState(null)
  const [tree, setTree] = useState(null)
  const [files, setFiles] = useState(null)
  const [dirs, setDirs] = useState(null)
  const [error, setError] = useState(null)
  const [viz, setViz] = useState('treemap')
  const [reload, setReload] = useState(0)

  const body = useMemo(() => buildBody(filter), [filter])
  const scope = filter.objectType === 'dir' ? filter.object : null

  useEffect(() => {
    let alive = true
    setError(null)
    const jobs = [
      api.metrics(repo.id, buildBody(filter, { with_timeline: true, with_breakdown: true })).then((d) => alive && setData(d)),
      api.tree(repo.id, body, { top_files: 400 }).then((d) => alive && setTree(d)),
      api.hotspots(repo.id, body, { is_file: true, limit: 14, parent: scope }).then((d) => alive && setFiles(d.hotspots)),
      api.hotspots(repo.id, body, { is_file: false, limit: 12, parent: scope }).then((d) => alive && setDirs(d.hotspots)),
    ]
    Promise.all(jobs).catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [repo.id, filter, body, scope, reload])

  const m = data?.metrics
  const breakdown = data?.author_breakdown || []
  const pickObject = (path, isFile) => {
    setFilter({ ...filter, object: path, objectType: isFile ? 'file' : 'dir' })
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }
  const pickAuthor = (authorId) => {
    if (!authorId) return
    const already = filter.authors.includes(authorId)
    const authors = already ? filter.authors.filter((a) => a !== authorId) : [...filter.authors, authorId]
    setFilter({ ...filter, authors })
    toast.info(already ? 'Author removed from filter.' : 'Author added to filter.')
  }
  const exportAs = async (dataset, fmt) => {
    try {
      await downloadExport(repo.id, { dataset, fmt, body: buildBody(filter) })
      toast.success(`Exported ${dataset} as ${fmt.toUpperCase()}.`)
    } catch (e) {
      toast.error(e.message)
    }
  }

  if (error) {
    return <div className="page"><ErrorBanner error={error} onRetry={() => setReload((v) => v + 1)} /></div>
  }
  if (!data) {
    return (
      <div className="page col">
        <div className="grid cols-4">{[0, 1, 2, 3].map((i) => <Skeleton key={i} height={76} />)}</div>
        <Skeleton height={300} />
        <div className="grid cols-2"><Skeleton height={340} /><Skeleton height={340} /></div>
      </div>
    )
  }

  return (
    <div className="page col" style={{ gap: 14 }}>
      <div className="row wrap">
        <div>
          <h1 style={{ fontSize: 19 }}>
            {filter.objectType === 'file' ? '📄' : '📁'} <span className="mono">{objectLabel(filter)}</span>
          </h1>
          <div className="muted small" style={{ marginTop: 4 }}>
            {filter.mode === 'list'
              ? <>Manual commit set · <b>{fmtInt(data.filter.count)}</b> commits</>
              : <>Commit set H = <b>{fmtInt(data.filter.count)}</b> non-merge commits{filter.from || filter.to ? ` within ${filter.from || data.filter.first_iso?.slice(0, 10)} → ${filter.to || data.filter.last_iso?.slice(0, 10)}` : ' reachable from HEAD'}</>}
            {m?.first_iso && <> · first {fmtDate(m.first_iso)} · last {fmtDate(m.last_iso)}</>}
          </div>
        </div>
        <div className="right row wrap" style={{ gap: 6 }}>
          <button className="sm" onClick={() => exportAs('metrics', 'csv')}>⬇ metrics.csv</button>
          <button className="sm" onClick={() => exportAs('metrics', 'json')}>⬇ metrics.json</button>
          <button className="sm" onClick={() => exportAs('hotspots', 'csv')}>⬇ hotspots.csv</button>
        </div>
      </div>

      {data.filter.count === 0 && (
        <ErrorBanner error="The commit set H is empty">
          No commits match the current filters, so all metrics are zero. Widen the time range or
          clear the author filter.
        </ErrorBanner>
      )}

      <div className="grid cols-4">
        <Kpi
          label="commits |H|" value={fmtInt(data.filter.count)}
          sub={`${fmtInt(m.touches)} changes touch this object`}
          info={<>The size of the commit set: non-merge commits reachable from HEAD, restricted by the active time range or manual list.</>}
        />
        <Kpi
          label="added l⁺" value={fmtInt(m.added)} tone="pos"
          sub={`${fmtFloat(m.added / Math.max(1, data.filter.count), 1)} lines / commit`}
          info={<>&sum; l⁺(h,o) over h &isin; H — lines added to the object.</>}
        />
        <Kpi
          label="removed l⁻" value={fmtInt(m.removed)} tone="neg"
          sub={`${fmtFloat(m.removed / Math.max(1, data.filter.count), 1)} lines / commit`}
          info={<>&sum; l⁻(h,o) over h &isin; H — lines removed from the object. Deletions count their old line count here.</>}
        />
        <Kpi
          label="growth δ" value={`${m.growth >= 0 ? '+' : ''}${fmtInt(m.growth)}`}
          tone={m.growth >= 0 ? 'pos' : 'neg'} sub="δ = l⁺ − l⁻"
          info={<>Growth: <code>δ = l⁺ − l⁻</code>. Negative growth means the object shrank over H.</>}
        />
        <Kpi
          label="churn λ" value={fmtInt(m.churn)} sub="λ = l⁺ + l⁻"
          info={<>Churn: <code>λ = l⁺ + l⁻</code> — total line activity on the object.</>}
        />
        <Kpi
          label="modifications n" value={fmtInt(m.modifications)}
          sub="commits that modified this object"
          info={<>n(H,o): the number of commits in H in which the object was modified, i.e. its churn in that commit was &gt; 0.</>}
        />
        <Kpi
          label="modification frequency η" value={fmtFloat(m.modification_frequency, 3)}
          sub="η = n / |H|"
          info={<>η = n(H,o) / |H| — the share of commits in H that touched this object.</>}
        />
        <Kpi
          label="churn rate ρ" value={fmtFloat(m.churn_rate, 1)} sub="ρ = λ / |H| lines per commit"
          info={<>ρ = λ(H,o) / |H| — average line churn per commit across the whole commit set.</>}
        />
      </div>

      {data.author_metrics && (
        <div className="card tight">
          <h3>
            Selected author(s) on this object — <span className="mono">{data.author_metrics.name}</span>
          </h3>
          <div className="card-body stat-strip">
            <div><span className="v">{fmtInt(data.author_metrics.commits)}</span><span className="k">author commits in H</span></div>
            <div><span className="v">{fmtInt(data.author_metrics.modifications)}</span><span className="k">modifications n(H,o,a)</span></div>
            <div><span className="v">{fmtInt(data.author_metrics.churn)}</span><span className="k">churn λ(H,o,a)</span></div>
            <div><span className="v">{(data.author_metrics.ownership * 100).toFixed(1)}%</span><span className="k">ownership ω = λ(a) / λ(o)</span></div>
          </div>
        </div>
      )}

      <div className="card">
        <h3>
          Activity over time
          <Info>
            Buckets are daily for windows ≤ 60 days, weekly up to 4 years, monthly beyond.
            Bars show lines added (up) and removed (down); the line shows commits per bucket.
          </Info>
        </h3>
        <div className="card-body">
          {data.timeline.length ? (
            <TimelineChart data={data.timeline} />
          ) : (
            <EmptyState title="No activity in this commit set">Nothing to plot yet.</EmptyState>
          )}
        </div>
      </div>

      <div className="split">
        <div className="card">
          <div className="row">
            <h3>
              {viz === 'treemap' ? 'Treemap of the repository' : 'Sunburst of the repository'}
              <Info>
                Area is proportional to <b>churn</b> over H; colour reflects growth (green) or
                shrinkage (red). Click a directory to filter the whole dashboard to it, or a tile
                to select that path.
              </Info>
            </h3>
            <div className="card-actions">
              <button className={classNames('sm', viz === 'treemap' && 'primary')} onClick={() => setViz('treemap')}>Treemap</button>
              <button className={classNames('sm', viz === 'sunburst' && 'primary')} onClick={() => setViz('sunburst')}>Sunburst</button>
            </div>
          </div>
          <div className="card-body">
            {tree ? (
              viz === 'treemap'
                ? <TreemapChart dirs={tree.dirs} files={tree.files} onSelect={pickObject} />
                : <SunburstChart dirs={tree.dirs} files={tree.files} onSelect={pickObject} />
            ) : <Skeleton height={380} />}
            <div className="tiny faint" style={{ marginTop: 6 }}>
              Click a tile to select it · {tree ? fmtInt(tree.dirs.length) : '…'} directories · {tree ? fmtInt(tree.files.length) : '…'} top files shown
            </div>
          </div>
        </div>

        <div className="col" style={{ gap: 14 }}>
          <div className="card">
            <h3>
              Ownership of {filter.object ? 'this object' : 'the repository'}
              <Info>&omega;(H,o,a) = λ(H,o,a) / λ(H,o) — each author's share of churn on the object over H. Click a slice to filter by that author.</Info>
            </h3>
            <div className="card-body">
              {breakdown.length ? (
                <OwnershipDonut breakdown={breakdown} onSelect={pickAuthor} height={230} />
              ) : <EmptyState title="No churn in H">Churn &gt; 0 is required for ownership.</EmptyState>}
            </div>
          </div>
          <div className="card">
            <h3>Top authors by churn</h3>
            <div className="card-body">
              {breakdown.length ? (
                <AuthorBars authors={breakdown} onSelect={pickAuthor} limit={10} height={260} />
              ) : <EmptyState title="No authors in H" />}
            </div>
          </div>
        </div>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <div className="row">
            <h3>🔥 Hottest files {scope ? <span className="mono small">under {scope}/</span> : 'in the repository'}</h3>
            <div className="card-actions"><span className="tiny faint">by churn over H</span></div>
          </div>
          <div className="card-body">
            {files === null ? <Skeleton height={220} /> : files.length ? (
              <BarList rows={files} onPick={(r) => pickObject(r.path, true)} />
            ) : <EmptyState title="No file activity in H" />}
          </div>
        </div>
        <div className="card">
          <div className="row">
            <h3>📁 Busiest directories {scope ? <span className="mono small">under {scope}/</span> : 'in the repository'}</h3>
            <div className="card-actions"><span className="tiny faint">by churn over H</span></div>
          </div>
          <div className="card-body">
            {dirs === null ? <Skeleton height={220} /> : dirs.length ? (
              <BarList rows={dirs} onPick={(r) => pickObject(r.path, false)} />
            ) : <EmptyState title="No directory activity in H" />}
          </div>
        </div>
      </div>

      <div className="card">
        <h3>
          Author breakdown on this object
          <Info>Per-author metrics restricted to H and this object. ω is the ownership share; click a row to filter by that author.</Info>
        </h3>
        <div className="card-body scroll-x">
          {breakdown.length ? (
            <table className="data">
              <thead>
                <tr>
                  <th className="nosort">Author</th>
                  <th className="nosort num">Modifications</th>
                  <th className="nosort num">Added</th>
                  <th className="nosort num">Removed</th>
                  <th className="nosort num">Growth</th>
                  <th className="nosort num">Churn λ</th>
                  <th className="nosort num">Ownership ω</th>
                  <th className="nosort" />
                </tr>
              </thead>
              <tbody>
                {breakdown.slice(0, 25).map((b) => (
                  <tr key={b.author_id} className="clickable" onClick={() => pickAuthor(b.author_id)}>
                    <td>
                      <div>{b.name}</div>
                      <div className="tiny faint">{b.email}</div>
                    </td>
                    <td className="num">{fmtInt(b.modifications)}</td>
                    <td className="num num-pos">+{fmtInt(b.added)}</td>
                    <td className="num num-neg">−{fmtInt(b.removed)}</td>
                    <td className={classNames('num', b.growth >= 0 ? 'num-pos' : 'num-neg')}>{b.growth >= 0 ? '+' : ''}{fmtInt(b.growth)}</td>
                    <td className="num">{fmtInt(b.churn)}</td>
                    <td className="num">{(b.ownership * 100).toFixed(1)}%</td>
                    <td className="num">
                      <button className="sm ghost" onClick={(e) => { e.stopPropagation(); pickAuthor(b.author_id) }}>
                        {filter.authors.includes(b.author_id) ? 'Remove filter' : 'Filter'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <EmptyState title="No author activity in H" />}
        </div>
      </div>
    </div>
  )
}
