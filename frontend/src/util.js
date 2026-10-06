// Formatting + small utilities shared across the UI.

export function fmtInt(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '–'
  return Math.round(n).toLocaleString('en-US')
}

export function fmtCompact(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '–'
  const abs = Math.abs(n)
  if (abs < 1000) return String(Math.round(n))
  if (abs < 1e6) return (n / 1e3).toFixed(abs < 1e4 ? 1 : 0) + 'k'
  return (n / 1e6).toFixed(1) + 'M'
}

export function fmtPct(x, digits = 1) {
  if (x === null || x === undefined || Number.isNaN(x)) return '–'
  return `${(x * 100).toFixed(digits)}%`
}

export function fmtFloat(x, digits = 2) {
  if (x === null || x === undefined || Number.isNaN(x)) return '–'
  return x.toFixed(digits)
}

export function fmtDate(iso, withTime = false) {
  if (!iso) return '–'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const opts = { year: 'numeric', month: 'short', day: 'numeric' }
  if (withTime) { opts.hour = '2-digit'; opts.minute = '2-digit' }
  return d.toLocaleDateString('en-US', opts)
}

export function fmtDuration(seconds) {
  if (!Number.isFinite(seconds)) return '–'
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds - m * 60)
  return `${m}m ${s}s`
}

export function toISODate(unixSeconds) {
  return new Date(unixSeconds * 1000).toISOString().slice(0, 10)
}

export function dayStartUnix(dateStr) {
  // A local-midnight-ish start; good enough for range filters.
  const d = new Date(`${dateStr}T00:00:00`)
  return Math.floor(d.getTime() / 1000)
}

export function clamp(x, lo, hi) { return Math.min(hi, Math.max(lo, x)) }

export function debounce(fn, ms = 250) {
  let t
  return (...args) => {
    clearTimeout(t)
    t = setTimeout(() => fn(...args), ms)
  }
}

export function classNames(...xs) { return xs.filter(Boolean).join(' ') }

export function parentPath(path) {
  if (!path) return null
  const i = path.lastIndexOf('/')
  return i < 0 ? '' : path.slice(0, i)
}

export function baseName(path) { return path === '' ? 'repository (root)' : path.slice(path.lastIndexOf('/') + 1) }

export function pathSegments(path) {
  if (!path) return []
  return path.split('/')
}

export function fmtRangeDays(fromCt, toCt) {
  if (!fromCt || !toCt) return ''
  const days = Math.max(1, Math.round((toCt - fromCt) / 86400))
  return `${days} days`
}
