import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"


class WelcomePanelParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inside = False
        self.depth = 0
        self.children: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if not self.inside and tag == "section" and attributes.get("id") == "welcome-panel":
            self.inside = True
            self.depth = 1
            return
        if self.inside:
            if self.depth == 1:
                self.children.append(tag)
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self.inside:
            return
        self.depth -= 1
        if self.depth == 0:
            self.inside = False

    def handle_data(self, data: str) -> None:
        if self.inside and data.strip():
            self.text.append(data.strip())


class DesktopAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (DESKTOP / "renderer" / "index.html").read_text(encoding="utf-8")
        cls.app = (DESKTOP / "renderer" / "app.js").read_text(encoding="utf-8")
        cls.css = (DESKTOP / "renderer" / "styles.css").read_text(encoding="utf-8")
        cls.layout = (DESKTOP / "renderer" / "layout.js").read_text(encoding="utf-8")
        cls.main = (DESKTOP / "main.mjs").read_text(encoding="utf-8")
        cls.preload = (DESKTOP / "preload.cjs").read_text(encoding="utf-8")

    def test_welcome_panel_contains_only_one_logo_canvas(self) -> None:
        parser = WelcomePanelParser()
        parser.feed(self.html)
        self.assertEqual(parser.children, ["canvas"])
        self.assertEqual(parser.text, [])
        self.assertEqual(self.html.count('id="welcome-logo"'), 1)
        combined = "\n".join((self.html, self.app, self.css)).lower()
        self.assertNotIn("topology", combined)
        self.assertNotIn("拓扑", combined)

    def test_logo_is_drawn_once_and_resize_only_updates_layout_class(self) -> None:
        self.assertEqual(self.app.count("drawWelcomeLogo()"), 2)
        self.assertIn(
            'window.addEventListener("resize", updateResponsiveLayout, { passive: true })',
            self.app,
        )
        resize_binding = re.search(
            r'window\.addEventListener\("resize"[^\n]+',
            self.app,
        )
        self.assertIsNotNone(resize_binding)
        self.assertNotIn("drawWelcomeLogo", resize_binding.group(0))
        self.assertNotIn("Configure", self.app)

    def test_hysteresis_and_action_positions_are_structurally_stable(self) -> None:
        self.assertIn("COMPACT_ENTER_WIDTH = 880", self.layout)
        self.assertIn("COMPACT_EXIT_WIDTH = 920", self.layout)
        self.assertIn(
            "grid-template-columns: max-content max-content minmax(0, 1fr) 92px",
            self.css,
        )
        compact_rule = re.search(
            r"body\.compact \.header-workspace,\s*body\.compact #send-hint\s*\{([^}]+)\}",
            self.css,
        )
        self.assertIsNotNone(compact_rule)
        self.assertIn("visibility: hidden", compact_rule.group(1))
        self.assertNotIn("grid-template", compact_rule.group(1))

    def test_model_page_has_an_explicit_return_to_conversation(self) -> None:
        self.assertRegex(
            self.html,
            r'id="model-back-button"[^>]*>←(?:&nbsp;)+返回对话</button>',
        )
        self.assertIn(
            'elements.modelBack.addEventListener("click", showConversation)',
            self.app,
        )

    def test_model_registration_includes_optional_base_url(self) -> None:
        self.assertIn('id="model-base-url-input"', self.html)
        self.assertIn('name="baseUrl"', self.html)
        self.assertIn("baseUrl: elements.modelBaseUrl.value", self.app)
        self.assertIn('model.baseUrl || "使用全局或 OpenAI 默认地址"', self.app)

    def test_busy_turn_keeps_prompt_editable_and_only_locks_send(self) -> None:
        self.assertIn("elements.prompt.disabled = state.closing", self.app)
        self.assertNotIn(
            'elements.prompt.disabled = session.state !== "idle"',
            self.app,
        )
        self.assertIn("elements.send.disabled = !session.canSend || state.closing", self.app)

    def test_renderer_handles_incremental_sidecar_output(self) -> None:
        self.assertIn('message.event === "stream-start"', self.app)
        self.assertIn('message.event === "stream-delta"', self.app)
        self.assertIn('article.className = "message streaming"', self.app)
        self.assertIn(".message.streaming .message-text::after", self.css)

    def test_renderer_is_isolated_and_launcher_defaults_to_myagent_electron(self) -> None:
        self.assertIn("contextIsolation: true", self.main)
        self.assertIn("nodeIntegration: false", self.main)
        self.assertIn("sandbox: true", self.main)
        self.assertIn('preload: path.join(desktopRoot, "preload.cjs")', self.main)
        self.assertIn('["-X", "utf8", "-u", "-m", "myagent.desktop_sidecar"]', self.main)
        self.assertIn('PYTHONIOENCODING: "utf-8"', self.main)
        self.assertIn('require("electron")', self.preload)
        self.assertFalse((DESKTOP / "preload.mjs").exists())
        self.assertIn('window.myagent.request(\"app.snapshot\")', self.main)
        self.assertIn('document.querySelector("#model-base-url-input")', self.main)
        self.assertIn("Model dialog does not expose the Base URL setting", self.main)
        self.assertIn(
            "Busy turn must keep the prompt editable and lock only send",
            self.main,
        )
        self.assertIn("Renderer did not incrementally render sidecar output", self.main)
        self.assertIn("Final state did not replace the streaming message", self.main)
        self.assertIn('if (action === "minimize") mainWindow.minimize()', self.main)
        self.assertIn('if (action === "close") mainWindow.close()', self.main)
        self.assertIn('document.querySelector("[data-window-action=maximize]").click()', self.main)
        self.assertIn('class="window-glyph window-minimize"', self.html)
        self.assertIn('class="window-glyph window-maximize"', self.html)
        self.assertIn('class="window-glyph window-close-glyph"', self.html)
        self.assertNotIn('elements.maximize.textContent', self.app)
        self.assertIn('.window-controls button:focus-visible', self.css)
        launcher = (ROOT / "start_gui.cmd").read_text(encoding="utf-8")
        self.assertIn("electron\\dist\\electron.exe", launcher)
        self.assertIn('if /i "%~1"=="--tk" goto tk_start', launcher)
        default_start = launcher.index('start "MyAgent"')
        fallback_start = launcher.index('start "MyAgent Tk fallback"')
        self.assertLess(default_start, fallback_start)


if __name__ == "__main__":
    unittest.main()
