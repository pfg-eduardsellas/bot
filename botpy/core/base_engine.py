import asyncio
import os
import re
import urllib.robotparser
from typing import List, Set, Dict, Any, Optional
from urllib.parse import urlparse

from botpy.models.action import Action, ActionType
from botpy.scrapers.detector import Detector
from botpy.core.accessibility import setup_axe


class BaseEngine:
    """Setup and helpers shared by both crawling engines."""

    def __init__(
        self,
        start_url: str,
        max_pages: int = 15,
        max_depth: int = 3,
        max_actions: int = 50,
        form_data: dict = None,
        in_domain: bool = False,
        accessibility: bool = True,
        owner_token: Optional[str] = None,
        log_fn=None,
    ):
        self.start_url = start_url
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.max_actions = max_actions
        self.in_domain = in_domain
        self.accessibility = accessibility
        self.start_domain = urlparse(start_url).netloc

        self.actions_graph: Dict[int, Action] = {}
        self.known_selectors: Set[str] = set()
        self.processed_urls: Set[str] = set()
        self.captured_errors: List[str] = []
        self.current_id_counter = 0

        self.detector = Detector(form_data=form_data or {})
        self.log = log_fn if log_fn is not None else print

        self._owner_token: Optional[str] = owner_token
        self._verify_ownership: bool = (
            os.getenv("BOT_VERIFY_OWNERSHIP", "true").lower() != "false"
        )

        self._robot_parser: Optional[urllib.robotparser.RobotFileParser] = None
        self._crawl_delay: float = 0.0

        ua = os.getenv("BOT_USER_AGENT", "")
        match = re.search(r";\s*([A-Za-z][A-Za-z0-9_-]+)/[\d.]+", ua)
        self._bot_name: str = match.group(1) if match else "PFGBot"

    # region action graph management

    def _register_action(self, action: Action):
        """Add action to the graph without queuing."""
        action.log_fn = self.log
        self.actions_graph[action.id] = action
        self.known_selectors.add(action.selector)
        self.current_id_counter = max(self.current_id_counter, action.id)

    def get_path_to_action(self, target_id: int) -> List[Action]:
        """Return the ordered list of actions to replay to reach target_id."""
        path: List[Action] = []
        curr_id = target_id
        while curr_id in self.actions_graph:
            action = self.actions_graph[curr_id]
            if action.id != target_id:
                path.append(action)
            if not action.predecessors or action.type == ActionType.URL:
                break
            curr_id = action.predecessors[0]
        return list(reversed(path))

    # endregion

    # region robots and domain checks

    def _load_robots(self):
        """Read the site's robots.txt and its crawl delay."""
        parsed = urlparse(self.start_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        try:
            rp.read()
            delay = rp.crawl_delay(self._bot_name) or rp.crawl_delay("*") or 0
            self._crawl_delay = float(delay)
            self.log(
                f"[Robots] {robots_url} loaded "
                f"(agent={self._bot_name}, crawl-delay={self._crawl_delay}s)"
            )
        except Exception as e:
            self.log(
                f"[Robots] Could not read robots.txt: {e} — proceeding without restrictions"
            )
        self._robot_parser = rp

    def _is_allowed(self, url: str) -> bool:
        """Check whether robots.txt allows crawling a URL."""
        if self._robot_parser is None:
            return True
        return self._robot_parser.can_fetch(self._bot_name, url)

    # endregion

    # region authentication check

    async def _check_ownership(self, page: Any) -> bool:
        """Return True if <meta name="testify" content="{token}"> is present."""
        if not self._verify_ownership or not self._owner_token:
            return True
        try:
            content = await page.evaluate(
                """
                (token) => {
                    const meta = document.querySelector('meta[name="testify"]');
                    return meta ? meta.getAttribute('content') : null;
                }
                """,
                self._owner_token,
            )
            return content == self._owner_token
        except Exception:
            return False

    # endregion

    # region helpers

    def _on_console_message(self, msg):
        """Capture console errors raised by the page."""
        if msg.type == "error":
            self.captured_errors.append(msg.text)

    def _on_request_failed(self, request):
        """Capture network-level failures (DNS, refused, timeout, blocked)."""
        failure = request.failure
        # ERR_ABORTED is usually a request cancelled by navigation, not a real
        # error — skip it to avoid false positives.
        if failure and "ERR_ABORTED" not in failure:
            self.captured_errors.append(
                f"Network request failed ({failure}): {request.url}"
            )

    def _on_response(self, response):
        """Capture HTTP error responses (4xx/5xx) that never reach the console."""
        if response.status >= 400:
            self.captured_errors.append(
                f"HTTP {response.status} on {response.request.method} {response.url}"
            )

    async def _wait_for_page(self, page: Any):
        """Wait for DOM + network to settle after an action."""
        await page.wait_for_load_state("domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            self.log("Network didn't settle, but proceeding anyway...")
        await asyncio.sleep(max(2.0, self._crawl_delay))

    # endregion

    # region browser setup

    async def _setup_browser(self, playwright):
        """Launch Chromium and open a page wired to the error listeners."""
        headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
        user_agent = os.getenv("BOT_USER_AGENT")
        browser = await playwright.chromium.launch(headless=headless)
        page = await browser.new_page(
            **({"user_agent": user_agent} if user_agent else {})
        )
        page.on("console", self._on_console_message)
        page.on("requestfailed", self._on_request_failed)
        page.on("response", self._on_response)
        return browser, page

    async def _init_accessibility(self, page: Any) -> bool:
        """Inject axe-core when accessibility analysis is enabled for the scan."""
        if not self.accessibility:
            self.log("[Accessibility] Accessibility analysis disabled for this scan.")
            return False
        axe_ready = await setup_axe(page)
        if axe_ready:
            self.log("[Accessibility] axe-core loaded and registered.")
        else:
            self.log(
                "[Accessibility] axe-core unavailable — accessibility scans skipped."
            )
        return axe_ready

    # endregion
