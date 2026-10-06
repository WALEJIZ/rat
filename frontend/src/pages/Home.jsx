// Repository management: add via URL (deep clone) or zip upload, list + status.
import { useRef, useState } from 'react'
import { api } from '../api'
import { useApp } from '../ctx'
import { DefinitionsModal, EmptyState, ErrorBanner, Modal, ProgressBar, Skeleton, StatusBadge } from '../ui'
import { classNames, fmtCompact, fmtDate, fmtInt } from '../util'

const SAMPLES = [
  ['cJSON', 'https://github.com/DaveGamble/cJSON.git'],
  ['Redis', 'https://github.com/redis/redis.git'],
  ['git', 'https://github.com/git/git.git'],
]

function RepoCard({ repo, onOpen, onDelete, onReanalyze, busy }) {
  const s = repo.summary || null
  const working = ['pending', 'cloning', 'fetching', 'analyzing'].includes(repo.status)
  return (
    <div className="card repo-card" onClick={() => repo.status === 'ready' && onOpen(repo.id)}>
      <div className="row">
        <div style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 8 }}>
            <strong style={{ fontSize: 15 }}>{repo.name}</strong>
            <StatusBadge status={repo.status} />
            <span className="badge">{repo.source_type === 'url' ? 'remote URL' : 'zip upload'}</span>
          </div>
          <div className="tiny faint mono" style={{ marginTop: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 420 }}>
            {repo.source}
          </div>
        </div>
        <div className="card-actions">
          {repo.status === 'ready' && <button className="sm" onClick={(e) => { e.stopPropagation(); onOpen(repo.id) }}>Open</button>}
          <button
            className="sm ghost" disabled={busy || working}
            onClick={(e) => { e.stopPropagation(); onReanalyze(repo.id) }}
            title="Re-run ingestion and analysis"
          >{working ? 'Working…' : 'Re-analyze'}</button>
          <button className="sm danger" onClick={(e) => { e.stopPropagation(); onDelete(repo) }}>Delete</button>
        </div>
      </div>

      {working && (
        <div>
          <ProgressBar value={repo.progress} />
          <div className="tiny faint" style={{ marginTop: 5 }}>
            {repo.message || repo.phase} … {Math.round((repo.progress || 0) * 100)}%
          </div>
        </div>
      )}

      {repo.status === 'error' && (
        <ErrorBanner error={repo.error || 'Analysis failed.'}>
          You can re-analyze to retry, or delete the repository.
        </ErrorBanner>
      )}

      {s && (
        <div className="stat-strip">
          <div><span className="v">{fmtInt(s.commits)}</span><span className="k">commits</span></div>
          <div><span className="v">{fmtInt(s.files)}</span><span className="k">files</span></div>
          <div><span className="v">{fmtInt(s.directories)}</span><span className="k">dirs</span></div>
          <div><span className="v">{fmtInt(s.authors)}</span><span className="k">authors</span></div>
          <div><span className="v num-pos">+{fmtCompact(s.added)}</span><span className="k">lines added</span></div>
          <div><span className="v num-neg">−{fmtCompact(s.removed)}</span><span className="k">lines removed</span></div>
          <div>
            <span className="v" style={{ color: s.growth >= 0 ? 'var(--green)' : 'var(--red)' }}>
              {s.growth >= 0 ? '+' : '−'}{fmtCompact(Math.abs(s.growth))}
            </span>
            <span className="k">growth</span>
          </div>
          <div><span className="v">{fmtCompact(s.churn)}</span><span className="k">churn</span></div>
        </div>
      )}

      {repo.status === 'ready' && (
        <div className="tiny faint">
          Analysed {fmtDate(repo.analyzed_at, true)} · HEAD <span className="mono">{(repo.head_hash || '').slice(0, 10)}</span>
        </div>
      )}
    </div>
  )
}

export default function Home() {
  const { repos, reposLoaded, refreshRepos, openRepo, toast } = useApp()
  const [url, setUrl] = useState('')
  const [adding, setAdding] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [showDefs, setShowDefs] = useState(false)
  const [confirmDel, setConfirmDel] = useState(null)
  const fileRef = useRef(null)

  const addUrl = async (raw) => {
    const value = (raw ?? url).trim()
    if (!value) { toast.error('Enter a repository URL first.'); return }
    setAdding(true)
    try {
      const r = await api.addRepoUrl(value)
      toast.success('Repository queued — deep cloning now.')
      setUrl('')
      refreshRepos()
      setTimeout(() => openRepo(r.id), 400)
    } catch (e) {
      if (e.payload?.repo_id) {
        toast.error(`${e.message} Opening the existing entry.`)
        refreshRepos()
        openRepo(e.payload.repo_id)
      } else {
        toast.error(e.message)
      }
    } finally {
      setAdding(false)
    }
  }

  const uploadZip = async (file) => {
    if (!file) return
    if (!file.name.toLowerCase().endsWith('.zip')) { toast.error('Only .zip files are supported.'); return }
    setUploading(true)
    try {
      const r = await api.addRepoZip(file)
      toast.success(`Uploaded ${file.name} — unpacking and analysing.`)
      refreshRepos()
      setTimeout(() => openRepo(r.id), 400)
    } catch (e) {
      toast.error(e.message)
    } finally {
      setUploading(false)
    }
  }

  const doDelete = async (repo) => {
    setConfirmDel(null)
    try {
      await api.deleteRepo(repo.id)
      toast.success(`Deleted ${repo.name}.`)
      refreshRepos()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const doReanalyze = async (id) => {
    try {
      await api.reanalyze(id)
      toast.info('Re-analysis started.')
      refreshRepos()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <div className="page col" style={{ gap: 16 }}>
      <div className="row wrap" style={{ justifyContent: 'space-between' }}>
        <div>
          <h1 style={{ fontSize: 22 }}>Repositories</h1>
          <div className="muted small" style={{ marginTop: 4 }}>
            Add a repository by remote URL (deep clone) or by uploading a zip of the repository
            including its <code className="mono">.git</code> directory. Analysis runs in the background.
          </div>
        </div>
        <button className="ghost sm" onClick={() => setShowDefs(true)}>📐 Metric definitions</button>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h3>Add by remote URL</h3>
          <div className="card-body col">
            <div className="row">
              <input
                type="text" placeholder="https://github.com/owner/repo.git"
                value={url} onChange={(e) => setUrl(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && addUrl()}
                style={{ flex: 1 }}
              />
              <button className="primary" onClick={() => addUrl()} disabled={adding}>
                {adding ? 'Starting…' : 'Clone & analyse'}
              </button>
            </div>
            <div className="chips">
              <span className="tiny faint">Try a sample:</span>
              {SAMPLES.map(([label, u]) => (
                <button key={label} className="chip" onClick={() => setUrl(u)} title={u}>{label}</button>
              ))}
              <span className="tiny faint">then press Clone & analyse</span>
            </div>
          </div>
        </div>

        <div className="card">
          <h3>Upload a zip</h3>
          <div className="card-body">
            <div
              className={classNames('dropzone', dragOver && 'over')}
              onClick={() => fileRef.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => { e.preventDefault(); setDragOver(false); uploadZip(e.dataTransfer.files?.[0]) }}
            >
              {uploading ? 'Uploading…' : (
                <>
                  <div style={{ fontSize: 22 }}>🗜</div>
                  <div>Drop a <b>.zip</b> here or click to browse</div>
                  <div className="tiny faint" style={{ marginTop: 4 }}>
                    The archive must contain the repository's <code className="mono">.git</code> folder.
                  </div>
                </>
              )}
            </div>
            <input
              ref={fileRef} type="file" accept=".zip,application/zip" style={{ display: 'none' }}
              onChange={(e) => { uploadZip(e.target.files?.[0]); e.target.value = '' }}
            />
          </div>
        </div>
      </div>

      {!reposLoaded ? (
        <div className="grid cols-2"><Skeleton height={150} /><Skeleton height={150} /></div>
      ) : !repos.length ? (
        <EmptyState title="No repositories yet" icon="📦">
          Add one above — everything else (metrics, filters, visualisations) unlocks the moment
          the analysis finishes.
        </EmptyState>
      ) : (
        <div className="grid cols-2">
          {repos.map((r) => (
            <RepoCard
              key={r.id} repo={r}
              busy={false}
              onOpen={openRepo}
              onDelete={(repo) => setConfirmDel(repo)}
              onReanalyze={doReanalyze}
            />
          ))}
        </div>
      )}

      {showDefs && <DefinitionsModal onClose={() => setShowDefs(false)} />}
      {confirmDel && (
        <Modal
          title={`Delete ${confirmDel.name}?`}
          onClose={() => setConfirmDel(null)}
          footer={(
            <>
              <button className="ghost" onClick={() => setConfirmDel(null)}>Cancel</button>
              <button className="danger" onClick={() => doDelete(confirmDel)}>Delete repository</button>
            </>
          )}
        >
          <p className="muted">
            All analysed metrics, author merges and the stored clone for this repository will be
            removed. This cannot be undone.
          </p>
        </Modal>
      )}
    </div>
  )
}
