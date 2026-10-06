// App shell: hash routing, repo polling, filters, theme, toasts.
import { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import { AppCtx } from './ctx'
import { FilterBar, emptyFilter } from './filters'
import { DefinitionsModal, ErrorBanner, ProgressBar, Toasts } from './ui'
import { classNames, fmtInt } from './util'
import Home from './pages/Home'
import Overview from './pages/Overview'
import Explore from './pages/Explore'
import Authors from './pages/Authors'
import Commits from './pages/Commits'

const TABS = [
  ['overview', 'Overview'],
  ['explore', 'Explore'],
  ['authors', 'Authors'],
  ['commits', 'Commits'],
]

const LS_THEME = 'rat.theme'
const LS_FILTERS = 'rat.filters.v1'

function parseHash() {
  const h = (typeof window !== 'undefined' ? window.location.hash : '') || ''
  const m = h.match(/^#\/r\/(\d+)(?:\/(\w+))?/)
  if (m) return { view: 'repo', repoId: Number(m[1]), tab: m[2] || 'overview' }
  return { view: 'home' }
}

function loadFilters() {
  try { return JSON.parse(localStorage.getItem(LS_FILTERS)) || {} } catch { return {} }
}

export default function App() {
  const [route, setRoute] = useState(parseHash)
  const [repos, setRepos] = useState([])
  const [reposLoaded, setReposLoaded] = useState(false)
  const [filters, setFilters] = useState(loadFilters)
  const [authors, setAuthors] = useState(null)
  const [theme, setTheme] = useState(() => localStorage.getItem(LS_THEME) || 'dark')
  const [toasts, setToasts] = useState([])
  const [showDefs, setShowDefs] = useState(false)
  const toastId = useRef(0)

  // ---------- theme ----------
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem(LS_THEME, theme)
  }, [theme])

  // ---------- routing ----------
  useEffect(() => {
    const onHash = () => setRoute(parseHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const navigate = useCallback((hash) => {
    if (window.location.hash === hash) setRoute(parseHash())
    else window.location.hash = hash
  }, [])
  const openRepo = useCallback((id, tab = 'overview') => navigate(`#/r/${id}/${tab}`), [navigate])
  const openTab = useCallback((tab) => {
    if (route.view !== 'repo') return
    navigate(`#/r/${route.repoId}/${tab}`)
  }, [route, navigate])

  // ---------- toasts ----------
  const toast = useMemo(() => {
    const push = (kind, message) => {
      const id = (toastId.current += 1)
      setToasts((t) => [...t.slice(-4), { id, kind, message }])
      setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === 'error' ? 8000 : 4500)
    }
    return {
      success: (m) => push('success', m),
      error: (m) => push('error', String(m)),
      info: (m) => push('info', m),
    }
  }, [])

  // ---------- repo polling ----------
  const refreshRepos = useCallback(async () => {
    try {
      const list = await api.listRepos()
      setRepos(list)
      setReposLoaded(true)
    } catch {
      setReposLoaded(true)
    }
  }, [])

  const anyBusy = repos.some((r) => ['pending', 'cloning', 'fetching', 'analyzing'].includes(r.status))

  useEffect(() => {
    refreshRepos()
  }, [refreshRepos])

  useEffect(() => {
    if (!anyBusy) return undefined
    const t = setInterval(refreshRepos, 2000)
    return () => clearInterval(t)
  }, [anyBusy, refreshRepos])

  const repo = route.view === 'repo' ? repos.find((r) => r.id === route.repoId) : null
  const repoMissing = route.view === 'repo' && reposLoaded && !repo

  // ---------- filters per repo ----------
  const filter = useMemo(
    () => (repo ? filters[repo.id] || emptyFilter() : emptyFilter()),
    [filters, repo],
  )
  const setFilter = useCallback((next) => {
    if (route.view !== 'repo') return
    setFilters((prev) => {
      const nextFilters = { ...prev, [route.repoId]: next }
      try { localStorage.setItem(LS_FILTERS, JSON.stringify(nextFilters)) } catch { /* ignore */ }
      return nextFilters
    })
  }, [route.view, route.repoId])

  // ---------- authors for the active repo ----------
  useEffect(() => {
    let alive = true
    setAuthors(null)
    if (repo?.status === 'ready') {
      api.authors(repo.id)
        .then((d) => { if (alive) setAuthors(d.authors) })
        .catch(() => { if (alive) setAuthors([]) })
    }
    return () => { alive = false }
  }, [repo?.id, repo?.status, repo?.analyzed_at])

  // ---------- keyboard ----------
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === '?' && !e.target.matches?.('input, textarea, select')) {
        e.preventDefault()
        setShowDefs((v) => !v)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const ctx = useMemo(() => ({
    repos, reposLoaded, refreshRepos, route, navigate, openRepo, openTab,
    repo, authors, filter, setFilter, toast, theme, setTheme,
  }), [repos, reposLoaded, refreshRepos, route, navigate, openRepo, openTab, repo, authors, filter, setFilter, toast, theme])

  let body
  if (route.view === 'home') {
    body = <Home />
  } else if (repoMissing) {
    body = (
      <div className="page">
        <ErrorBanner error="This repository does not exist (it may have been deleted).">
          <button className="sm" onClick={() => navigate('#/')}>Back to repositories</button>
        </ErrorBanner>
      </div>
    )
  } else if (!repo) {
    body = <div className="page"><ProgressBar value={0.4} /></div>
  } else if (repo.status !== 'ready') {
    body = <RepoPending repo={repo} />
  } else {
    body = (
      <div className="page col" style={{ gap: 14 }}>
        <FilterBar />
        {route.tab === 'overview' && <Overview />}
        {route.tab === 'explore' && <Explore />}
        {route.tab === 'authors' && <Authors />}
        {route.tab === 'commits' && <Commits />}
      </div>
    )
  }

  return (
    <AppCtx.Provider value={ctx}>
      <div
        className="app"
        data-view={route.view}
        data-repo={route.view === 'repo' ? route.repoId : ''}
        data-tab={route.view === 'repo' ? route.tab : ''}
      >
        <header className="topbar">
          <a
            className="brand" href="#/"
            onClick={() => navigate('#/')}
            style={{ color: 'inherit', textDecoration: 'none' }}
          >
            <img className="logo" src="/favicon.svg" alt="" />
            <span>
              RAT
              <small>repository analysis tool</small>
            </span>
          </a>

          <select
            value={route.view === 'repo' ? route.repoId : ''}
            onChange={(e) => e.target.value && openRepo(Number(e.target.value))}
            style={{ maxWidth: 220 }}
            aria-label="Switch repository"
          >
            <option value="">All repositories…</option>
            {repos.filter((r) => r.status === 'ready').map((r) => (
              <option key={r.id} value={r.id}>{r.name} · {fmtInt(r.commit_count)} commits</option>
            ))}
          </select>

          {route.view === 'repo' && repo?.status === 'ready' && (
            <nav className="tabs">
              {TABS.map(([id, label]) => (
                <button
                  key={id}
                  className={classNames('tab', route.tab === id && 'active')}
                  onClick={() => openTab(id)}
                >{label}</button>
              ))}
            </nav>
          )}

          <div className="spacer" />
          <button className="ghost sm" onClick={() => setShowDefs(true)} title="Metric definitions (?)">📐 Formulas</button>
          <button
            className="ghost sm"
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            title="Toggle theme"
          >{theme === 'dark' ? '🌙' : '☀️'}</button>
        </header>

        {body}
      </div>

      <Toasts toasts={toasts} onDismiss={(id) => setToasts((t) => t.filter((x) => x.id !== id))} />
      {showDefs && <DefinitionsModal onClose={() => setShowDefs(false)} />}
    </AppCtx.Provider>
  )
}

function RepoPending({ repo }) {
  const { refreshRepos, toast } = useContext(AppCtx)
  const busy = ['pending', 'cloning', 'fetching', 'analyzing'].includes(repo.status)
  const retry = async () => {
    try {
      await api.reanalyze(repo.id)
      toast.info('Re-analysis started.')
      refreshRepos()
    } catch (e) {
      toast.error(e.message)
    }
  }
  return (
    <div className="page col" style={{ gap: 14, maxWidth: 720 }}>
      <h1 style={{ fontSize: 20 }}>{repo.name}</h1>
      {busy ? (
        <div className="card">
          <h3>Analysis in progress</h3>
          <div className="card-body col">
            <ProgressBar value={repo.progress} />
            <div className="row small muted">
              <span>{repo.message || repo.phase}</span>
              <span className="right mono">{Math.round((repo.progress || 0) * 100)}%</span>
            </div>
            <div className="tiny faint">
              Ingestion and analysis run in the background — you can leave this page and come back.
              Large repositories (100k commits) typically take well under a minute to stream.
            </div>
          </div>
        </div>
      ) : (
        <ErrorBanner error={repo.error || 'Analysis failed.'} onRetry={retry}>
          Fix the cause (e.g. the URL must be public) and re-analyze, or delete the repository from the home page.
        </ErrorBanner>
      )}
    </div>
  )
}
