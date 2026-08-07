import DOMPurify from "../node_modules/dompurify/dist/purify.es.mjs"
import { marked } from "../node_modules/marked/lib/marked.esm.js"

marked.setOptions({
  breaks: true,
  gfm: true,
})

function renderMarkdown(target, source) {
  try {
    const rendered = marked.parse(String(source || ""), { async: false })
    target.innerHTML = DOMPurify.sanitize(rendered, {
      FORBID_ATTR: ["style"],
      FORBID_TAGS: ["style"],
      USE_PROFILES: { html: true },
    })
  } catch {
    target.textContent = String(source || "")
  }
}

export { renderMarkdown }
