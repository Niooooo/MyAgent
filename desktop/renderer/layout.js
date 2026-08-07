export const COMPACT_ENTER_WIDTH = 880
export const COMPACT_EXIT_WIDTH = 920
export const COMPACT_INITIAL_WIDTH = (COMPACT_ENTER_WIDTH + COMPACT_EXIT_WIDTH) / 2

export function nextCompactLayout(current, width) {
  if (current === null) return width < COMPACT_INITIAL_WIDTH
  if (current) return width < COMPACT_EXIT_WIDTH
  return width < COMPACT_ENTER_WIDTH
}

export function sessionsInWorkspace(sessions, workspace) {
  if (!Array.isArray(sessions) || typeof workspace !== "string") return []
  return sessions.filter((session) => session?.workspace === workspace)
}

export function reconcileOpenSessionIds(openIds, sessions, activeSessionId) {
  const available = new Set(
    Array.isArray(sessions) ? sessions.map((session) => session?.id) : [],
  )
  const reconciled = []
  for (const sessionId of openIds || []) {
    if (available.has(sessionId) && !reconciled.includes(sessionId)) reconciled.push(sessionId)
  }
  if (available.has(activeSessionId) && !reconciled.includes(activeSessionId)) {
    reconciled.push(activeSessionId)
  }
  return reconciled
}
