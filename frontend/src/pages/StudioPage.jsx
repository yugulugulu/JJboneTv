import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { LoadingScreen } from '../components/feedback/LoadingScreen'
import { notifyUnauthorized } from '../services/httpClient'
import { createUuid } from '../utils/uuid'

const THEME_STORAGE_KEY = 'thinkai-theme-mode'
const THEME_SYNC_CHANNEL = 'thinkai-theme-sync-v1'
const STUDIO_FRAME_VERSION = 'quality-image-editor-v1'
const THEME_MODES = new Set(['dark', 'light', 'system'])
const RESOLVED_THEMES = new Set(['dark', 'light'])
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

function normalizeStudioReturnTo(value) {
  if (typeof value !== 'string') return '/projects'

  try {
    const url = new URL(value, window.location.origin)
    if (url.origin !== window.location.origin || url.hash) return '/projects'
    if (url.pathname === '/' && !url.search) return '/'
    if (url.pathname !== '/projects') return '/projects'

    const entries = [...url.searchParams.entries()]
    if (!entries.length) return '/projects'
    if (entries.length !== 1 || entries[0][0] !== 'folder' || !UUID_PATTERN.test(entries[0][1])) {
      return '/projects'
    }
    return `/projects?folder=${encodeURIComponent(entries[0][1])}`
  } catch {
    return '/projects'
  }
}

function readThemeMode(value = localStorage.getItem(THEME_STORAGE_KEY)) {
  return THEME_MODES.has(value) ? value : 'dark'
}

export function StudioPage() {
  const [readyRoute, setReadyRoute] = useState(null)
  const [frameLoaded, setFrameLoaded] = useState(false)
  const [apiStatus, setApiStatus] = useState('checking')
  const frameRef = useRef(null)
  const latestRouteRequestRef = useRef(null)
  const returnToRef = useRef('/projects')
  const location = useLocation()
  const navigate = useNavigate()
  const currentRoute = `${location.pathname}${location.search}`

  const sendThemeToStudio = useCallback((mode, resolvedOverride = null) => {
    const normalizedMode = readThemeMode(mode)
    const resolved = normalizedMode === 'system'
      ? (RESOLVED_THEMES.has(resolvedOverride)
          ? resolvedOverride
          : (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'))
      : normalizedMode
    frameRef.current?.contentWindow?.postMessage(
      { type: 'thinktv:theme', mode: normalizedMode, resolved },
      window.location.origin,
    )
  }, [])

  const sendRouteToStudio = useCallback(() => {
    const requestId = createUuid()
    const route = `${location.pathname}${location.search}`
    latestRouteRequestRef.current = { requestId, route }
    frameRef.current?.contentWindow?.postMessage(
      {
        type: 'thinktv:route',
        request_id: requestId,
        path: route,
        returnTo: returnToRef.current,
      },
      window.location.origin,
    )
  }, [location.pathname, location.search])

  useEffect(() => {
    let active = true

    fetch('/api/health', { credentials: 'include' })
      .then((response) => {
        if (active) setApiStatus(response.ok ? 'connected' : 'error')
      })
      .catch(() => {
        if (active) setApiStatus('error')
      })

    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    const navigateFromStudio = (path, replace = false, returnTo = null) => {
      if (typeof path !== 'string') return

      const url = new URL(path, window.location.origin)
      const isStudioRoute =
        url.origin === window.location.origin &&
        (url.pathname === '/' ||
          url.pathname === '/projects' ||
          url.pathname === '/settings/models' ||
          /^\/projects\/[0-9a-f-]+$/i.test(url.pathname))

      if (!isStudioRoute) return
      if (/^\/projects\/[0-9a-f-]+$/i.test(url.pathname)) {
        returnToRef.current = normalizeStudioReturnTo(returnTo)
      }
      navigate(`${url.pathname}${url.search}`, { replace: Boolean(replace) })
    }

    const handleStudioMessage = (event) => {
      if (event.origin !== window.location.origin || event.source !== frameRef.current?.contentWindow) return

      if (event.data?.type === 'thinktv:unauthorized') {
        notifyUnauthorized({ source: 'studio-frame', code: event.data?.code || null })
      }

      if (event.data?.type === 'thinktv:navigate') {
        navigateFromStudio(event.data.path, event.data.replace, event.data.returnTo)
      }

      if (
        event.data?.type === 'thinktv:route-ready' &&
        event.data.request_id === latestRouteRequestRef.current?.requestId
      ) {
        setReadyRoute(latestRouteRequestRef.current.route)
      }
    }

    window.__thinktvNavigate = navigateFromStudio
    window.addEventListener('message', handleStudioMessage)
    return () => {
      delete window.__thinktvNavigate
      window.removeEventListener('message', handleStudioMessage)
    }
  }, [navigate])

  useEffect(() => {
    if (!frameLoaded) return
    sendRouteToStudio()
  }, [frameLoaded, sendRouteToStudio])

  useEffect(() => {
    const handleThemeStorage = (event) => {
      if (event.key !== THEME_STORAGE_KEY) return
      sendThemeToStudio(readThemeMode(event.newValue))
    }

    window.addEventListener('storage', handleThemeStorage)
    return () => window.removeEventListener('storage', handleThemeStorage)
  }, [sendThemeToStudio])

  useEffect(() => {
    if (typeof BroadcastChannel === 'undefined') return undefined
    const channel = new BroadcastChannel(THEME_SYNC_CHANNEL)
    channel.onmessage = (event) => {
      const message = event.data
      if (
        message?.type !== 'thinktv:theme' ||
        !THEME_MODES.has(message.mode) ||
        !RESOLVED_THEMES.has(message.resolved)
      ) return
      sendThemeToStudio(message.mode, message.resolved)
    }
    return () => channel.close()
  }, [sendThemeToStudio])

  const handleFrameLoad = () => {
    setFrameLoaded(true)
    sendThemeToStudio(readThemeMode())
  }

  return (
    <main className="studio-shell" data-api-status={apiStatus}>
      {readyRoute !== currentRoute && <LoadingScreen />}
      <iframe
        key={STUDIO_FRAME_VERSION}
        ref={frameRef}
        className="studio-frame"
        src={`/studio.html?v=${STUDIO_FRAME_VERSION}`}
        title="JJboneTv 创作工作台"
        onLoad={handleFrameLoad}
      />
    </main>
  )
}
