/**
 * Initiatives — web dashboard page (phone-first).
 *
 * The Hermes web dashboard (`hermes dashboard`, /initiatives) loads this file. It is the mobile surface of the
 * plugin: answer Review decisions, read initiatives and cycles, and file and move tasks from a phone browser.
 * Session launching, the ideas Inbox and session defaults stay in the Desktop page (desktop/plugin.js), which
 * talks to the gateway directly.
 *
 * Plain IIFE, no build step. React, auth and fetch come from window.__HERMES_PLUGIN_SDK__; every request goes
 * through SDK.fetchJSON so the dashboard's login (basic auth cookie, OAuth, or loopback token) applies.
 * Backend: dashboard/plugin_api.py at /api/plugins/initiatives (the same routes the Desktop page uses).
 */
(function () {
  'use strict'

  const SDK = window.__HERMES_PLUGIN_SDK__
  const PLUGINS = window.__HERMES_PLUGINS__
  if (!SDK || !PLUGINS) return

  const React = SDK.React
  const h = React.createElement
  const { useState, useEffect, useCallback, useRef } = SDK.hooks

  const NAME = 'initiatives'
  const API = '/api/plugins/initiatives'
  const REFRESH_MS = 60000
  const TAB_KEY = 'hermes.initiatives.mobile.tab'
  const PROJECT_KEY = 'hermes.initiatives.mobile.project'
  const VIEW_KEY = 'hermes.initiatives.mobile.view'
  const BOARD_STAGES = ['exploring', 'decided', 'shipping', 'measuring']
  const CARD_SPLIT = /\b(t_[0-9a-f]{8})\b/
  const OPEN = new Set(['exploring', 'decided', 'shipping', 'measuring'])
  // Results where nothing was applied: shown as a warning so a refused tap is never mistaken for success.
  const NOT_APPLIED = new Set(['stale', 'refused', 'failed', 'resolved_elsewhere'])
  const STAGE_TONE = { exploring: 'muted', decided: 'muted', shipping: 'warn', measuring: 'good', concluded: 'muted', abandoned: 'muted' }
  const FLAG = {
    invalid: ['invalid', 'bad'],
    release_unverified: ['release unverified', 'warn'],
    evaluation_due: ['evaluation due', 'warn'],
    reevaluation_due: ['re-evaluation due', 'warn'],
    verdict_due: ['verdict due', 'warn'],
    disposition_due: ['disposition due', 'warn'],
    observation_stale: ['observations stale', 'warn'],
    criteria_missing: ['no success criteria', 'muted'],
    no_next_action: ['no next action', 'warn'],
    cards_unlinked: ['cards not linked', 'warn'],
    // Outcome-loop reasons arrive as `name: detail`; the detail is shown or summarized beside the label.
    contract_gap: ['contract gap', 'warn'],
    waiting_for: ['waiting until', 'muted'],
    delivery_open: ['cards open', 'muted'],
    delivery_done: ['delivery done', 'good'],
    assessment_receipt: ['assessment', 'muted'],
    diagnostic_contract_assessment: ['assessment needed', 'warn'],
    assessment_card_without_epoch: ['assessment card lacks epoch', 'warn'],
    spot_check: ['spot check', 'muted'],
    verdict_recorded: ['verdict', 'good'],
    window_ended: ['window ended', 'warn'],
    exploring: ['exploring', 'muted'],
    review_by_passed: ['review overdue', 'bad'],
    target_date: ['target', 'muted'],
    await_evidence: ['awaiting evidence', 'muted'],
    no_delivery_record: ['no delivery record', 'muted'],
    source_unavailable: ['source unavailable', 'bad'],
    cycle_closed: ['closed', 'muted'],
    cycle_concluded: ['concluded', 'muted']
  }
  const TONE_ORDER = { bad: 0, warn: 1, good: 2, muted: 3 }

  // ---- helpers -------------------------------------------------------------------------------------------

  function cx() {
    return Array.prototype.filter.call(arguments, Boolean).join(' ')
  }

  function readLocal(key, fallback) {
    try {
      const value = window.localStorage.getItem(key)
      return value == null ? fallback : value
    } catch (_e) {
      return fallback
    }
  }

  function writeLocal(key, value) {
    try {
      window.localStorage.setItem(key, value)
    } catch (_e) {
      /* private mode */
    }
  }

  function errorText(cause) {
    const text = cause && typeof cause === 'object' && 'message' in cause ? String(cause.message) : String(cause || '')
    return text.trim() || 'Request failed'
  }

  function call(path, init) {
    const options = Object.assign({}, init || {})
    if (options.body !== undefined && typeof options.body !== 'string') {
      options.body = JSON.stringify(options.body)
      options.headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {})
    }
    return SDK.fetchJSON(API + path, options)
  }

  function day(value) {
    return value ? String(value).slice(0, 10) : ''
  }

  function localTime(iso) {
    const at = Date.parse(iso || '')
    if (!Number.isFinite(at)) return String(iso || '')
    return new Date(at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  }

  function ageDays(iso) {
    const at = Date.parse(iso || '')
    return Number.isFinite(at) ? Math.max(0, Math.floor((Date.now() - at) / 86400000)) : 0
  }

  function ago(minutes) {
    if (minutes == null) return ''
    if (minutes < 1) return 'just now'
    if (minutes < 60) return minutes + ' min ago'
    const hours = Math.floor(minutes / 60)
    if (hours < 48) return hours + ' h ago'
    return Math.floor(hours / 24) + ' days ago'
  }

  function basename(path) {
    return String(path).split('/').filter(Boolean).pop() || String(path)
  }

  // How long ago an ISO time was, in the largest whole unit: "40 min", "5 h", "3 d".
  function since(iso) {
    const at = Date.parse(iso || '')
    if (!Number.isFinite(at)) return ''
    const minutes = Math.max(0, Math.floor((Date.now() - at) / 60000))
    if (minutes < 60) return minutes + ' min'
    if (minutes < 48 * 60) return Math.floor(minutes / 60) + ' h'
    return Math.floor(minutes / 1440) + ' d'
  }

  // Kanban titles carry a machine suffix (`[epoch 2c2155bf0e5e]`); the full title stays in the tooltip.
  function cardTitle(title) {
    return String(title || '').replace(/\s*\[epoch [0-9a-f]+\]\s*$/i, '')
  }

  // Browse state for this page load: the last payload of each path, the open initiative and scroll offsets.
  // Leaving for another dashboard tab and coming back shows the same place at once while a fresh read runs.
  const memory = { project: '', initiative: '', cycle: '', scroll: new Map(), data: new Map() }

  // Restores a saved scroll offset and holds it while the page is still laying out: content still loading
  // has no height yet, and late layout (summaries measuring their clamp) moves rows that scroll anchoring then
  // follows. Holds until the offset has been reachable and steady for a moment, the user scrolls (wheel,
  // touch, key, pointer), or a cap passes. Returns a stop function with .holding().
  function holdScroll(options) {
    const { read, write, saved, watch, input } = options
    let holding = saved > 0
    let steadySince = 0
    const started = Date.now()
    const tick = () => {
      if (!holding) return
      if (Math.abs(read() - saved) > 1) write(saved)
      const now = Date.now()
      if (Math.abs(read() - saved) <= 1) steadySince = steadySince || now
      else steadySince = 0
      if ((steadySince && now - steadySince > 800) || now - started > 8000) stop()
    }
    const observer = holding && window.ResizeObserver && watch ? new window.ResizeObserver(tick) : null
    if (observer) observer.observe(watch)
    const timer = holding ? window.setInterval(tick, 50) : 0
    const events = ['touchstart', 'wheel', 'keydown', 'pointerdown']
    function stop() {
      if (!holding) return
      holding = false
      window.clearInterval(timer)
      if (observer) observer.disconnect()
      for (const name of events) input.removeEventListener(name, stop)
    }
    if (holding) for (const name of events) input.addEventListener(name, stop, { passive: true })
    stop.holding = () => holding
    return stop
  }
  const MEMORY_PATHS = 80

  function remember(path, data) {
    memory.data.delete(path)
    memory.data.set(path, data)
    if (memory.data.size > MEMORY_PATHS) memory.data.delete(memory.data.keys().next().value)
  }

  // Load one API path, refresh every minute while the page is visible, and again when the phone wakes the tab.
  // State is tagged with the path it was read for: switching project shows that path's remembered payload or
  // loading, never the previous project's payload.
  function useResource(path) {
    const [state, setState] = useState(() => ({ data: (path && memory.data.get(path)) || null, error: '', loading: Boolean(path), path }))
    const generation = useRef(0)
    const load = useCallback(() => {
      if (!path) {
        setState({ data: null, error: '', loading: false, path })
        return
      }
      const mine = ++generation.current
      setState(prev => Object.assign({}, prev, { loading: true }))
      call(path).then(
        data => {
          if (mine === generation.current) {
            remember(path, data)
            setState({ data: data, error: '', loading: false, path })
          }
        },
        cause => {
          if (mine === generation.current) {
            setState(prev => ({ data: prev.path === path ? prev.data : null, error: errorText(cause), loading: false, path }))
          }
        }
      )
    }, [path])
    useEffect(() => {
      load()
      if (!path) return undefined
      const timer = window.setInterval(() => {
        if (document.visibilityState !== 'hidden') load()
      }, REFRESH_MS)
      const onVisible = () => {
        if (document.visibilityState === 'visible') load()
      }
      document.addEventListener('visibilitychange', onVisible)
      return () => {
        // Drop a response still in flight for the path being left.
        generation.current += 1
        window.clearInterval(timer)
        document.removeEventListener('visibilitychange', onVisible)
      }
    }, [load, path])
    const fresh = state.path === path
    const cached = !fresh && path ? memory.data.get(path) || null : null
    return [{ data: fresh ? state.data : cached, error: fresh ? state.error : '', loading: state.loading || !fresh }, load]
  }

  // ---- card references -----------------------------------------------------------------------------------

  // Opens one Kanban card in a sheet over the page. Provided by the page, so any card ID on it can open.
  const CardContext = React.createContext(null)

  function CardRef(props) {
    const cards = React.useContext(CardContext)
    if (!cards) return props.id
    return h(
      'button',
      {
        type: 'button',
        className: 'ini-cardref',
        title: 'Open card ' + props.id,
        'data-ini': 'card-ref-' + props.id,
        // Card IDs sit inside tappable cards and rows: open the card, not its container.
        onClick: event => {
          event.stopPropagation()
          cards.open(props.id)
        }
      },
      props.id
    )
  }

  // Text with every Kanban card ID as a button that opens that card. Non-strings pass through unchanged.
  function linked(text) {
    if (typeof text !== 'string' || !CARD_SPLIT.test(text)) return text
    return text.split(CARD_SPLIT).map((part, i) => (i % 2 ? h(CardRef, { key: part + '-' + i, id: part }) : part))
  }

  function refList(ids) {
    return ids.reduce((out, id, i) => out.concat(i ? [', ', h(CardRef, { key: id, id })] : [h(CardRef, { key: id, id })]), [])
  }

  function CardSheet(props) {
    const [state] = useResource('/projects/' + encodeURIComponent(props.project) + '/cards/' + encodeURIComponent(props.id))
    const card = state.data
    const onClose = props.onClose
    const panelRef = useRef(null)
    // Modal: focus moves into the sheet, Tab stays inside it, the page behind is inert, and closing returns
    // focus to the card ID that opened it.
    useEffect(() => {
      const opener = document.activeElement
      const panel = panelRef.current
      const sheet = panel && panel.parentElement
      const page = sheet && sheet.parentElement
      const behind = page ? Array.from(page.children).filter(node => node !== sheet && !node.hasAttribute('inert')) : []
      for (const node of behind) node.setAttribute('inert', '')
      if (panel) panel.focus()
      const onKey = event => {
        if (event.key === 'Escape') return onClose()
        if (event.key !== 'Tab' || !panel) return
        const stops = Array.from(panel.querySelectorAll('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'))
        if (!stops.length) return event.preventDefault()
        const first = stops[0]
        const last = stops[stops.length - 1]
        if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
          event.preventDefault()
          last.focus()
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault()
          first.focus()
        }
      }
      document.addEventListener('keydown', onKey)
      return () => {
        document.removeEventListener('keydown', onKey)
        for (const node of behind) node.removeAttribute('inert')
        if (opener && opener.isConnected && opener.focus) opener.focus()
      }
    }, [onClose])
    const field = (label, value) => (value ? h('div', { key: label, className: 'ini-field' }, h('div', { className: 'ini-h4' }, label), h('div', { className: 'ini-pre' }, linked(String(value)))) : null)
    const meta = card
      ? [
          ['Board', card.board],
          ['Created', day(card.created_at)],
          ['Completed', card.completed_at ? day(card.completed_at) : ''],
          ['Branch', card.branch]
        ].filter(row => row[1])
      : []
    return h(
      'div',
      {
        className: 'ini-sheet',
        role: 'dialog',
        'aria-modal': true,
        'aria-label': card ? card.title : props.id,
        'data-ini': 'card-sheet',
        onClick: event => {
          if (event.target === event.currentTarget) onClose()
        }
      },
      h(
        'div',
        { className: 'ini-sheet__panel', ref: panelRef, tabIndex: -1 },
        h(
          'div',
          { className: 'ini-sheet__head' },
          h('strong', { className: 'ini-sheet__title' }, card ? cardTitle(card.title) : props.id),
          h(Btn, { variant: 'ghost', onClick: onClose, testId: 'card-close' }, 'Close')
        ),
        h(
          'div',
          { className: 'ini-sheet__body ini-stack', 'data-ini': 'card-detail-' + props.id },
          state.error
            ? h('div', { className: 'ini-bad' }, 'Could not read ' + props.id + ': ' + state.error)
            : !card
              ? h(Muted, null, 'Loading…')
              : [
                  h(
                    'div',
                    { key: 'head', className: 'ini-row ini-wrap' },
                    h(Pill, { tone: card.status === 'done' ? 'good' : card.status === 'blocked' ? 'bad' : 'muted' }, card.status),
                    h('code', null, card.id),
                    card.assignee ? h(Muted, null, card.assignee) : null
                  ),
                  card.pr && card.pr.url
                    ? h('a', { key: 'pr', className: 'ini-link', href: card.pr.url, target: '_blank', rel: 'noopener noreferrer' }, 'Open PR · ' + prText(card.pr))
                    : h(Muted, { key: 'pr' }, prText(card.pr)),
                  meta.length ? h('dl', { key: 'meta', className: 'ini-meta' }, meta.map(row => [h('dt', { key: row[0] + 't' }, row[0]), h('dd', { key: row[0] + 'd' }, row[1])])) : null,
                  (card.parents || []).length ? h('div', { key: 'parents' }, h('strong', null, 'Parents: '), refList(card.parents)) : null,
                  (card.children || []).length ? h('div', { key: 'children' }, h('strong', null, 'Children: '), refList(card.children)) : null,
                  field('Latest summary', card.latest_summary),
                  card.result && card.result !== card.latest_summary ? field('Result', card.result) : null,
                  field('Card', card.body),
                  (card.comments || []).length
                    ? h(
                        'div',
                        { key: 'comments', className: 'ini-stack' },
                        h('div', { className: 'ini-h4' }, 'Comments (' + card.comments.length + ')'),
                        card.comments.map((comment, i) =>
                          h('div', { key: i, className: 'ini-observation' }, h(Muted, null, comment.author + ' · ' + day(comment.created_at)), h('div', { className: 'ini-pre' }, linked(comment.body || '')))
                        )
                      )
                    : null,
                  (card.runs || []).length
                    ? h(
                        'div',
                        { key: 'runs', className: 'ini-stack' },
                        h('div', { className: 'ini-h4' }, 'Runs (' + card.runs.length + ')'),
                        card.runs.map(run => h(Muted, { key: run.id }, linked([day(run.started_at), run.profile, run.outcome || run.status, run.error].filter(Boolean).join(' · '))))
                      )
                    : null,
                  h('a', { key: 'board', className: 'ini-link', href: '/kanban', 'data-ini': 'card-open-board' }, 'Open the Kanban board ›')
                ]
        )
      )
    )
  }

  // ---- primitives ----------------------------------------------------------------------------------------

  function Pill(props) {
    return h('span', { className: cx('ini-pill', 'ini-pill--' + (props.tone || 'muted')) }, props.children)
  }

  function Flags(props) {
    // Grouped by name, worst first: thirteen `waiting_for: <date>` reasons read as one pill, not thirteen.
    const groups = new Map()
    for (const raw of props.flags || []) {
      const text = String(raw)
      const at = text.indexOf(':')
      const name = (at > 0 ? text.slice(0, at) : text).trim()
      if (!groups.has(name)) groups.set(name, [])
      if (at > 0) groups.get(name).push(text.slice(at + 1).trim())
    }
    const rows = Array.from(groups.entries())
      .map(([name, raw]) => {
        const entry = FLAG[name] || [name.replace(/_/g, ' '), 'muted']
        // Details are written for machines (`insufficient_data`, `5 of 10 cards open`): drop underscores and a repeated label.
        const tidy = d => {
          let text = d.replace(/_/g, ' ').trim()
          for (const word of [entry[0], name.replace(/_/g, ' ')]) {
            const tail = ' ' + word.toLowerCase()
            if (text.toLowerCase().endsWith(tail)) text = text.slice(0, -tail.length).trim()
          }
          return text
        }
        const details = Array.from(new Set(raw.map(tidy))).sort()
        const suffix = !details.length
          ? ''
          : details.length === 1
            ? details[0].length <= 28 ? ' ' + details[0] : ''
            : details.every(d => /^\d{4}-\d{2}-\d{2}$/.test(d))
              ? ' ' + details[0] + ' +' + (details.length - 1)
              : ' ×' + details.length
        return { name, tone: entry[1], text: entry[0] + suffix, title: details.length ? entry[0] + ': ' + details.join('; ') : entry[0] }
      })
      .sort((a, b) => TONE_ORDER[a.tone] - TONE_ORDER[b.tone])
    const max = props.max || rows.length
    const shown = rows.length > max ? rows.slice(0, max - 1) : rows
    const hidden = rows.slice(shown.length)
    return shown
      .map(row => h('span', { key: row.name, title: row.title, className: 'ini-flag' }, h(Pill, { tone: row.tone }, row.text)))
      .concat(hidden.length ? [h('span', { key: 'more', title: hidden.map(row => row.title).join('\n'), className: 'ini-flag' }, h(Pill, null, '+' + hidden.length))] : [])
  }

  function Btn(props) {
    return h(
      'button',
      {
        type: 'button',
        className: cx('ini-btn', props.variant ? 'ini-btn--' + props.variant : 'ini-btn--primary', props.block && 'ini-btn--block', props.icon && 'ini-btn--icon'),
        disabled: props.disabled,
        onClick: props.onClick,
        title: props.title,
        'aria-label': props.ariaLabel,
        'data-ini': props.testId
      },
      props.children
    )
  }

  function Muted(props) {
    return h('div', { className: cx('ini-muted', props.className) }, props.children)
  }

  function Fold(props) {
    const [open, setOpen] = useState(Boolean(props.defaultOpen))
    // A fold that must be seen (a pending confirmation) opens when that becomes true, even after a refresh.
    useEffect(() => {
      if (props.defaultOpen) setOpen(true)
    }, [props.defaultOpen])
    if (!props.count) return null
    return h(
      'section',
      { className: 'ini-fold', 'data-ini': props.testId },
      h(
        'button',
        { type: 'button', className: 'ini-fold__head', 'aria-expanded': open, onClick: () => setOpen(!open) },
        h('span', { className: 'ini-chevron', 'aria-hidden': true }, open ? '▾' : '▸'),
        h('span', { className: 'ini-fold__title' }, props.title),
        h(Pill, { tone: props.tone || 'muted' }, String(props.count))
      ),
      open ? h('div', { className: 'ini-fold__body' }, props.children) : null
    )
  }

  function Notice(props) {
    const bad = NOT_APPLIED.has(props.result.state)
    return h(
      'div',
      { className: cx('ini-notice', bad && 'ini-notice--bad'), role: 'status', 'data-ini': 'notice' },
      h(
        'div',
        { className: 'ini-notice__text' },
        h('strong', null, props.result.short_id + (bad ? ': nothing was applied' : ': done')),
        h('div', null, linked((props.result.title ? props.result.title + ' — ' : '') + (props.result.message || props.result.state)))
      ),
      h(Btn, { variant: 'ghost', onClick: props.onClose }, 'Dismiss')
    )
  }

  // ---- document preview ----------------------------------------------------------------------------------

  function DocSheet(props) {
    const [state] = useResource('/projects/' + encodeURIComponent(props.projectId) + '/doc?path=' + encodeURIComponent(props.path))
    const doc = state.data
    useEffect(() => {
      const onKey = event => {
        if (event.key === 'Escape') props.onClose()
      }
      document.addEventListener('keydown', onKey)
      return () => document.removeEventListener('keydown', onKey)
    }, [])
    return h(
      'div',
      { className: 'ini-sheet', role: 'dialog', 'aria-modal': true, 'aria-label': basename(props.path) },
      h(
        'div',
        { className: 'ini-sheet__panel' },
        h(
          'div',
          { className: 'ini-sheet__head' },
          h('strong', { className: 'ini-sheet__title' }, doc ? doc.name : basename(props.path)),
          h(Btn, { variant: 'ghost', onClick: props.onClose, testId: 'doc-close' }, 'Close')
        ),
        h(
          'div',
          { className: 'ini-sheet__body', 'data-ini': 'doc-preview' },
          state.error
            ? h('div', { className: 'ini-bad' }, state.error)
            : !doc
              ? h(Muted, null, 'Loading…')
              : [
                  h(Muted, { key: 'meta', className: 'ini-mono' }, doc.path + ' · ' + doc.bytes + ' bytes · SHA-256 ' + doc.sha256),
                  h('pre', { key: 'text', className: 'ini-doc' }, doc.text)
                ]
        )
      )
    )
  }

  function Docs(props) {
    const [open, setOpen] = useState(null)
    const docs = (props.item.links || []).filter(link => link.kind === 'doc' && link.path)
    if (!docs.length || !props.item.project) return null
    return h(
      'div',
      { className: 'ini-row ini-wrap ini-docs', 'data-ini': 'docs-' + props.item.short_id },
      h(Muted, null, 'Documents'),
      docs.map(doc =>
        h(
          Btn,
          { key: doc.path, variant: 'outline', title: doc.path, onClick: () => setOpen(doc.path), testId: 'doc-' + props.item.short_id + (docs.length > 1 ? '-' + docs.indexOf(doc) : '') },
          h('span', { className: 'ini-ellipsis' }, '📄 ' + basename(doc.path))
        )
      ),
      open ? h(DocSheet, { projectId: props.item.project, path: open, onClose: () => setOpen(null) }) : null
    )
  }

  // ---- Review --------------------------------------------------------------------------------------------

  function Summary(props) {
    const summary = props.summary
    if (!summary || (!summary.what && !summary.why)) return null
    return h(
      'div',
      { className: 'ini-summary', 'data-ini': 'summary-' + props.id },
      summary.what ? h('div', null, h('strong', null, 'What: '), linked(summary.what)) : null,
      summary.why ? h('div', null, h('strong', null, 'Why: '), linked(summary.why)) : null
    )
  }

  function Decision(props) {
    const item = props.item
    const version = item.version || {}
    const [choice, setChoice] = useState('')
    const [note, setNote] = useState('')
    const [busy, setBusy] = useState(false)
    const [result, setResult] = useState(null)
    const option = (version.options || []).find(o => o.id === choice)
    const waiting = result && result.state === 'awaiting_confirm'
    const send = (action, body) => {
      setBusy(true)
      call('/review/items/' + encodeURIComponent(item.short_id) + '/' + action, { method: 'POST', body: body || {} })
        .then(data => {
          setResult(data.result)
          if (data.result.state !== 'awaiting_confirm') props.onDone(Object.assign({}, data.result, { title: version.question || item.title }))
        })
        .catch(cause => setResult({ state: 'failed', message: errorText(cause) }))
        .finally(() => setBusy(false))
    }
    const meta = [ageDays(item.opened_at) + 'd open']
    if ((item.blocks || []).length) meta.push('blocks ' + item.blocks.length)
    if (props.initiativeTitle || item.initiative) meta.push(props.initiativeTitle || item.initiative)
    return h(
      'article',
      { className: 'ini-card', 'data-ini': 'review-' + item.short_id },
      h(
        'div',
        { className: 'ini-row ini-wrap' },
        h(Pill, { tone: item.urgent_rule ? 'bad' : 'warn' }, item.short_id),
        item.urgent_rule ? h(Pill, { tone: 'bad' }, item.urgent_rule) : null
      ),
      h('h3', { className: 'ini-question' }, linked(version.question || item.title)),
      h(Muted, null, linked(meta.join(' · '))),
      h(Summary, { summary: version.summary, id: item.short_id }),
      version.context ? h('div', { className: 'ini-context' }, linked(version.context)) : null,
      h(Docs, { item: item }),
      h(
        'div',
        { className: 'ini-options', role: 'radiogroup', 'aria-label': version.question || item.title },
        (version.options || []).map(o => {
          // The radio is its own button; card IDs named in its label are separate buttons beside it,
          // never buttons inside the radio button.
          const refs = (String(o.label || '').match(new RegExp(CARD_SPLIT.source, 'g')) || []).filter((id, i, all) => all.indexOf(id) === i)
          return h(
            'div',
            { key: o.id, className: cx('ini-option', choice === o.id && 'ini-option--picked', (busy || waiting) && 'ini-option--disabled') },
            h(
              'button',
              {
                type: 'button',
                role: 'radio',
                'aria-checked': choice === o.id,
                className: 'ini-option__hit',
                disabled: busy || waiting,
                onClick: () => setChoice(o.id),
                'data-ini': 'option-' + item.short_id + '-' + o.id
              },
              h('span', { className: 'ini-radio', 'aria-hidden': true }),
              h('span', { className: 'ini-option__id' }, o.id),
              h(
                'span',
                { className: 'ini-option__label' },
                o.label,
                o.id === version.recommend ? h(Pill, { tone: 'good' }, 'recommended') : null,
                o.confirm ? h(Pill, { tone: 'warn' }, 'needs confirm') : null,
                /^pr_merge/.test(o.action || '') ? h(Pill, { tone: 'bad' }, 'merge in GitHub') : null
              )
            ),
            refs.length ? h('span', { className: 'ini-option__refs', 'data-ini': 'option-refs-' + item.short_id + '-' + o.id }, 'Cards: ', refList(refs)) : null
          )
        })
      ),
      h('textarea', {
        className: 'ini-input',
        placeholder: 'Optional note, copied unchanged to the card or decision',
        value: note,
        rows: 2,
        disabled: busy || waiting,
        onChange: event => setNote(event.target.value),
        'data-ini': 'note-' + item.short_id
      }),
      h(
        'div',
        { className: 'ini-actions' },
        waiting
          ? h(Btn, { variant: 'danger', block: true, disabled: busy, onClick: () => send('confirm'), testId: 'confirm-' + item.short_id }, 'Confirm ' + choice + ' (cannot be undone)')
          : h(
              Btn,
              { block: true, disabled: busy || !option, onClick: () => send('answer', { option: choice, note: note }), testId: 'apply-' + item.short_id },
              busy ? 'Applying…' : choice ? 'Apply ' + choice : 'Pick an option'
            ),
        waiting
          ? null
          : h(
              'div',
              { className: 'ini-row' },
              h(Btn, { variant: 'outline', disabled: busy, onClick: () => send('defer', { kind: 'state_change', reason: note }), testId: 'defer-' + item.short_id }, 'Defer until it changes'),
              h(Btn, { variant: 'outline', disabled: busy, onClick: () => send('auto', { note: note }), testId: 'auto-' + item.short_id }, 'Let agents handle it')
            )
      ),
      result ? h('div', { className: cx('ini-result', NOT_APPLIED.has(result.state) && 'ini-bad'), 'data-ini': 'result-' + item.short_id }, linked(result.state + ': ' + (result.message || ''))) : null
    )
  }

  // A one-line item with one action: hand it to the agents (Auto), take an auto item back, or confirm.
  function Line(props) {
    const item = props.item
    const [busy, setBusy] = useState(false)
    const [message, setMessage] = useState('')
    const run = (action, body) => {
      setBusy(true)
      call('/review/items/' + encodeURIComponent(item.short_id) + '/' + action, { method: 'POST', body: body })
        .then(data => {
          setMessage(data.result.state + ': ' + data.result.message)
          props.onDone(Object.assign({}, data.result, { title: item.title }))
        })
        .catch(cause => setMessage(errorText(cause)))
        .finally(() => setBusy(false))
    }
    const button =
      props.action === 'manual'
        ? h(Btn, { variant: 'outline', disabled: busy, onClick: () => run('manual'), testId: 'manual-' + item.short_id }, 'Take back')
        : props.action === 'auto'
          ? h(Btn, { variant: 'outline', disabled: busy, onClick: () => run('auto', { note: '' }), testId: 'lineauto-' + item.short_id }, 'Auto')
          : props.action === 'confirm'
            ? h(Btn, { variant: 'danger', disabled: busy, onClick: () => run('confirm'), testId: 'confirm-' + item.short_id }, 'Confirm (cannot be undone)')
            : null
    return h(
      'div',
      { className: 'ini-line', 'data-ini': 'line-' + item.short_id },
      h('div', { className: 'ini-line__text' }, h('strong', null, item.short_id), ' ', linked(item.title), props.detail ? h(Muted, null, linked(props.detail)) : null),
      button,
      message ? h(Muted, null, linked(message)) : null
    )
  }

  function Waiting(props) {
    const entry = props.entry
    const item = entry.item
    const blocked = entry.status === 'blocked'
    const facts = [entry.card, entry.assignee, item ? item.short_id + ' is ' + item.status : null].filter(Boolean)
    return h(
      'article',
      { className: cx('ini-card ini-waiting', blocked && 'ini-waiting--blocked'), 'data-ini': 'waiting-' + entry.card },
      h(
        'div',
        { className: 'ini-row ini-between' },
        h(Pill, { tone: blocked ? 'bad' : 'warn' }, entry.status || 'waiting'),
        entry.since ? h('span', { className: 'ini-muted', title: localTime(entry.since) }, 'waiting ' + since(entry.since)) : null
      ),
      h('div', { className: 'ini-strong', title: entry.title }, cardTitle(entry.title)),
      h('div', { className: 'ini-waiting__step' }, h('span', { className: 'ini-label' }, 'Your step'), linked(entry.you)),
      h(Muted, { className: 'ini-ellipsis' }, linked(facts.join(' · ')))
    )
  }

  const GROUP_LISTS = ['decisions', 'pending', 'in_progress', 'auto', 'watching', 'deferred', 'recent', 'settled', 'suggested']

  function reviewById(data) {
    const byId = {}
    for (const name of ['queue', 'pending', 'watching', 'deferred', 'auto', 'in_progress', 'recent', 'settled', 'suggested']) {
      for (const item of (data && data[name]) || []) byId[item.short_id] = item
    }
    return byId
  }

  function hasReview(group) {
    if (!group) return false
    if (group.summary || group.update) return true
    return GROUP_LISTS.concat(['waiting']).some(name => (group[name] || []).length > 0)
  }

  // What waits on the user for one initiative: open decisions, confirmations, and cards whose handoff asks him.
  function attentionCount(group, byId) {
    const parts = attentionParts(group, byId)
    return parts.review + parts.waiting
  }

  // The same count split by what the user does: decide or confirm an item ("to review"), or do a card's step
  // ("waiting on you"). The badge names each kind, so it never promises a decision the section does not show.
  function attentionParts(group, byId) {
    if (!group) return { review: 0, waiting: 0 }
    const confirms = (group.in_progress || []).filter(id => byId[id] && byId[id].status === 'awaiting_confirm').length
    return { review: (group.decisions || []).length + confirms, waiting: (group.waiting || []).length }
  }

  function attentionLabel(parts) {
    return [
      parts.review ? (parts.review === 1 ? '1 to review' : parts.review + ' to review') : null,
      parts.waiting ? parts.waiting + ' waiting on you' : null
    ]
      .filter(Boolean)
      .join(' · ')
  }

  function attentionPills(parts, testId) {
    return [
      parts.review
        ? h('span', { key: 'review', title: 'Open decisions and confirmations for this initiative', 'data-ini': testId ? 'needs-you-' + testId : undefined }, h(Pill, { tone: 'warn' }, parts.review === 1 ? '1 to review' : parts.review + ' to review'))
        : null,
      parts.waiting
        ? h('span', { key: 'waiting', title: 'Cards whose newest handoff asks you to do a step. They are listed first under Reviews.', 'data-ini': testId ? 'waiting-on-you-' + testId : undefined }, h(Pill, { tone: 'warn' }, parts.waiting + ' waiting on you'))
        : null
    ]
  }

  // The page-wide waiting-on-you rows by card id, so each initiative's Reviews can list its own.
  function waitingIndex(data) {
    const index = {}
    for (const entry of (data && data.waiting_on_you) || []) index[entry.card] = entry
    return index
  }

  // A group holding a decision or a question starts open; quiet history stays folded.
  function startsOpen(group) {
    return Boolean((group.decisions || []).length || (group.pending || []).length || (group.in_progress || []).length || (group.waiting || []).length)
  }

  function groupCounts(group) {
    return [
      (group.waiting || []).length ? group.waiting.length + ' waiting on you' : null,
      group.decisions.length ? (group.decisions.length === 1 ? '1 decision' : group.decisions.length + ' decisions') : 'no decisions',
      (group.pending || []).length ? group.pending.length + ' waiting for options' : null,
      (group.watching || []).length ? group.watching.length + ' watching' : null,
      (group.deferred || []).length ? group.deferred.length + ' deferred' : null,
      (group.auto || []).length ? group.auto.length + ' auto' : null,
      (group.in_progress || []).length ? group.in_progress.length + ' in progress' : null
    ]
      .filter(Boolean)
      .join(' · ')
  }

  // The reviews of one initiative: prose, decisions and every lifecycle line. It is a sibling of the initiative
  // card, so a tap on a review action never opens the initiative.
  function ReviewGroupBody(props) {
    const g = props.group
    const byId = props.byId
    const ids = name => (g[name] || []).filter(id => byId[id])
    const item = id => byId[id]
    const progress = ids('in_progress')
    const auto = ids('auto')
    const watching = ids('watching')
    const deferred = ids('deferred')
    const recent = ids('recent')
    const settled = ids('settled')
    const waitingByCard = props.waitingByCard || {}
    const waiting = (g.waiting || []).map(card => waitingByCard[card]).filter(Boolean)
    return h(
      'div',
      { className: 'ini-stack ini-reviewbody' },
      // A card step the user owes comes first: it is what the initiative's "waiting on you" badge counts.
      waiting.length
        ? h(
            'div',
            { key: 'waiting', className: 'ini-stack', 'data-ini': 'review-waiting-' + (g.initiative || 'none') },
            h('span', { className: 'ini-label' }, 'Waiting on you (' + waiting.length + ')'),
            waiting.map(entry => h(Waiting, { key: entry.project + '-' + entry.card, entry }))
          )
        : null,
      g.summary ? h('div', { className: 'ini-summary', key: 'summary' }, linked(g.summary)) : null,
      g.update ? h(Muted, { key: 'update' }, linked('Since last review: ' + g.update)) : null,
      ...ids('decisions').map(sid => h(Decision, { key: 'decision-' + sid, item: item(sid), initiativeTitle: g.title, onDone: props.onDone })),
      h(
        Fold,
        { key: 'progress', title: 'In progress', count: progress.length, tone: 'warn', defaultOpen: progress.some(sid => item(sid).status === 'awaiting_confirm'), testId: 'fold-progress-' + (g.initiative || 'none') },
        progress.map(sid =>
          h(Line, {
            key: sid,
            item: item(sid),
            onDone: props.onDone,
            action: item(sid).status === 'awaiting_confirm' ? 'confirm' : null,
            detail: item(sid).status + (item(sid).closed_note ? ' — ' + item(sid).closed_note : '')
          })
        )
      ),
      h(
        Fold,
        { key: 'auto', title: 'Handled by agents', count: auto.length, testId: 'fold-auto-' + (g.initiative || 'none') },
        auto.map(sid =>
          h(Line, {
            key: sid,
            item: item(sid),
            onDone: props.onDone,
            action: 'manual',
            detail: 'auto — agents handle it at the next tick' + (item(sid).auto && item(sid).auto.note ? ': "' + item(sid).auto.note + '"' : '')
          })
        )
      ),
      ...ids('pending').map(sid =>
        h(
          'div',
          { key: 'pending-' + sid, 'data-ini': 'review-pending-' + sid },
          h(Line, { item: item(sid), onDone: props.onDone, action: 'auto', detail: item(sid).outdated ? 'its source changed; new options come with the next tick (within 30 min)' : 'options not written yet' })
        )
      ),
      h(
        Fold,
        { key: 'watching', title: 'Watching', count: watching.length, testId: 'fold-watching-' + (g.initiative || 'none') },
        watching.map(sid => h(Line, { key: sid, item: item(sid), onDone: props.onDone, action: 'auto', detail: item(sid).watch_reason || '' }))
      ),
      h(
        Fold,
        { key: 'deferred', title: 'Deferred', count: deferred.length, testId: 'fold-deferred-' + (g.initiative || 'none') },
        deferred.map(sid => {
          const d = item(sid).deferral
          return h(Line, { key: sid, item: item(sid), onDone: props.onDone, action: 'auto', detail: d ? (d.kind + ' ' + (d.until || '')).trim() + (d.reason ? ' — ' + d.reason : '') : '' })
        })
      ),
      h(
        Fold,
        { key: 'recent', title: 'Decided recently', count: recent.length, testId: 'fold-recent-' + (g.initiative || 'none') },
        recent.map(sid => {
          const last = (item(sid).actions || [])[0]
          return h(Line, { key: sid, item: item(sid), onDone: props.onDone, detail: item(sid).status + (last && last.receipt ? ' — ' + last.receipt : '') })
        })
      ),
      h(
        Fold,
        { key: 'settled', title: 'Settled by agents', count: settled.length, testId: 'fold-settled-' + (g.initiative || 'none') },
        [h(Muted, { key: 'hint' }, 'Overrule one from Desktop to suspend its rule.')].concat(
          settled.map(sid => h(Line, { key: sid, item: item(sid), onDone: props.onDone, detail: (item(sid).closed_note || '') + (item(sid).overruled ? ' (overruled)' : '') }))
        )
      ),
      ...ids('suggested').map(sid => h(Line, { key: 'suggested-' + sid, item: item(sid), detail: 'suggested' }))
    )
  }

  function ReviewGroup(props) {
    const g = props.group
    const id = g.initiative || 'none'
    const [open, setOpen] = useState(() => startsOpen(g))
    // A confirmation that appears after the group was folded must still be seen.
    const confirmations = (g.in_progress || []).filter(sid => props.byId[sid] && props.byId[sid].status === 'awaiting_confirm').length
    useEffect(() => {
      if (confirmations) setOpen(true)
    }, [confirmations])
    // So does a card that starts waiting on the user.
    const waitingCount = (g.waiting || []).length
    useEffect(() => {
      if (waitingCount) setOpen(true)
    }, [waitingCount])
    return h(
      'section',
      { className: cx('ini-reviewgroup', props.embedded && 'ini-reviewgroup--embedded'), 'data-ini': 'review-group-' + id },
      h(
        'button',
        { type: 'button', className: 'ini-reviewgroup__head', 'aria-expanded': open, onClick: () => setOpen(!open), 'data-ini': 'review-group-head-' + id },
        h('span', { className: 'ini-chevron', 'aria-hidden': true }, open ? '▾' : '▸'),
        h('span', { className: 'ini-strong' }, props.embedded ? 'Reviews' : g.title || g.initiative || 'No initiative'),
        !props.embedded && g.initiative ? h('code', { className: 'ini-mono' }, g.initiative) : null,
        h(Pill, { tone: g.decisions.length || waitingCount ? 'warn' : 'muted' }, groupCounts(g))
      ),
      open ? h(ReviewGroupBody, { group: g, byId: props.byId, waitingByCard: props.waitingByCard, onDone: props.onDone }) : null
    )
  }

  // The permanent home for reviews with no initiative, or one this project does not define.
  function UnassignedReviews(props) {
    const shown = props.groups.filter(group => (!group.initiative || group.known === false) && hasReview(group))
    return h(
      'section',
      { className: 'ini-stack ini-unassigned', 'data-ini': 'review-unassigned' },
      h('h2', { className: 'ini-h2' }, 'Unassigned reviews'),
      h(Muted, null, 'Reviews with no initiative, or naming an initiative this project does not define.'),
      shown.length
        ? shown.map(group => h(ReviewGroup, { key: 'unassigned-' + (group.initiative || 'none'), group, byId: props.byId, waitingByCard: props.waitingByCard, onDone: props.onDone }))
        : h('div', { className: 'ini-empty' }, 'No unassigned reviews.')
    )
  }

  function ReviewHeader(props) {
    const review = props.review
    // One line carries the review timestamp and the initiative count; the row below no longer repeats a Refresh.
    const meta = [(review && review.review ? 'Review ' + localTime(review.review.generated_at) : 'No review yet'), props.countText || ''].filter(Boolean).join(' · ')
    return h(
      'div',
      { className: 'ini-stack', 'data-ini': 'review-header' },
      h(
        'div',
        { className: 'ini-row ini-between' },
        h(Muted, { className: 'ini-ellipsis' }, meta),
        h(
          Btn,
          { variant: 'outline', icon: true, disabled: props.state.loading, onClick: props.onRefresh, testId: 'review-refresh', title: 'Refresh the review and this list', ariaLabel: 'Refresh' },
          props.state.loading ? '…' : '↻'
        )
      ),
      props.state.error ? h('div', { className: 'ini-bad' }, 'Could not read the review: ' + props.state.error) : null,
      props.notice ? h(Notice, { result: props.notice, onClose: props.onCloseNotice }) : null
    )
  }

  // ---- Initiatives ---------------------------------------------------------------------------------------

  function StatusSummary(props) {
    const summary = props.summary
    const clamp = props.clamp
    const [open, setOpen] = useState(false)
    const [overflow, setOverflow] = useState(false)
    const textRef = useRef(null)
    useEffect(() => {
      const node = textRef.current
      if (!node || !clamp || open) return undefined
      const measure = () => setOverflow(node.scrollHeight > node.clientHeight + 1)
      measure()
      if (!window.ResizeObserver) return undefined
      const observer = new window.ResizeObserver(measure)
      observer.observe(node)
      return () => observer.disconnect()
    }, [summary && summary.text, clamp, open])
    if (!summary) return h(Muted, { className: 'ini-italic' }, 'No status summary yet.')
    const checked = Date.parse(summary.checked_at)
    const age = Number.isFinite(checked) ? Math.max(0, Math.floor((Date.now() - checked) / 60000)) : summary.age_minutes
    return h(
      'div',
      { className: 'ini-summary', 'data-ini': 'status-summary' },
      h('div', { ref: textRef, className: clamp && !open ? 'ini-clamp ini-clamp--3' : null }, linked(summary.text)),
      clamp && overflow
        ? h(
            'button',
            { type: 'button', className: 'ini-more', 'aria-expanded': open, onClick: () => setOpen(!open), 'data-ini': 'status-summary-more' },
            open ? 'Less' : 'More'
          )
        : null,
      h(Muted, { className: summary.stale ? 'ini-bad' : '' }, summary.stale ? 'Not refreshed for ' + ago(age).replace(' ago', '') : 'Status · updated ' + ago(age))
    )
  }

  // Review attention for one cycle: its cards waiting on the user, plus open decisions and confirmations whose
  // item key names the cycle or one of its cards. Items naming none of them stay on the initiative.
  function cycleAttention(cycle, group, byId) {
    const parts = cycleAttentionParts(cycle, group, byId)
    return parts.review + parts.waiting
  }

  function cycleAttentionParts(cycle, group, byId) {
    if (!group) return { review: 0, waiting: 0 }
    const cards = (cycle.cards || []).concat(cycle.assessment_cards || []).map(card => card.id)
    const waiting = (group.waiting || []).filter(id => cards.indexOf(id) >= 0).length
    const ids = (group.decisions || []).concat((group.in_progress || []).filter(id => byId[id] && byId[id].status === 'awaiting_confirm'))
    const items = ids.filter(id => {
      const key = (byId[id] && byId[id].key) || ''
      return key.indexOf(cycle.id) >= 0 || cards.some(card => key.indexOf(card) >= 0)
    }).length
    return { review: items, waiting }
  }

  // Every open cycle of the active initiatives, one column per stage. On a phone the columns are a sideways
  // swipe, each close to a screen wide; on a wide screen all four sit side by side.
  function Board(props) {
    const active = props.initiatives.filter(initiative => initiative.status === 'active')
    const tiles = []
    active.forEach((initiative, index) =>
      initiative.open_cycles.forEach(cycle => {
        const parts = cycleAttentionParts(cycle, props.groupFor(initiative.id), props.byId)
        tiles.push({ initiative, cycle, index, parts, count: parts.review + parts.waiting })
      })
    )
    const rank = initiative => (initiative.rank == null ? Infinity : initiative.rank)
    const hidden = props.initiatives.filter(initiative => initiative.status !== 'active').reduce((n, initiative) => n + initiative.open_cycles.length, 0)
    const idle = active.filter(initiative => !initiative.open_cycles.length)
    return h(
      'section',
      { className: 'ini-stack', 'data-ini': 'board' },
      h('h2', { className: 'ini-h2' }, 'Open cycles by stage (' + tiles.length + ')'),
      h(
        'div',
        { className: 'ini-board' },
        BOARD_STAGES.map(stage => {
          const column = tiles
            .filter(tile => tile.cycle.stage === stage)
            .sort((a, b) => b.count - a.count || rank(a.initiative) - rank(b.initiative) || a.index - b.index)
          return h(
            'div',
            { key: stage, className: 'ini-board__col', 'data-ini': 'board-column-' + stage },
            h('div', { className: 'ini-row' }, h(Pill, { tone: STAGE_TONE[stage] }, stage), h('span', { className: 'ini-muted' }, String(column.length))),
            column.length
              ? column.map(tile => {
                  const cards = tile.cycle.cards || []
                  const open = cards.filter(card => card.state === 'fresh' && card.status !== 'done' && card.status !== 'archived').length
                  return h(
                    'article',
                    {
                      key: tile.cycle.id,
                      className: cx('ini-card ini-board__tile', tile.count && 'ini-unit--attention'),
                      'data-ini': 'board-cycle-' + tile.cycle.id,
                      onClick: () => props.onOpen(tile.initiative.id, tile.cycle.id)
                    },
                    h(
                      'div',
                      { className: 'ini-row' },
                      h('span', { className: 'ini-muted ini-ellipsis ini-grow', title: tile.initiative.title }, tile.initiative.title || tile.initiative.id),
                      ...(tile.count ? attentionPills(tile.parts) : [])
                    ),
                    h(
                      'button',
                      {
                        type: 'button',
                        className: 'ini-board__title',
                        onClick: event => {
                          event.stopPropagation()
                          props.onOpen(tile.initiative.id, tile.cycle.id)
                        },
                        'data-ini': 'board-open-' + tile.cycle.id
                      },
                      tile.cycle.title
                    ),
                    tile.cycle.next_action ? h(Muted, { className: 'ini-clamp ini-clamp--3' }, linked(tile.cycle.next_action)) : null,
                    (tile.cycle.flags || []).length ? h('div', { className: 'ini-row ini-wrap ini-flags' }, h(Flags, { flags: tile.cycle.flags, max: 3 })) : null,
                    h(Muted, null, [cards.length ? open + ' of ' + cards.length + ' cards open' : 'No linked cards', tile.cycle.evaluation_date ? 'evaluate ' + tile.cycle.evaluation_date : ''].filter(Boolean).join(' · '))
                  )
                })
              : h(Muted, null, 'None')
          )
        })
      ),
      idle.length
        ? h(
            'div',
            { className: 'ini-row ini-wrap', 'data-ini': 'board-idle' },
            h(Muted, null, 'No open cycle:'),
            idle.map(initiative => h(Btn, { key: initiative.id, variant: 'ghost', onClick: () => props.onOpen(initiative.id) }, initiative.title || initiative.id))
          )
        : null,
      hidden ? h(Muted, null, hidden + ' open cycles of paused or closed initiatives are not shown.') : null
    )
  }

  function InitiativeList(props) {
    const project = props.project
    const [state, reload] = useResource('/projects/' + encodeURIComponent(project) + '/initiatives')
    const [summaryState, reloadSummaries] = useResource('/projects/' + encodeURIComponent(project) + '/summaries')
    const summaries = (summaryState.data && summaryState.data.summaries) || {}
    const data = state.data
    const review = props.review
    const groups = (review && review.initiative_groups) || []
    const byId = reviewById(review)
    const groupFor = id => groups.find(group => group.initiative === id)
    const header = h(ReviewHeader, {
      review,
      countText: data ? data.initiatives.length + ' initiatives' : '',
      state: Object.assign({ loading: false, error: '' }, props.reviewState || {}, {
        loading: Boolean((props.reviewState || {}).loading || state.loading)
      }),
      onRefresh: () => {
        reload()
        reloadSummaries()
        if (props.onReviewRefresh) props.onReviewRefresh()
      },
      notice: props.notice,
      onCloseNotice: props.onCloseNotice
    })
    // One review fetch feeds every card, so a card must not paint before its reviews do.
    if (!review && !(props.reviewState && props.reviewState.error)) return h(Muted, null, 'Loading…')
    if (!data) {
      return h(
        'div',
        { className: 'ini-stack' },
        header,
        state.error ? h('div', { className: 'ini-bad' }, 'Could not read initiatives: ' + state.error) : h(Muted, null, 'Loading…')
      )
    }
    const freshness = data.freshness || {}
    const waiting = (review && review.waiting_on_you) || []
    const claimed = new Set(data.initiatives.map(initiative => initiative.id))
    // A known initiative with reviews that the list does not show keeps its group instead of losing it.
    const orphans = groups.filter(group => group.initiative && group.known && !claimed.has(group.initiative) && hasReview(group))
    // Initiatives with something for the user rise to the top, most items first; the API's rank order breaks ties.
    const waitingByCard = waitingIndex(review)
    const units = data.initiatives.map((initiative, index) => {
      const parts = attentionParts(groupFor(initiative.id), byId)
      return { initiative, index, group: groupFor(initiative.id), parts, count: parts.review + parts.waiting }
    })
    const needs = units.filter(u => u.count > 0).sort((a, b) => b.count - a.count || a.index - b.index)
    const quiet = units.filter(u => u.count === 0 && u.initiative.status === 'active')
    const inactive = units.filter(u => u.count === 0 && u.initiative.status !== 'active')
    const board = props.view === 'board'
    const unit = u => {
      const initiative = u.initiative
      const group = u.group
      return h('div', { key: initiative.id, className: cx('ini-card ini-unit', u.count && 'ini-unit--attention'), 'data-ini': 'initiative-unit-' + initiative.id, 'data-needs-review': u.count ? String(u.count) : undefined }, [
        h(
          'button',
          { key: 'card', type: 'button', className: 'ini-unit__header', onClick: () => props.onOpen(initiative.id), 'data-ini': 'initiative-' + initiative.id },
          h(
            'div',
            { className: 'ini-row ini-wrap' },
            h('span', { className: 'ini-strong' }, initiative.title || initiative.id),
            ...(u.count ? attentionPills(u.parts, initiative.id) : []),
            initiative.status === 'active' ? null : h(Pill, null, initiative.status),
            initiative.rank != null ? h(Pill, null, 'Rank ' + initiative.rank) : null,
            h('span', { className: 'ini-unit__chevron', 'aria-hidden': true }, '›')
          ),
          initiative.goal ? h(Muted, { className: 'ini-clamp' }, linked(initiative.goal)) : null,
          (initiative.flags || []).length ? h('div', { className: 'ini-row ini-wrap ini-flags' }, h(Flags, { flags: initiative.flags, max: 4 })) : null,
          h(Muted, null, (initiative.open_cycles.length ? initiative.open_cycles.length + ' open cycles' : 'No open cycles') + (initiative.next_evaluation ? ' · next evaluation ' + initiative.next_evaluation : ''))
        ),
        // Cycle lines sit outside the header button so a card ID in a next action can be its own button; a tap
        // elsewhere on a line still opens the initiative at that cycle.
        initiative.open_cycles.length
          ? h(
              'div',
              { key: 'cycles', className: 'ini-unit__cycles' },
              initiative.open_cycles.slice(0, 3).map(cycle =>
                h(
                  'div',
                  { key: cycle.id, className: 'ini-cycle-mini', 'data-ini': 'cycle-line-' + cycle.id, onClick: () => props.onOpen(initiative.id, cycle.id) },
                  h(Pill, { tone: STAGE_TONE[cycle.stage] }, cycle.stage),
                  h('span', null, cycle.title),
                  cycle.next_action ? h(Muted, { className: 'ini-clamp' }, linked(cycle.next_action)) : null
                )
              )
            )
          : null,
        summaryState.data && initiative.status === 'active'
          ? h('div', { key: 'summary', className: 'ini-unit__summary', 'data-ini': 'summary-for-' + initiative.id }, h(StatusSummary, { summary: summaries[initiative.id], clamp: true }))
          : null,
        hasReview(group) ? h('div', { key: 'reviews', className: 'ini-unit__reviews', 'data-ini': 'reviews-for-' + initiative.id }, h(ReviewGroup, { group, byId, waitingByCard, onDone: props.onReviewDone, embedded: true })) : null
      ])
    }
    return h(
      'div',
      { className: 'ini-stack', 'data-ini': 'initiatives', 'data-view': props.view || 'list' },
      header,
      props.onView
        ? h(Chips, {
            label: 'View',
            testId: 'view',
            value: props.view || 'list',
            onChange: props.onView,
            items: [
              { id: 'list', label: 'List' },
              { id: 'board', label: 'Board' }
            ]
          })
        : null,
      summaryState.error ? h('div', { className: 'ini-bad', role: 'alert', 'data-ini': 'summary-error' }, 'Could not refresh status summaries: ' + summaryState.error) : null,
      waiting.length
        ? h(
            'section',
            { className: 'ini-stack', 'data-ini': 'waiting' },
            h('h2', { className: 'ini-h2' }, 'Waiting on you (' + waiting.length + ')'),
            h(Muted, null, 'Cards whose newest handoff asks you to do a step. Start the session for one from Desktop or chat.'),
            h('div', { className: 'ini-grid' }, waiting.map(entry => h(Waiting, { key: entry.project + '-' + entry.card, entry })))
          )
        : null,
      (review && (review.waiting_errors || []).length) ? h('div', { className: 'ini-bad' }, 'Could not check some boards: ' + review.waiting_errors.join('; ')) : null,
      state.error ? h('div', { className: 'ini-bad' }, 'Refresh failed: ' + state.error) : null,
      freshness.kanban === 'unavailable' ? h('div', { className: 'ini-bad' }, 'Kanban unavailable: ' + (freshness.kanban_error || '')) : null,
      data.validation && !data.validation.ok ? h('div', { className: 'ini-bad' }, data.validation.error_count + ' validation errors') : null,
      board && needs.length
        ? h(
            'section',
            { className: 'ini-stack', 'data-ini': 'board-needs-review' },
            h('h2', { className: 'ini-h2' }, 'Needs your review (' + needs.length + ')'),
            h(
              'div',
              { className: 'ini-row ini-wrap' },
              needs.map(u => h(Btn, { key: u.initiative.id, variant: 'outline', testId: 'board-needs-' + u.initiative.id, onClick: () => props.onOpen(u.initiative.id) }, (u.initiative.title || u.initiative.id) + ' · ' + attentionLabel(u.parts)))
            )
          )
        : null,
      board ? h(Board, { initiatives: data.initiatives, groupFor, byId, onOpen: props.onOpen }) : null,
      !board && needs.length ? h('section', { className: 'ini-stack', 'data-ini': 'initiatives-needs-review' }, h('h2', { className: 'ini-h2' }, 'Needs your review (' + needs.length + ')'), needs.map(unit)) : null,
      !board && quiet.length
        ? h(
            'section',
            { className: 'ini-stack', 'data-ini': 'initiatives-other' },
            needs.length ? h('h2', { className: 'ini-h2' }, 'Other initiatives (' + quiet.length + ')') : null,
            h('div', { className: 'ini-grid' }, quiet.map(unit))
          )
        : null,
      board ? null : h(Fold, { title: 'Inactive initiatives', count: inactive.length, testId: 'initiatives-inactive' }, inactive.map(unit)),
      orphans.length
        ? h(
            'section',
            { className: 'ini-stack' },
            h('h2', { className: 'ini-h2' }, 'Reviews for initiatives not in this list'),
            orphans.map(group => h(ReviewGroup, { key: 'orphan-' + group.initiative, group, byId, waitingByCard, onDone: props.onReviewDone }))
          )
        : null,
      h(UnassignedReviews, { groups, byId, waitingByCard, onDone: props.onReviewDone }),
      (data.other_folders || []).length ? h(Muted, null, 'Not initiatives: ' + data.other_folders.join(', ')) : null
    )
  }

  function prText(pr) {
    pr = pr || { state: 'none' }
    if (pr.state === 'merged') return ('merged ' + day(pr.merged_at) + ' ' + (pr.merge_commit || '')).trim()
    if (pr.state === 'open_or_unknown') return 'not merged per card'
    return 'no PR'
  }

  function CardRow(props) {
    const card = props.card
    if (card.state !== 'fresh') {
      return h('div', { className: 'ini-cardrow' }, h(CardRef, { id: card.id }), h(Pill, { tone: card.state === 'not_found' ? 'bad' : 'warn' }, card.state === 'not_found' ? 'not found' : 'unavailable'))
    }
    return h(
      'div',
      { className: 'ini-cardrow', 'data-ini': 'card-' + card.id },
      h('div', { className: 'ini-row ini-wrap' }, h(Pill, { tone: card.status === 'done' ? 'good' : 'muted' }, card.status), h(CardRef, { id: card.id }), card.assignee ? h(Muted, null, card.assignee) : null),
      h('div', null, linked(card.title)),
      card.pr && card.pr.url
        ? h('a', { className: 'ini-link', href: card.pr.url, target: '_blank', rel: 'noopener noreferrer' }, 'Open PR · ' + prText(card.pr))
        : h(Muted, null, prText(card.pr))
    )
  }

  function Cycle(props) {
    const cycle = props.cycle
    const [open, setOpen] = useState(Boolean(props.defaultOpen))
    const release = cycle.release || { state: 'none' }
    const releaseText = release.state === 'verified' ? 'verified ' + (release.date || '') : release.state === 'failed' ? 'failed' : 'not verified'
    const measurement = cycle.measurement || {}
    const observations = (cycle.observations || []).slice().reverse().slice(0, 3)
    return h(
      'article',
      { className: 'ini-card', 'data-ini': 'cycle-' + cycle.id },
      h(
        'button',
        { type: 'button', className: 'ini-cycle-head', 'aria-expanded': open, onClick: () => setOpen(!open) },
        h('span', { className: 'ini-chevron', 'aria-hidden': true }, open ? '▾' : '▸'),
        h(Pill, { tone: STAGE_TONE[cycle.stage] }, cycle.stage),
        h('span', { className: 'ini-strong' }, cycle.title),
        h('code', { className: 'ini-muted' }, cycle.id.split('-').pop())
      ),
      h('div', { className: 'ini-row ini-wrap' }, h(Flags, { flags: cycle.flags })),
      h(
        'dl',
        { className: 'ini-meta' },
        cycle.next_action ? [h('dt', { key: 'nt' }, 'Next'), h('dd', { key: 'nd' }, linked(cycle.next_action))] : null,
        h('dt', null, 'Release'),
        h('dd', null, releaseText),
        cycle.evaluation_date ? [h('dt', { key: 'et' }, 'Evaluation'), h('dd', { key: 'ed' }, cycle.evaluation_date)] : null
      ),
      open
        ? h(
            'div',
            { className: 'ini-stack' },
            cycle.question ? h('div', null, h('strong', null, 'Question: '), linked(cycle.question)) : null,
            measurement.success_criteria ? h('div', null, h('strong', null, 'Success: '), linked(measurement.success_criteria)) : null,
            measurement.primary_metric ? h(Muted, null, 'Metric: ' + measurement.primary_metric) : null,
            h('h4', { className: 'ini-h4' }, 'Kanban cards (' + (cycle.cards || []).length + ')'),
            (cycle.cards || []).length ? cycle.cards.map(card => h(CardRow, { key: card.id, card: card })) : h(Muted, null, 'No linked cards'),
            (cycle.assessment_cards || []).length ? h('h4', { className: 'ini-h4' }, 'Outcome assessments') : null,
            (cycle.assessment_cards || []).map(card => h(CardRow, { key: card.id, card: card })),
            cycle.outcome_loop && cycle.outcome_loop.enabled ? h(Muted, null, linked(
              'Outcome review: ' + cycle.outcome_loop.state + (cycle.outcome_loop.assessment_card ? ' · Assessment card: ' + cycle.outcome_loop.assessment_card : '') +
              ' · Review due: ' + (cycle.outcome_loop.due_date || 'Not agreed') +
              ' · Contract gaps: ' + ((cycle.outcome_loop.contract_gaps || []).join(', ') || 'None'))) : null,
            h('h4', { className: 'ini-h4' }, 'Latest observations'),
            observations.length
              ? observations.map((row, i) =>
                  h(
                    'div',
                    { key: i, className: 'ini-observation' },
                    h('div', null, linked([day(row.evidence_date || row.at), row.kind, row.metric, row.value].filter(Boolean).join(' · '))),
                    row.detail ? h(Muted, null, linked(row.detail)) : null
                  )
                )
              : h(Muted, null, 'No observations'),
            cycle.result && cycle.result.verdict && cycle.result.verdict !== 'unset'
              ? h('div', null, h('strong', null, 'Result: '), linked(cycle.result.verdict + (cycle.result.summary ? ' — ' + cycle.result.summary : '')))
              : null,
            (cycle.errors || []).map((e, i) => h('div', { key: 'e' + i, className: 'ini-bad' }, linked(e)))
          )
        : null
    )
  }

  function InitiativeDetail(props) {
    const [state, reload] = useResource('/initiatives/' + encodeURIComponent(props.id) + '?project=' + encodeURIComponent(props.project))
    const [summaryState, reloadSummaries] = useResource('/projects/' + encodeURIComponent(props.project) + '/summaries')
    const detail = state.data
    const rootRef = useRef(null)
    // Opened from a board tile: bring that cycle into view once it has rendered.
    const ready = Boolean(detail)
    useEffect(() => {
      if (!ready || !props.cycle || !rootRef.current) return
      const node = rootRef.current.querySelector('[data-ini="cycle-' + props.cycle + '"]')
      if (node && node.scrollIntoView) node.scrollIntoView({ block: 'start' })
    }, [ready, props.cycle])
    const back = h(Btn, { variant: 'ghost', onClick: props.onBack, testId: 'back' }, '‹ All initiatives')
    if (!detail) return h('div', { className: 'ini-stack' }, back, state.error ? h('div', { className: 'ini-bad' }, state.error) : h(Muted, null, 'Loading…'))
    const cycles = detail.cycles || []
    const open = cycles.filter(c => OPEN.has(c.stage))
    const closed = cycles.filter(c => !OPEN.has(c.stage))
    const groups = (props.review && props.review.initiative_groups) || []
    const byId = reviewById(props.review)
    const waitingByCard = waitingIndex(props.review)
    const group = groups.find(entry => entry.initiative === detail.id) || { decisions: [], pending: [], in_progress: [], auto: [], watching: [], deferred: [], recent: [], settled: [], suggested: [], waiting: [] }
    return h(
      'div',
      { className: 'ini-stack', 'data-ini': 'detail-' + detail.id, ref: rootRef },
      h('div', { className: 'ini-row ini-between' }, back, h(Btn, { variant: 'outline', disabled: state.loading || summaryState.loading, onClick: () => { reload(); reloadSummaries() } }, '↻ Refresh')),
      h('h2', { className: 'ini-h1' }, detail.title || detail.id),
      h('div', { className: 'ini-row ini-wrap' }, h(Pill, { tone: detail.status === 'active' ? 'good' : 'muted' }, detail.status), detail.rank != null ? h(Pill, null, 'Rank ' + detail.rank) : null),
      detail.goal ? h('div', null, h('strong', null, 'Goal: '), linked(detail.goal)) : null,
      summaryState.error ? h('div', { className: 'ini-bad', role: 'alert', 'data-ini': 'summary-error' }, 'Could not refresh status summaries: ' + summaryState.error) : null,
      summaryState.data && detail.status === 'active' ? h(StatusSummary, { summary: (summaryState.data.summaries || {})[detail.id] }) : null,
      detail.focus_cycle ? h(Muted, null, 'Focus: ' + detail.focus_cycle) : null,
      (detail.freshness || {}).kanban === 'unavailable' ? h('div', { className: 'ini-bad' }, 'Kanban unavailable: ' + (detail.freshness.kanban_error || '')) : null,
      h(
        'section',
        { className: 'ini-stack', 'data-ini': 'detail-review-' + detail.id },
        h('h3', { className: 'ini-h2' }, 'Reviews'),
        h(ReviewHeader, {
          review: props.review,
          state: props.reviewState || { loading: false, error: '' },
          onRefresh: props.onReviewRefresh || (() => undefined),
          notice: props.notice,
          onCloseNotice: props.onCloseNotice
        }),
        !props.review
          ? h(Muted, null, props.reviewState && props.reviewState.error ? 'Review unavailable.' : 'Loading reviews…')
          : hasReview(group) ? h(ReviewGroupBody, { group, byId, waitingByCard, onDone: props.onReviewDone }) : h(Muted, null, 'No reviews for this initiative.')
      ),
      h('h3', { className: 'ini-h2' }, 'Open cycles (' + open.length + ')'),
      open.length ? open.map(cycle => h(Cycle, { key: cycle.id, cycle: cycle, defaultOpen: cycle.id === (props.cycle || detail.focus_cycle) })) : h(Muted, null, 'No open cycles'),
      h(
        Fold,
        { title: 'Concluded and abandoned', count: closed.length, testId: 'fold-closed' },
        closed.map(cycle => h(Cycle, { key: cycle.id, cycle: cycle }))
      )
    )
  }

  // ---- Tasks ---------------------------------------------------------------------------------------------

  const TASK_TONE = { open: 'warn', triaged: 'warn', in_progress: 'good', done: 'muted', cancelled: 'muted' }
  const PRIORITY_TONE = { high: 'bad', medium: 'warn', low: 'muted' }
  const STATUS_LABEL = { open: 'Open', triaged: 'Triaged', in_progress: 'In progress', done: 'Done', cancelled: 'Cancelled' }
  const TASK_FILTER_KEY = 'hermes.initiatives.mobile.taskFilter'
  const TASK_FILTERS = ['active', 'open', 'triaged', 'in_progress', 'done', 'cancelled', 'all']
  // Task notes are hand- and agent-written: render whatever a field holds instead of assuming its shape.
  function asText(value) {
    return value === null || value === undefined ? '' : String(value)
  }
  function asList(value) {
    return Array.isArray(value) ? value.map(asText) : value ? [asText(value)] : []
  }
  function title(value) {
    const text = asText(value)
    return text ? text[0].toUpperCase() + text.slice(1) : ''
  }

  function Chips(props) {
    const chips = h(
      'div',
      { className: 'ini-chips', role: 'group', 'aria-label': props.label, 'data-ini': props.testId },
      props.items.map(item =>
        h(
          'button',
          {
            key: item.id,
            type: 'button',
            className: cx('ini-chip', item.id === props.value && 'ini-chip--on'),
            'aria-pressed': item.id === props.value,
            onClick: () => props.onChange(item.id),
            'data-ini': props.testId + '-' + item.id
          },
          item.label
        )
      )
    )
    return props.showLabel ? h('div', null, h('span', { className: 'ini-label' }, props.label), chips) : chips
  }

  function TaskField(props) {
    if (!props.value) return null
    return h('div', { className: 'ini-field' }, h('div', { className: 'ini-h4' }, props.label), h('div', { className: 'ini-pre' }, linked(String(props.value))))
  }

  function TaskDetail(props) {
    const task = props.task
    const base = '/projects/' + encodeURIComponent(props.project.id) + '/tasks/' + encodeURIComponent(task.id)
    const [reason, setReason] = useState('')
    const [assignee, setAssignee] = useState('')
    const [note, setNote] = useState('')
    const [busy, setBusy] = useState('')
    const inFlight = useRef(false)
    const message = props.message
    const run = (key, request, success) => {
      // One request at a time, even for two taps landing in the same frame.
      if (inFlight.current) return
      inFlight.current = true
      setBusy(key)
      props.onMessage(null)
      request()
        .then(data => props.onMessage({ ok: true, text: success(data) }))
        .catch(cause => props.onMessage({ ok: false, text: errorText(cause) }))
        .finally(() => {
          inFlight.current = false
          setBusy('')
          // Reload after a failure too: a timed-out card create may have succeeded on the server.
          props.onChanged()
        })
    }
    const move = status =>
      run(
        status,
        () => call(base, { method: 'PATCH', body: status === 'done' || status === 'cancelled' ? { status: status, reason: reason.trim() || null } : { status: status } }),
        () => {
          setReason('')
          return task.id + ': ' + STATUS_LABEL[status]
        }
      )
    const createCard = () =>
      run('card', () => call(base + '/card', { method: 'POST', body: { assignee: assignee.trim() || null, note: note } }), data => 'Created ' + data.card + ' for ' + data.assignee)
    const closed = task.status === 'done' || task.status === 'cancelled'
    const canComplete = Boolean(task.card_id || reason.trim())
    const canReopen = closed || task.status === 'triaged'
    const card = task.card
    return h(
      'div',
      { className: 'ini-stack ini-task-detail', 'data-ini': 'task-detail-' + task.id },
      h(TaskField, { label: 'Description', value: task.description }),
      task.type === 'bug' && task.symptom !== task.description ? h(TaskField, { label: 'Symptom', value: task.symptom }) : null,
      task.type === 'bug' ? h(TaskField, { label: 'Expected', value: task.expected }) : null,
      task.type === 'bug' ? h(TaskField, { label: 'Repro', value: task.repro }) : null,
      task.type === 'bug' ? h(TaskField, { label: 'Where', value: task.where }) : null,
      asList(task.evidence).length ? h(TaskField, { label: 'Evidence', value: asList(task.evidence).map(e => '- ' + e).join('\n') }) : null,
      h(TaskField, { label: 'Notes', value: task.body }),
      h(TaskField, { label: 'Reason', value: task.reason }),
      asList(task.errors).length ? h('div', { className: 'ini-bad' }, linked(asList(task.errors).join('; '))) : null,
      h('div', { className: 'ini-h4' }, 'Kanban card'),
      task.card_id
        ? h(
            'div',
            { className: 'ini-cardrow', 'data-ini': 'task-card-' + task.id },
            h(
              'div',
              { className: 'ini-row ini-wrap' },
              card && card.status ? h(Pill, { tone: card.status === 'done' ? 'good' : 'muted' }, card.status) : null,
              card && card.state === 'fresh'
                ? null
                : (card && card.state === 'not_found') || !props.kanbanDown
                  ? h(Pill, { tone: 'bad' }, 'not found')
                  : h(Pill, { tone: 'warn' }, 'unavailable'),
              h(CardRef, { id: task.card_id }),
              card && card.assignee ? h(Muted, null, card.assignee) : null
            ),
            card && card.title ? h('div', null, card.title) : null,
            card && card.pr && card.pr.url
              ? h('a', { className: 'ini-link', href: card.pr.url, target: '_blank', rel: 'noopener noreferrer' }, 'Open PR · ' + prText(card.pr))
              : card && card.state === 'fresh'
                ? h(Muted, null, prText(card.pr))
                : null
          )
        : closed
          ? h(Muted, null, 'No card')
          : h(
              'div',
              { className: 'ini-stack' },
              h('input', {
                className: 'ini-input',
                placeholder: 'Assignee (blank: project intake)',
                'aria-label': 'Assignee',
                value: assignee,
                disabled: Boolean(busy),
                onChange: event => setAssignee(event.target.value),
                'data-ini': 'task-assignee-' + task.id
              }),
              h('textarea', {
                className: 'ini-input',
                placeholder: 'Optional note for the card',
                'aria-label': 'Note for the card',
                rows: 2,
                value: note,
                disabled: Boolean(busy),
                onChange: event => setNote(event.target.value),
                'data-ini': 'task-note-' + task.id
              }),
              h(Btn, { variant: 'outline', block: true, disabled: Boolean(busy), onClick: createCard, testId: 'task-create-card-' + task.id }, busy === 'card' ? 'Creating…' : 'Create Kanban card')
            ),
      h('textarea', {
        className: 'ini-input',
        placeholder: 'Reason (needed to cancel, or to complete without a card)',
        'aria-label': 'Reason',
        rows: 2,
        value: reason,
        disabled: Boolean(busy),
        onChange: event => setReason(event.target.value),
        'data-ini': 'task-reason-' + task.id
      }),
      h(
        'div',
        { className: 'ini-row ini-wrap ini-task-actions' },
        task.status === 'open' ? h(Btn, { variant: 'outline', disabled: Boolean(busy), onClick: () => move('triaged'), testId: 'task-triaged-' + task.id }, 'Mark triaged') : null,
        closed ? null : h(Btn, { variant: 'outline', disabled: Boolean(busy) || !canComplete, onClick: () => move('done'), testId: 'task-done-' + task.id }, 'Mark done'),
        closed ? null : h(Btn, { variant: 'outline', disabled: Boolean(busy) || !reason.trim(), onClick: () => move('cancelled'), testId: 'task-cancelled-' + task.id }, task.type === 'bug' ? 'Cancel bug' : 'Cancel task'),
        canReopen ? h(Btn, { variant: 'outline', disabled: Boolean(busy), onClick: () => move('open'), testId: 'task-reopen-' + task.id }, 'Reopen') : null
      ),
      closed || canComplete ? null : h(Muted, null, 'Mark done needs a card or a reason.'),
      message ? h('div', { className: message.ok ? 'ini-result' : 'ini-bad', 'data-ini': 'task-result-' + task.id, role: 'status' }, linked(message.text)) : null,
      h(Muted, { className: 'ini-mono' }, asText(task.path))
    )
  }

  function NewTask(props) {
    const kind = props.kind || 'task'
    const bug = kind === 'bug'
    const type = kind
    const [title, setTitle] = useState('')
    const [description, setDescription] = useState('')
    const [priority, setPriority] = useState(bug ? 'high' : 'medium')
    const [expected, setExpected] = useState('')
    const [repro, setRepro] = useState('')
    const [where, setWhere] = useState('')
    const [busy, setBusy] = useState(false)
    const [error, setError] = useState('')
    const ready = title.trim() && description.trim()
    const submit = event => {
      event.preventDefault()
      if (busy || !ready) return
      setBusy(true)
      setError('')
      const body = { type: type, title: title.trim(), description: description.trim(), priority: priority }
      if (bug) {
        if (expected.trim()) body.expected = expected.trim()
        if (repro.trim()) body.repro = repro.trim()
        if (where.trim()) body.where = where.trim()
      }
      call('/projects/' + encodeURIComponent(props.project.id) + '/tasks', { method: 'POST', body: body })
        .then(data => props.onCreated(data.task))
        .catch(cause => {
          // Keep the input for a retry.
          setError(errorText(cause))
          setBusy(false)
        })
    }
    const area = (testId, label, value, set, rows) =>
      h('textarea', { className: 'ini-input', placeholder: label, 'aria-label': label, rows: rows, value: value, disabled: busy, onChange: event => set(event.target.value), 'data-ini': testId })
    return h(
      'form',
      { className: 'ini-card ini-stack', onSubmit: submit, 'data-ini': 'task-form', 'data-kind': kind },
      h('h3', { className: 'ini-question' }, bug ? 'Report bug' : 'New task'),
      h('input', { className: 'ini-input', placeholder: 'Title', 'aria-label': 'Title', value: title, disabled: busy, required: true, maxLength: 300, onChange: event => setTitle(event.target.value), 'data-ini': 'task-title' }),
      h('textarea', {
        className: 'ini-input',
        placeholder: bug ? 'What happens' : 'What needs doing',
        'aria-label': 'Description',
        rows: 4,
        value: description,
        disabled: busy,
        required: true,
        onChange: event => setDescription(event.target.value),
        'data-ini': 'task-description'
      }),
      bug ? area('task-expected', 'What should happen (optional)', expected, setExpected, 2) : null,
      bug ? area('task-repro', 'Steps to reproduce (optional)', repro, setRepro, 2) : null,
      bug ? h('input', { className: 'ini-input', placeholder: 'Page, file, or service (optional)', 'aria-label': 'Where', value: where, disabled: busy, onChange: event => setWhere(event.target.value), 'data-ini': 'task-where' }) : null,
      h(Chips, {
        label: 'Priority',
        showLabel: true,
        value: priority,
        onChange: setPriority,
        testId: 'task-form-priority',
        items: [{ id: 'high', label: 'High' }, { id: 'medium', label: 'Medium' }, { id: 'low', label: 'Low' }]
      }),
      error ? h('div', { className: 'ini-bad', role: 'alert' }, error) : null,
      ready || busy ? null : h(Muted, null, 'A title and a description are needed.'),
      h(
        'div',
        { className: 'ini-row' },
        h('button', { type: 'submit', className: 'ini-btn ini-btn--primary', style: { flex: 1 }, disabled: busy || !ready, 'data-ini': 'task-submit' }, busy ? 'Creating…' : bug ? 'Report bug' : 'Create task'),
        h(Btn, { variant: 'outline', disabled: busy, onClick: props.onClose, testId: 'task-form-cancel' }, 'Cancel')
      )
    )
  }

  // One tab per record type (props.kind is 'task' or 'bug'). Both read the same canonical task API and share the
  // row, detail and status rules; only the list, its counts, its remembered filter and the form are scoped.
  function Tasks(props) {
    const project = props.project
    const kind = props.kind || 'task'
    const bug = kind === 'bug'
    const filterKey = TASK_FILTER_KEY + (bug ? '.bug' : '')
    const [state, reload] = useResource('/projects/' + encodeURIComponent(project.id) + '/tasks')
    const [filter, setFilterState] = useState(() => {
      const saved = readLocal(filterKey, 'active')
      return TASK_FILTERS.includes(saved) ? saved : 'active'
    })
    // Action results live here, not in the row: a cancelled task leaves the Active list, its result must not.
    const [messages, setMessages] = useState({})
    const setMessage = (id, message) => setMessages(previous => Object.assign({}, previous, { [id]: message }))
    const [creating, setCreating] = useState(false)
    const [expanded, setExpanded] = useState('')
    const [created, setCreated] = useState('')
    const data = state.data
    const all = (data && data.tasks) || []
    const typed = all.filter(task => (task.type === 'bug') === bug)
    const openStatuses = new Set((data && data.open_statuses) || ['open', 'triaged', 'in_progress'])
    const openCount = typed.filter(task => openStatuses.has(task.status)).length
    useEffect(() => {
      if (data) props.onCount(openCount)
    }, [data, openCount])
    const setFilter = next => {
      setFilterState(next)
      writeLocal(filterKey, next)
    }
    if (!data) return state.error ? h('div', { className: 'ini-bad' }, 'Could not read ' + (bug ? 'bugs' : 'tasks') + ': ' + state.error) : h(Muted, null, 'Loading…')
    const shown = filter === 'all' ? typed : filter === 'active' ? typed.filter(task => openStatuses.has(task.status)) : typed.filter(task => task.status === filter)
    const count = status => typed.filter(task => task.status === status).length
    const statuses = data.statuses || ['open', 'triaged', 'in_progress', 'done', 'cancelled']
    const noun = bug ? 'bugs' : 'tasks'
    return h(
      'div',
      { className: 'ini-stack', 'data-ini': bug ? 'bugs' : 'tasks', 'data-kind': kind },
      creating
        ? h(NewTask, {
            project: project,
            kind: kind,
            onClose: () => setCreating(false),
            onCreated: task => {
              setCreating(false)
              setFilter('active')
              setCreated(task ? task.id : '')
              if (task) setExpanded(task.id)
              reload()
            }
          })
        : h(
            'div',
            { className: 'ini-row ini-between' },
            h(Btn, { onClick: () => setCreating(true), testId: 'task-new' }, bug ? '+ Report bug' : '+ New task'),
            h(Btn, { variant: 'outline', disabled: state.loading, onClick: reload }, state.loading ? 'Refreshing…' : '↻ Refresh')
          ),
      Object.keys(messages)
        .filter(id => messages[id] && !(expanded === id && shown.some(task => task.id === id)))
        .map(id =>
          h(
            'div',
            { key: id, className: cx('ini-notice', !messages[id].ok && 'ini-notice--bad'), role: 'status', 'data-ini': 'task-result-' + id },
            h('div', { className: 'ini-notice__text' }, messages[id].text),
            h(Btn, { variant: 'ghost', onClick: () => setMessage(id, null) }, 'Dismiss')
          )
        ),
      created ? h('div', { className: 'ini-notice', role: 'status', 'data-ini': 'task-created' }, h('div', { className: 'ini-notice__text' }, h('strong', null, 'Created ' + created)), h(Btn, { variant: 'ghost', onClick: () => setCreated('') }, 'Dismiss')) : null,
      state.error ? h('div', { className: 'ini-bad' }, 'Refresh failed: ' + state.error) : null,
      data.kanban && data.kanban.state === 'unavailable' ? h('div', { className: 'ini-bad' }, 'Kanban unavailable: ' + (data.kanban.error || '')) : null,
      creating ? null : h(Chips, {
        label: 'Status',
        value: filter,
        onChange: setFilter,
        testId: 'task-filter',
        items: [{ id: 'active', label: 'Active (' + openCount + ')' }]
          .concat(statuses.map(status => ({ id: status, label: (STATUS_LABEL[status] || status) + ' (' + count(status) + ')' })))
          .concat([{ id: 'all', label: 'All (' + typed.length + ')' }])
      }),
      creating
        ? null
        : shown.length === 0
        ? typed.length
          ? h(
              'div',
              { className: 'ini-empty' },
              h('div', null, 'No ' + noun + ' match this filter.'),
              h(Btn, { variant: 'outline', onClick: () => setFilter('all'), testId: 'task-show-all' }, 'Show all')
            )
          : h('div', { className: 'ini-empty' }, bug ? 'No bugs yet. Report one here, or ask an agent to file one.' : 'No tasks yet. Create one here, or ask an agent to file one.')
        : null,
      (creating ? [] : shown).map(task => {
        const open = expanded === task.id
        return h(
          'article',
          { key: task.id, className: 'ini-card', 'data-ini': 'task-' + task.id },
          // The linked card's ID sits beside the expand button (a button may not hold another).
          task.card_id
            ? h('div', { className: 'ini-task-ref ini-muted' }, h(CardRef, { id: task.card_id }), task.card && task.card.status ? ' · ' + task.card.status : '')
            : null,
          h(
            'button',
            { type: 'button', className: 'ini-task-head', 'aria-expanded': open, onClick: () => setExpanded(open ? '' : task.id) },
            h(
              'div',
              { className: 'ini-row ini-wrap' },
              h(Pill, { tone: TASK_TONE[task.status] || 'muted' }, STATUS_LABEL[task.status] || title(task.status)),
              h(Pill, { tone: PRIORITY_TONE[asText(task.priority)] || 'muted' }, title(task.priority) || 'No priority'),
              asList(task.errors).length ? h(Pill, { tone: 'bad' }, 'invalid') : null
            ),
            h('div', { className: 'ini-strong' }, asText(task.title) || task.id),
            open || !task.description ? null : h(Muted, { className: 'ini-clamp' }, asText(task.description)),
            h(
              'div',
              { className: 'ini-task-meta' },
              [day(task.created), asText(task.related_initiative), task.id].filter(Boolean).map((part, index) => h('span', { key: index }, (index ? ' · ' : '') + part))
            )
          ),
          open
            ? h(TaskDetail, {
                project: project,
                task: task,
                kanbanDown: Boolean(data.kanban && data.kanban.state === 'unavailable'),
                message: messages[task.id] || null,
                onMessage: message => setMessage(task.id, message),
                onChanged: reload
              })
            : null
        )
      })
    )
  }

  // ---- metrics -------------------------------------------------------------------------------------------

  // Kanban health for the project's board: totals vs the previous equal window, then per-day/week charts.
  const METRIC_DAYS = [7, 14, 30, 90]
  const METRIC_KEY = 'hermes.initiatives.mobile.metricsDays'
  const CAUSE = {
    rate_limited: ['rate limited', '#d29922'],
    protocol_violation: ['protocol violation', '#a371f7'],
    crashed: ['crashed', '#dc2626'],
    timed_out: ['timed out', '#db6d28'],
    gave_up: ['gave up', '#8b949e'],
    reclaimed: ['reclaimed', '#3fb950'],
    spawn_failed: ['spawn failed', '#58a6ff']
  }

  function compact(n) {
    if (n == null) return '–'
    const abs = Math.abs(n)
    if (abs >= 1e9) return (n / 1e9).toFixed(1) + 'B'
    if (abs >= 1e6) return (n / 1e6).toFixed(1) + 'M'
    if (abs >= 1e3) return (n / 1e3).toFixed(1) + 'k'
    return String(Math.round(n * 10) / 10)
  }

  function pct(r) {
    return r == null ? '–' : Math.round(r * 100) + '%'
  }

  function hrs(h) {
    if (h == null) return '–'
    return h >= 48 ? (h / 24).toFixed(1) + ' d' : h + ' h'
  }

  function shortDate(iso, withTime) {
    const at = Date.parse(iso || '')
    if (!Number.isFinite(at)) return String(iso || '')
    const opts = withTime ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' } : { month: 'short', day: 'numeric' }
    return new Date(at).toLocaleString(undefined, opts)
  }

  function delta(cur, prev, lowerBetter) {
    if (cur == null || prev == null || prev === 0) return null
    const change = (cur - prev) / Math.abs(prev)
    if (Math.abs(change) < 0.05) return { text: '≈', tone: 'muted' }
    const better = lowerBetter ? change < 0 : change > 0
    return { text: (change > 0 ? '▲ ' : '▼ ') + Math.round(Math.abs(change) * 100) + '%', tone: better ? 'good' : 'bad' }
  }

  function Kpi(props) {
    const d = props.d
    return h(
      'div',
      { className: 'ini-kpi', 'data-ini': props.testId },
      h('div', { className: 'ini-label' }, props.label),
      h(
        'div',
        { className: 'ini-kpi__value' },
        props.value,
        d ? h('span', { className: 'ini-kpi__delta ini-tone--' + d.tone, title: 'previous window: ' + props.prev }, d.text) : null
      ),
      props.hint ? h('div', { className: 'ini-muted' }, props.hint) : null
    )
  }

  function Chart(props) {
    const [hover, setHover] = useState(-1)
    const labels = props.labels
    const series = props.series || []
    const lines = props.lines || []
    const n = labels.length
    if (!n) return null
    const W = 360
    const H = 120
    const pad = { l: 42, r: 4, t: 6, b: 18 }
    const iw = W - pad.l - pad.r
    const ih = H - pad.t - pad.b
    const totals = labels.map((_, i) =>
      props.stacked ? series.reduce((s, x) => s + (x.values[i] || 0), 0) : Math.max.apply(null, [0].concat(series.map(x => x.values[i] || 0)))
    )
    const lineVals = lines.reduce((all, l) => all.concat(l.values.map(v => v || 0)), [])
    const max = Math.max.apply(null, [1].concat(totals, lineVals))
    const bw = iw / n
    const y = v => pad.t + ih - (v / max) * ih
    const kids = []
    ;[0, max / 2, max].forEach(v => {
      kids.push(h('line', { key: 'g' + v, x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), stroke: 'currentColor', opacity: 0.12 }))
      kids.push(h('text', { key: 't' + v, x: pad.l - 3, y: y(v) + 3, textAnchor: 'end', fontSize: 10, fill: 'currentColor', opacity: 0.8 }, compact(v)))
    })
    labels.forEach((_, i) => {
      let base = 0
      series.forEach((s, si) => {
        const v = s.values[i] || 0
        if (!v) return
        const w = props.stacked ? bw * 0.7 : (bw * 0.7) / series.length
        const x = pad.l + i * bw + bw * 0.15 + (props.stacked ? 0 : w * si)
        const top = y(props.stacked ? base + v : v)
        const bottom = y(props.stacked ? base : 0)
        kids.push(h('rect', { key: s.key + i, x: x, y: top, width: Math.max(1, w), height: Math.max(0.5, bottom - top), fill: s.color, opacity: hover === -1 || hover === i ? 0.9 : 0.45 }))
        if (props.stacked) base += v
      })
    })
    lines.forEach(l => {
      const pts = l.values.map((v, i) => (v == null ? null : pad.l + i * bw + bw / 2 + ',' + y(v))).filter(Boolean)
      kids.push(h('polyline', { key: 'l' + l.key, points: pts.join(' '), fill: 'none', stroke: l.color, strokeWidth: 1.6, strokeDasharray: l.dash ? '4 3' : undefined }))
    })
    const every = Math.max(1, Math.ceil(n / 4))
    labels.forEach((label, i) => {
      if ((i % every === 0 && n - 1 - i >= every / 2) || i === n - 1) kids.push(h('text', { key: 'x' + i, x: i === n - 1 ? W - pad.r : pad.l + i * bw + bw / 2, y: H - 3, textAnchor: i === n - 1 ? 'end' : 'middle', fontSize: 10, fill: 'currentColor', opacity: 0.8 }, shortDate(label)))
    })
    labels.forEach((_, i) => {
      kids.push(h('rect', { key: 'h' + i, x: pad.l + i * bw, y: pad.t, width: bw, height: ih, fill: 'transparent', onMouseEnter: () => setHover(i), onClick: () => setHover(i) }))
    })
    const legend = series.concat(lines)
    const at = hover >= 0 ? hover : n - 1
    return h(
      'div',
      { className: 'ini-card ini-chart', 'data-ini': props.testId },
      h('div', { className: 'ini-h4' }, props.title),
      h('div', { className: 'ini-muted', 'data-ini': props.testId + '-readout' }, shortDate(labels[at], props.withTime) + ': ' + legend.map(s => s.label + ' ' + (s.values[at] == null ? '–' : compact(s.values[at]))).join(' · ')),
      h('svg', { viewBox: '0 0 ' + W + ' ' + H, width: '100%', role: 'img', 'aria-label': props.title, onMouseLeave: () => setHover(-1) }, kids),
      h(
        'div',
        { className: 'ini-legend' },
        legend.map(s => h('span', { key: s.key, className: 'ini-legend__item' }, h('span', { className: 'ini-legend__dot', style: { background: s.color } }), s.label))
      )
    )
  }

  function Metrics(props) {
    const [days, setDaysState] = useState(() => {
      const saved = Number(readLocal(METRIC_KEY, '30'))
      return METRIC_DAYS.indexOf(saved) >= 0 ? saved : 30
    })
    const setDays = d => {
      setDaysState(d)
      writeLocal(METRIC_KEY, String(d))
    }
    const [state, reload] = useResource('/projects/' + encodeURIComponent(props.project) + '/metrics?days=' + days)
    const data = state.data
    const picker = h(
      'div',
      { className: 'ini-chips' },
      METRIC_DAYS.map(d =>
        h('button', { key: d, type: 'button', className: cx('ini-chip', d === days && 'ini-chip--on'), onClick: () => setDays(d), 'data-ini': 'metrics-days-' + d }, d + ' d')
      ),
      h(Btn, { variant: 'outline', icon: true, disabled: state.loading, onClick: reload, testId: 'metrics-refresh', title: 'Refresh', ariaLabel: 'Refresh' }, state.loading ? '…' : '↻')
    )
    if (!data) {
      return h(
        'div',
        { className: 'ini-stack', 'data-ini': 'metrics' },
        picker,
        state.error ? h('div', { className: 'ini-bad' }, 'Could not read metrics: ' + state.error) : h(Muted, null, 'Loading…')
      )
    }
    const tot = data.totals
    const prev = data.previous.totals
    const s = data.series
    const labels = s.map(b => b.start)
    const per = data.window.bucket === 'week' ? 'week' : 'day'
    const causes = (data.causes || []).filter(c => s.some(b => b.failed_by_cause[c]))
    const history = data.history || []
    const lanes = (data.lanes || []).filter(l => l.runs || l.sessions)
    return h(
      'div',
      { className: 'ini-stack', 'data-ini': 'metrics' },
      h(
        'div',
        null,
        h('div', { className: 'ini-h2' }, 'Kanban health · ' + data.board),
        h(
          Muted,
          null,
          localTime(data.window.start) + ' → ' + localTime(data.window.end) + ' (' + (per === 'week' ? 'weekly' : 'daily') + ') · vs ' + shortDate(data.previous.start) + ' → ' + shortDate(data.previous.end)
        )
      ),
      picker,
      h(
        'div',
        { className: 'ini-kpis', 'data-ini': 'metrics-kpis' },
        h(Kpi, { testId: 'kpi-delivered', label: 'Delivered', value: compact(tot.delivered), hint: tot.delivered_pr + ' with a PR', d: delta(tot.delivered, prev.delivered, false), prev: prev.delivered }),
        h(Kpi, { testId: 'kpi-lead', label: 'Lead time p50', value: hrs(tot.lead_p50_h), hint: 'p90 ' + hrs(tot.lead_p90_h), d: delta(tot.lead_p50_h, prev.lead_p50_h, true), prev: hrs(prev.lead_p50_h) }),
        h(Kpi, { testId: 'kpi-failed', label: 'Failed runs', value: pct(tot.failed_rate), hint: tot.runs_failed + ' of ' + tot.runs + ' · ' + tot.failed_hours + ' h', d: delta(tot.failed_rate, prev.failed_rate, true), prev: pct(prev.failed_rate) }),
        h(Kpi, { testId: 'kpi-first-pass', label: 'First-pass review', value: pct(tot.first_pass_rate), hint: tot.reviewed_deliveries + ' reviewed', d: delta(tot.first_pass_rate, prev.first_pass_rate, false), prev: pct(prev.first_pass_rate) }),
        h(Kpi, { testId: 'kpi-runs-per', label: 'Runs / delivery p50', value: compact(tot.runs_per_delivery_p50), hint: 'p90 ' + (tot.runs_per_delivery_p90 == null ? '–' : tot.runs_per_delivery_p90), d: delta(tot.runs_per_delivery_p50, prev.runs_per_delivery_p50, true), prev: prev.runs_per_delivery_p50 }),
        h(Kpi, { testId: 'kpi-tokens', label: 'Fresh tokens / delivery', value: compact(tot.tokens_fresh_per_delivery), hint: compact(tot.tokens_fresh) + ' fresh · ' + compact(tot.tokens_cache) + ' cache', d: delta(tot.tokens_fresh_per_delivery, prev.tokens_fresh_per_delivery, true), prev: compact(prev.tokens_fresh_per_delivery) })
      ),
      h(
        'div',
        { className: 'ini-grid' },
      h(Chart, {
        testId: 'chart-flow',
        title: 'Delivery per ' + per,
        labels: labels,
        stacked: true,
        series: [
          { key: 'pr', label: 'with PR', color: 'var(--ini-good)', values: s.map(b => b.delivered_pr) },
          { key: 'other', label: 'delivered', color: 'color-mix(in srgb, var(--ini-good) 45%, transparent)', values: s.map(b => b.delivered - b.delivered_pr) }
        ],
        lines: [{ key: 'created', label: 'created', color: 'var(--ini-primary)', values: s.map(b => b.created), dash: true }]
      }),
      h(Chart, {
        testId: 'chart-failures',
        title: 'Failed runs by cause per ' + per,
        labels: labels,
        stacked: true,
        series: causes.map(c => ({ key: c, label: (CAUSE[c] || [c])[0], color: (CAUSE[c] || [c, '#888'])[1], values: s.map(b => b.failed_by_cause[c] || 0) })),
        lines: [{ key: 'runs', label: 'runs', color: 'var(--ini-muted)', values: s.map(b => b.runs), dash: true }]
      }),
      h(Chart, {
        testId: 'chart-review',
        title: 'Review rounds per ' + per,
        labels: labels,
        series: [
          { key: 'reviews', label: 'reviews', color: 'var(--ini-primary)', values: s.map(b => b.reviews) },
          { key: 'changes', label: 'changes requested', color: 'var(--ini-warn)', values: s.map(b => b.changes_requested) }
        ]
      }),
      h(Chart, {
        testId: 'chart-tokens',
        title: 'Fresh tokens per ' + per,
        labels: labels,
        series: [{ key: 'fresh', label: 'fresh tokens', color: 'var(--ini-primary)', values: s.map(b => b.tokens_fresh) }]
      }),
      history.length > 1
        ? h(Chart, {
            testId: 'chart-snapshots',
            title: 'Board state (hourly snapshots)',
            labels: history.map(x => x.at),
            withTime: true,
            lines: [
              { key: 'open', label: 'open', color: 'var(--ini-primary)', values: history.map(x => x.open) },
              { key: 'blocked', label: 'blocked', color: 'var(--ini-warn)', values: history.map(x => x.blocked) },
              { key: 'needs', label: 'need you', color: 'var(--ini-bad)', values: history.map(x => x.needs_you) }
            ]
          })
        : h(Muted, null, 'Snapshots start now: one is stored at most hourly while this page is opened, so the board-state chart fills over time.')
      ),
      h(
        'div',
        { className: 'ini-card', 'data-ini': 'metrics-now' },
        h('div', { className: 'ini-h4' }, 'Right now · ' + localTime(data.now.at)),
        h(
          'div',
          { className: 'ini-row ini-wrap' },
          h('span', { className: 'ini-pill ini-pill--muted' }, data.now.open + ' open'),
          h('span', { className: cx('ini-pill', data.now.counts.blocked ? 'ini-pill--warn' : 'ini-pill--muted') }, (data.now.counts.blocked || 0) + ' blocked'),
          h('span', { className: cx('ini-pill', data.now.needs_you ? 'ini-pill--bad' : 'ini-pill--muted') }, data.now.needs_you + ' need you')
        ),
        (data.now.oldest_blocked || []).slice(0, 5).map(card =>
          h('div', { key: card.id, className: 'ini-task-meta' }, h(CardRef, { id: card.id }), ' · ' + card.title + ' — since ' + shortDate(card.since, true) + ' · ' + hrs(card.age_h) + ' · ' + card.kind.replace(/_/g, ' '))
        )
      ),
      lanes.length
        ? h(
            'div',
            { className: 'ini-card', 'data-ini': 'metrics-lanes' },
            h('div', { className: 'ini-h4' }, 'Lanes'),
            lanes.map(l =>
              h(
                'div',
                { key: l.profile, className: 'ini-task-meta' },
                h('strong', null, l.profile),
                ' · ' + l.runs + ' runs · ' + l.runs_failed + ' failed (' + pct(l.failed_rate) + ') · ' + l.run_hours + ' h · ' + compact(l.tokens_fresh) + ' fresh'
              )
            )
          )
        : null,
      h(
        'details',
        { className: 'ini-muted' },
        h('summary', null, 'How these are counted'),
        h(
          'ul',
          { className: 'ini-notes' },
          ['Generated ' + localTime(data.generated_at) + ' (' + data.timezone + ', UTC' + data.utc_offset + ')'].concat(data.notes || []).map((n, i) => h('li', { key: i }, n))
        )
      )
    )
  }

  // ---- page ----------------------------------------------------------------------------------------------

  // The Initiatives badge adds two different things; its label names both so neither section count looks wrong.
  function badgeText(review) {
    const open = review ? (review.queue || []).length : 0
    const waiting = review ? (review.waiting_on_you || []).length : 0
    return open + ' to decide · ' + waiting + ' waiting on you'
  }

  // Initiatives (with their reviews), Tasks, Bugs and Metrics; a tab saved by an older page ("review") opens Initiatives.
  const TABS = ['initiatives', 'tasks', 'bugs', 'metrics']
  const normalizeTab = value => (TABS.indexOf(value) >= 0 ? value : 'initiatives')

  function InitiativesMobilePage() {
    const [projectsState] = useResource('/projects')
    const projects = (projectsState.data && projectsState.data.projects) || []
    const [projectId, setProjectId] = useState(() => readLocal(PROJECT_KEY, ''))
    const [tab, setTabState] = useState(() => normalizeTab(readLocal(TAB_KEY, 'initiatives')))
    // The open initiative and its cycle live in page memory, so a dashboard tab switch and back reopens them.
    const [where, setWhere] = useState(() => (memory.project && memory.project === readLocal(PROJECT_KEY, '') ? { id: memory.initiative, cycle: memory.cycle } : { id: '', cycle: '' }))
    const initiativeId = where.id
    const setInitiativeId = (id, cycle) => setWhere({ id: id || '', cycle: id ? cycle || '' : '' })
    const [view, setViewState] = useState(() => (readLocal(VIEW_KEY, 'list') === 'board' ? 'board' : 'list'))
    const setView = next => {
      setViewState(next)
      writeLocal(VIEW_KEY, next)
    }
    const [cardId, setCardId] = useState('')
    const closeCard = useCallback(() => setCardId(''), [])
    const cards = React.useMemo(() => ({ open: setCardId }), [])
    const [reviewCount, setReviewCount] = useState(null)
    const [taskCount, setTaskCount] = useState(null)
    const [bugCount, setBugCount] = useState(null)
    const [notice, setNotice] = useState(null)
    const project = projects.find(p => p.id === projectId) || projects[0] || null
    const setTab = next => {
      setTabState(next)
      writeLocal(TAB_KEY, next)
    }
    const pickProject = id => {
      setProjectId(id)
      writeLocal(PROJECT_KEY, id)
      setInitiativeId('')
      setCardId('')
      setTaskCount(null)
      setBugCount(null)
      setNotice(null)
    }
    // The tab count is the selected project's, read without opening the tab.
    const projectKey = project ? project.id : ''
    const [reviewState, reloadReview] = useResource(projectKey ? '/review?project=' + encodeURIComponent(projectKey) : null)
    const review = reviewState.data
    useEffect(() => {
      if (review) setReviewCount((review.queue || []).length + (review.waiting_on_you || []).length)
    }, [review])
    // A saved tab this page no longer has is rewritten, so a stale "review" never comes back.
    useEffect(() => {
      if (readLocal(TAB_KEY, '') !== tab) writeLocal(TAB_KEY, tab)
    }, [])
    const reviewDone = result => {
      if (result && result.state) setNotice(result)
      reloadReview()
    }
    useEffect(() => {
      if (!projectKey) return undefined
      let live = true
      call('/projects/' + encodeURIComponent(projectKey) + '/tasks')
        .then(data => {
          if (!live) return
          const open = new Set(data.open_statuses || ['open', 'triaged', 'in_progress'])
          const active = (data.tasks || []).filter(task => open.has(task.status))
          setTaskCount(active.filter(task => task.type !== 'bug').length)
          setBugCount(active.filter(task => task.type === 'bug').length)
        })
        .catch(() => undefined)
      return () => {
        live = false
      }
    }, [projectKey])
    useEffect(() => {
      if (!projectKey) return
      memory.project = projectKey
      memory.initiative = initiativeId
      memory.cycle = where.cycle
    }, [projectKey, initiativeId, where.cycle])
    const pageRef = useRef(null)
    // The dashboard scrolls <main>, not the window. Each screen keeps its own offset: an initiative opens at its
    // top, and Back returns to the list where it was left, once the remembered rows have painted.
    const screen = !projectKey ? '' : tab !== 'initiatives' ? projectKey + ':' + tab : initiativeId ? projectKey + ':initiative:' + initiativeId : projectKey + ':list:' + view
    // A board tile opens its initiative at one cycle: the detail scrolls to that cycle, so no offset is restored.
    const jumping = tab === 'initiatives' && Boolean(initiativeId && where.cycle)
    useEffect(() => {
      const find = () => {
        let node = pageRef.current && pageRef.current.parentElement
        while (node && node !== document.body) {
          if (/(auto|scroll)/.test(window.getComputedStyle(node).overflowY)) return node
          node = node.parentElement
        }
        return null
      }
      const node = find()
      const target = node || window
      const read = () => (node ? node.scrollTop : window.scrollY)
      const write = y => (node ? (node.scrollTop = y) : window.scrollTo(0, y))
      const saved = jumping ? 0 : (screen && memory.scroll.get(screen)) || 0
      const stop = holdScroll({ read, write, saved, watch: pageRef.current, input: window })
      const onScroll = () => {
        if (!stop.holding() && screen) memory.scroll.set(screen, read())
      }
      target.addEventListener('scroll', onScroll, { passive: true })
      if (!jumping) write(saved)
      return () => {
        stop()
        target.removeEventListener('scroll', onScroll)
      }
    }, [screen, jumping])
    return h(CardContext.Provider, { value: cards }, h(
      'div',
      { className: 'ini-m', 'data-ini': 'page', ref: pageRef },
      projects.length > 1
        ? h(
            'header',
            { className: 'ini-header' },
            h('span', { className: 'ini-header__label' }, 'Project'),
            h(
              'select',
              { className: 'ini-select', value: project ? project.id : '', onChange: event => pickProject(event.target.value), 'aria-label': 'Project' },
              projects.map(p => h('option', { key: p.id, value: p.id }, p.label || p.id))
            )
          )
        : null,
      h(
        'nav',
        { className: 'ini-tabs', role: 'tablist' },
        h(
          'button',
          { type: 'button', role: 'tab', 'aria-selected': tab === 'initiatives', className: cx('ini-tab', tab === 'initiatives' && 'ini-tab--on'), onClick: () => setTab('initiatives'), 'data-ini': 'tab-initiatives' },
          h('span', null, 'Initiatives'),
          reviewCount ? h('span', { className: 'ini-tab__badge', title: badgeText(review), 'aria-label': badgeText(review) }, String(reviewCount)) : null
        ),
        h(
          'button',
          { type: 'button', role: 'tab', 'aria-selected': tab === 'tasks', className: cx('ini-tab', tab === 'tasks' && 'ini-tab--on'), onClick: () => setTab('tasks'), 'data-ini': 'tab-tasks' },
          'Tasks' + (taskCount === null ? '' : ' (' + taskCount + ')')
        ),
        h(
          'button',
          { type: 'button', role: 'tab', 'aria-selected': tab === 'bugs', className: cx('ini-tab', tab === 'bugs' && 'ini-tab--on'), onClick: () => setTab('bugs'), 'data-ini': 'tab-bugs' },
          'Bugs' + (bugCount === null ? '' : ' (' + bugCount + ')')
        ),
        h(
          'button',
          { type: 'button', role: 'tab', 'aria-selected': tab === 'metrics', className: cx('ini-tab', tab === 'metrics' && 'ini-tab--on'), onClick: () => setTab('metrics'), 'data-ini': 'tab-metrics' },
          'Metrics'
        )
      ),
      projectsState.error && !projectsState.data ? h('div', { className: 'ini-bad' }, 'Could not read projects: ' + projectsState.error) : null,
      projectsState.data && projectsState.data.settings_error ? h('div', { className: 'ini-bad' }, 'Settings file is invalid: ' + projectsState.data.settings_error) : null,
      !projectsState.data
        ? h(Muted, null, 'Loading…')
        : !project
          ? h('div', { className: 'ini-empty' }, 'No initiative projects configured.')
          : tab === 'metrics'
            ? h(Metrics, { key: project.id + '-metrics', project: project.id })
          : tab === 'tasks' || tab === 'bugs'
            ? h(Tasks, { key: project.id + '-' + tab, project: project, kind: tab === 'bugs' ? 'bug' : 'task', onCount: tab === 'bugs' ? setBugCount : setTaskCount })
            : initiativeId
            ? h(InitiativeDetail, {
                key: project.id + initiativeId,
                project: project.id,
                id: initiativeId,
                review,
                reviewState,
                onReviewRefresh: reloadReview,
                notice,
                onCloseNotice: () => setNotice(null),
                onReviewDone: reviewDone,
                cycle: where.cycle,
                onBack: () => setInitiativeId('')
              })
            : h(InitiativeList, {
                key: project.id,
                project: project.id,
                onOpen: setInitiativeId,
                review,
                reviewState,
                onReviewRefresh: reloadReview,
                notice,
                onCloseNotice: () => setNotice(null),
                onReviewDone: reviewDone,
                view,
                onView: setView
              }),
      cardId && project ? h(CardSheet, { key: 'card-' + cardId, project: project.id, id: cardId, onClose: closeCard }) : null
    ))
  }

  PLUGINS.register(NAME, InitiativesMobilePage)
})()
