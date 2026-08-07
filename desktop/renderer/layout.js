export const COMPACT_ENTER_WIDTH = 880
export const COMPACT_EXIT_WIDTH = 920
export const COMPACT_INITIAL_WIDTH = (COMPACT_ENTER_WIDTH + COMPACT_EXIT_WIDTH) / 2

export function nextCompactLayout(current, width) {
  if (current === null) return width < COMPACT_INITIAL_WIDTH
  if (current) return width < COMPACT_EXIT_WIDTH
  return width < COMPACT_ENTER_WIDTH
}
