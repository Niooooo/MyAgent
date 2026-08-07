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
        cls.sidecar = (ROOT / "src" / "myagent" / "desktop_sidecar.py").read_text(
            encoding="utf-8"
        )
        cls.gui = (ROOT / "src" / "myagent" / "gui.py").read_text(encoding="utf-8")

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

    def test_original_ma_mark_is_used_in_brand_welcome_and_window_icon(self) -> None:
        self.assertIn('viewBox="0 0 120 120"', self.html)
        self.assertIn("M24 88V35L60 75L96 35V88", self.html)
        self.assertIn("M68 66L96 80", self.html)
        self.assertIn("context.lineTo(204, 107)", self.app)
        self.assertIn("[35, 33, 46, 40]", self.main)
        self.assertNotIn('<span class="brand-mark" aria-hidden="true">M</span>', self.html)
        self.assertIn('const BACKGROUND = "#0b0e10"', self.main)
        self.assertIn("--app: #0b0e10", self.css)
        self.assertIn('_APP_BACKGROUND = "#0b0e10"', self.gui)
        self.assertIn("(18, 16, 23, 20)", self.gui)
        self.assertIn("self.header_logo.create_line", self.gui)

    def test_hysteresis_and_action_positions_are_structurally_stable(self) -> None:
        self.assertIn("COMPACT_ENTER_WIDTH = 880", self.layout)
        self.assertIn("COMPACT_EXIT_WIDTH = 920", self.layout)
        self.assertIn(
            "grid-template-columns: max-content minmax(0, 1fr) 92px",
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

    def test_agent_configuration_replaces_runtime_kind_selection(self) -> None:
        self.assertIn('id="agent-config"', self.html)
        self.assertIn('id="main-agent-model-selector"', self.html)
        self.assertIn('id="sub-agent-model-selector"', self.html)
        self.assertNotIn('id="model-selector"', self.html)
        self.assertNotIn('id="kind-selector"', self.html)
        self.assertIn('"canConfigureAgents": controller.state == "idle"', self.sidecar)
        self.assertIn('request("session.configure_agent_model"', self.app)
        self.assertIn('role: "main"', self.app)
        self.assertIn('role: "sub"', self.app)
        self.assertIn("!session.canConfigureAgents", self.app)
        self.assertNotIn('request("session.set_kind"', self.app)
        self.assertNotIn("对话与 AgentTeammate", self.html)
        self.assertNotIn("run_subagent 与 fork_subagent", self.html)
        self.assertNotIn(".agent-config-menu small", self.css)
        self.assertRegex(
            self.css,
            r"\.agent-config-menu\s*\{[^}]*width:\s*240px",
        )

    def test_workspace_history_opens_saved_conversations_as_tabs(self) -> None:
        self.assertIn('id="history-sidebar"', self.html)
        self.assertIn('id="history-list"', self.html)
        self.assertIn('id="history-sidebar-toggle"', self.html)
        self.assertIn("sessionsInWorkspace(state.sessions, active.workspace)", self.app)
        self.assertIn("openedSessionIds.add(sessionId)", self.app)
        self.assertIn("openHistorySession(session.id)", self.app)
        self.assertIn("closeSessionTab(session.id)", self.app)
        self.assertNotIn('request("session.close", { sessionId: session.id })', self.app)
        self.assertIn("关闭标签（不会删除历史）", self.app)
        self.assertIn("Workspace history did not reopen the conversation in a tab", self.main)
        self.assertIn("myagent.historySidebarCollapsed", self.app)
        self.assertIn('classList.toggle("sidebar-collapsed", sidebarCollapsed)', self.app)
        self.assertIn("body.sidebar-collapsed #history-list", self.css)
        self.assertIn("History sidebar collapse and restore behavior failed", self.main)

    def test_settings_and_agent_config_dismissal_are_wired(self) -> None:
        self.assertIn('id="settings-button"', self.html)
        self.assertIn('id="settings-form"', self.html)
        self.assertIn('request("settings.update", payload', self.app)
        self.assertIn('"settings.update",', self.main)
        self.assertIn('"settings.update": self._update_settings', self.sidecar)
        self.assertIn("!elements.agentConfig.contains(event.target)", self.app)
        self.assertIn('window.addEventListener("blur", closeAgentConfig)', self.app)
        self.assertIn('window.addEventListener("resize", closeAgentConfig', self.app)
        self.assertRegex(self.css, r"#settings-dialog\s*\{[^}]*overflow:\s*hidden")
        self.assertRegex(self.css, r"#settings-dialog form\s*\{[^}]*width:\s*100%")
        self.assertIn("settingsDialog.scrollWidth <= settingsDialog.clientWidth", self.main)
        self.assertIn("settingsFormBounds.right <= settingsBounds.right + 1", self.main)

    def test_user_and_agent_cards_render_before_submit_request_resolves(self) -> None:
        send_start = self.app.index("async function send()")
        optimistic = self.app.index("session.messages.push(optimisticMessage)", send_start)
        agent_placeholder = self.app.index('streamingMessages.set(session.id, "")', send_start)
        clear_input = self.app.index('elements.prompt.value = ""', send_start)
        request = self.app.index('await request("session.submit"', send_start)
        self.assertLess(optimistic, request)
        self.assertLess(agent_placeholder, request)
        self.assertLess(clear_input, request)
        self.assertIn("streamingMessages.delete(session.id)", self.app)
        self.assertIn('text.textContent = "正在思考"', self.app)
        self.assertIn('text.classList.remove("thinking")', self.app)
        self.assertIn(".message-text.thinking::after", self.css)
        self.assertIn("@keyframes thinking-pulse", self.css)
        self.assertIn("current.messages.indexOf(optimisticMessage)", self.app)
        self.assertIn("Object.assign(current, previous)", self.app)

    def test_renderer_handles_incremental_sidecar_output(self) -> None:
        self.assertIn('message.event === "stream-start"', self.app)
        self.assertIn('message.event === "stream-delta"', self.app)
        self.assertIn('createMessageCard("MyAgent", streamText, session.id)', self.app)
        self.assertIn('renderMarkdown(text, streamingMessages.get(sessionId))', self.app)
        self.assertIn(".message.streaming .message-text:not(.thinking)::after", self.css)

    def test_messages_are_separate_role_aligned_cards_with_safe_markdown(self) -> None:
        markdown = (DESKTOP / "renderer" / "markdown.js").read_text(encoding="utf-8")
        self.assertIn('article.className = `message ${role}', self.app)
        self.assertIn('role === "assistant"', self.app)
        self.assertIn("renderMarkdown(text, content)", self.app)
        self.assertIn('from "../node_modules/marked/lib/marked.esm.js"', markdown)
        self.assertIn('from "../node_modules/dompurify/dist/purify.es.mjs"', markdown)
        self.assertIn("DOMPurify.sanitize", markdown)
        self.assertIn("align-items: flex-start", self.css)
        self.assertIn(".message.user", self.css)
        self.assertIn("align-self: flex-end", self.css)
        self.assertIn("margin-left: auto", self.css)
        self.assertIn(".message.assistant", self.css)
        self.assertIn(".markdown-body pre", self.css)

    def test_errors_use_a_five_second_toast_and_tabs_can_be_renamed(self) -> None:
        self.assertIn('id="error-toast"', self.html)
        self.assertIn("const ERROR_TOAST_DURATION_MS = 5000", self.app)
        self.assertIn("showErrorToast(error)", self.app)
        self.assertIn("elements.errorToast.hidden = true", self.app)
        self.assertNotIn("setStatus(error?.message || String(error), true)", self.app)
        self.assertIn('title.addEventListener("dblclick"', self.app)
        self.assertIn('tab.addEventListener("contextmenu"', self.app)
        self.assertIn("右键可修改名称或删除对话", self.app)
        self.assertIn("双击或按 F2 修改名称", self.app)
        self.assertIn('id="tab-context-menu"', self.html)
        self.assertIn('id="rename-tab-menu-item"', self.html)
        self.assertIn('id="delete-tab-menu-item"', self.html)
        self.assertIn(">修改名称</button>", self.html)
        self.assertIn(">删除对话</button>", self.html)
        self.assertIn("renameTabFromContextMenu", self.app)
        self.assertIn('request("session.rename"', self.app)
        self.assertIn('"session.rename",', self.main)
        self.assertIn('"session.delete",', self.main)
        self.assertIn('"session.rename": self._rename_session', self.sidecar)
        self.assertIn('"session.delete": self._delete_session', self.sidecar)
        self.assertIn('request("session.delete"', self.app)
        self.assertIn("这会删除本机持久化记录", self.app)
        self.assertIn(".tab-title-input", self.css)
        self.assertIn(".tab-context-menu", self.css)

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
            "Busy turn must keep the prompt editable and lock Agent configuration",
            self.main,
        )
        self.assertIn("Renderer did not incrementally render Markdown output", self.main)
        self.assertIn("Message cards, Markdown safety, or Agent configuration failed", self.main)
        self.assertIn("User and Agent message cards were not rendered before the sidecar response", self.main)
        self.assertIn("Rejected optimistic messages did not restore the prompt", self.main)
        self.assertIn("Conversation rename context menu did not open the editor", self.main)
        self.assertIn("Conversation title rename did not persist", self.main)
        self.assertIn("Cancelled conversation deletion changed persisted state", self.main)
        self.assertIn("Confirmed conversation deletion removed the wrong tab", self.main)
        self.assertIn("Desktop request errors are not shown as a clean toast", self.main)
        self.assertIn("Desktop request error toast did not dismiss after five seconds", self.main)
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
