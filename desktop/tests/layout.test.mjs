import assert from "node:assert/strict"
import test from "node:test"

import {
  nextCompactLayout,
  reconcileOpenSessionIds,
  sessionsInWorkspace,
} from "../renderer/layout.js"

test("responsive layout uses 880/920 hysteresis", () => {
  assert.equal(nextCompactLayout(null, 899), true)
  assert.equal(nextCompactLayout(null, 900), false)
  assert.equal(nextCompactLayout(true, 900), true)
  assert.equal(nextCompactLayout(true, 920), false)
  assert.equal(nextCompactLayout(false, 879), true)
  assert.equal(nextCompactLayout(false, 880), false)
})

test("history is scoped to the active workspace", () => {
  const sessions = [
    { id: 1, workspace: "D:\\workspace-a" },
    { id: 2, workspace: "D:\\workspace-b" },
    { id: 3, workspace: "D:\\workspace-a" },
  ]
  assert.deepEqual(
    sessionsInWorkspace(sessions, "D:\\workspace-a").map((session) => session.id),
    [1, 3],
  )
})

test("open tabs retain valid order and always include the active session", () => {
  const sessions = [{ id: 1 }, { id: 2 }, { id: 3 }]
  assert.deepEqual(reconcileOpenSessionIds(new Set([3, 99, 1]), sessions, 2), [3, 1, 2])
  assert.deepEqual(reconcileOpenSessionIds([], sessions, 2), [2])
})
