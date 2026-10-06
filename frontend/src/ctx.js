import { createContext, useContext } from 'react'

// App-wide context: routing, repos, filters, toasts.
export const AppCtx = createContext(null)

export function useApp() {
  const v = useContext(AppCtx)
  if (!v) throw new Error('AppCtx missing')
  return v
}
