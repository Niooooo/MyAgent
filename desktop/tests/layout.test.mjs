import assert from "node:assert/strict"
import test from "node:test"

import { nextCompactLayout } from "../renderer/layout.js"

test("responsive layout uses 880/920 hysteresis", () => {
  assert.equal(nextCompactLayout(null, 899), true)
  assert.equal(nextCompactLayout(null, 900), false)
  assert.equal(nextCompactLayout(true, 900), true)
  assert.equal(nextCompactLayout(true, 920), false)
  assert.equal(nextCompactLayout(false, 879), true)
  assert.equal(nextCompactLayout(false, 880), false)
})
