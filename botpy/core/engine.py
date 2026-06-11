import asyncio
import json
import os
import re
import urllib.robotparser
from collections import deque
from typing import List, Set, Dict, Any, Optional, Tuple
from urllib.parse import urlparse
from playwright.async_api import async_playwright
from google import genai
from google.genai import types
from pydantic import BaseModel

from botpy.models.action import Action, ActionType, ActionRetryError
from botpy.scrapers.detector import Detector
from botpy.actions.url_action import URLAction
from botpy.core.accessibility import setup_axe, run_full_scan, run_partial_scan


class AgentDecision(BaseModel):
    reasoning: str
    element_id: int
    action_type: str  # "click" or "type"
    text_to_type: str | None
    goal_achieved: bool


class Engine:
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

        self.page_queues: Dict[str, deque[Action]] = {}
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

    def _get_queue(self, url: str) -> deque:
        if url not in self.page_queues:
            self.page_queues[url] = deque()
        return self.page_queues[url]

    def add_action(self, action: Action, page_url: str):
        """Register action and push it to the front of its page queue."""
        self._register_action(action)
        self._get_queue(page_url).appendleft(action)

    def _next_action(self, current_url: str) -> Tuple[Optional[Action], str]:
        """Pop the next action: prefer current page, else first page with pending actions."""
        if current_url in self.page_queues and self.page_queues[current_url]:
            return self.page_queues[current_url].popleft(), current_url
        for url, queue in self.page_queues.items():
            if queue:
                return queue.popleft(), url
        return None, current_url

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

    def _is_same_domain(self, url: str) -> bool:  # is correct?
        return urlparse(url).netloc == self.start_domain

    def _load_robots(self):
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
        if self._robot_parser is None:
            return True
        return self._robot_parser.can_fetch(self._bot_name, url)

    # endregion

    # region autentication check

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
        if msg.type == "error":
            self.captured_errors.append(msg.text)

    async def execute_path(self, page: Any, path: List[Action]):
        """Replay a sequence of actions to restore browser state."""
        for action in path:
            try:
                await action.execute(page)
                await self._wait_for_page(page)
            except Exception as e:
                self.log(f"Path execution failed at action {action.id}: {e}")
                break

    async def _backtrack_to_element(
        self, page: Any, action: Action
    ) -> Tuple[bool, Optional[Action]]:
        """
        Replay predecessors to restore the state where action's element is visible.
        Returns (success, first_action_in_path).
        """
        path = self.get_path_to_action(action.id)
        new_start = path[0] if path else None
        await self.execute_path(page, path)
        if not await action.get_locator(page).is_visible():
            msg = f"Element not visible after backtracking: {action.selector}"
            action.errors.append(msg)
            self.log(f"Failed to recover element for Action {action.id}.")
            return False, new_start
        return True, new_start

    async def _wait_for_page(self, page: Any):
        """Wait for DOM + network to settle after an action."""
        await page.wait_for_load_state("domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            self.log("Network didn't settle, but proceeding anyway...")
        await asyncio.sleep(max(2.0, self._crawl_delay))

    async def _snapshot_selectors(self, page: Any) -> Set[str]:
        """Return the set of selectors currently detectable on the page."""
        detected = await self.detector.detect(page, self.current_id_counter)
        return {a.selector for a in detected}

    async def _resolve_url_navigation(
        self,
        page: Any,
        current_action: Action,
        post_url: str,
        start_action: Action,
    ) -> Tuple[Optional[URLAction], Action, Action, bool]:
        """
        Called when a non-URL action caused a page navigation.

        Returns (new_url_action, parent_for_new_actions, updated_start_action, blocked).
        blocked=True means ownership failed and the caller should skip this iteration.
        new_url_action is None when the URL was already known, out of limits, or out-of-domain.
        """
        if not await self._check_ownership(page):
            msg = 'Missing <meta name="testify" content="[token]"> — ownership not verified'
            self.log(f"[Ownership] {msg} on {post_url}")
            current_action.errors.append(msg)
            return None, current_action, start_action, True

        # If this URL is already in the graph, just link and move on
        for action in self.actions_graph.values():
            if action.type == ActionType.URL and action.value == post_url:
                current_action.add_successor(action.id)
                action.add_predecessor(current_action.id)
                return None, current_action, start_action, False

        # Respect page and depth limits
        over_limit = (
            len(self.processed_urls) >= self.max_pages
            and start_action.depth >= self.max_depth
        )
        if over_limit or not self._is_allowed(post_url):
            if not self._is_allowed(post_url):
                self.log(f"[Robots] Blocked discovered URL: {post_url}")
            return None, current_action, start_action, False

        # Create a new URL node (navigation already happened, don't queue it)
        self.current_id_counter += 1
        url_action = URLAction(
            id=self.current_id_counter,
            url=post_url,
            depth=start_action.depth + 1,
        )
        self.processed_urls.add(post_url)
        current_action.add_successor(url_action.id)
        url_action.add_predecessor(current_action.id)
        self._register_action(url_action)

        if self.in_domain and not self._is_same_domain(post_url):
            self.log(f"Skipping out-of-domain URL: {post_url}")
            return None, current_action, start_action, False

        return url_action, url_action, url_action, False

    # endregion
    # region accessibility scanning

    async def _scan_accessibility(
        self,
        page: Any,
        current_action: Action,
        new_url_action: Optional[URLAction],
        new_actions: List[Action],
        post_url: str,
        axe_ready: bool,
    ):
        if not axe_ready:
            return

        if current_action.type == ActionType.URL:
            current_action.accessibility_violations = await run_full_scan(page)
            if current_action.accessibility_violations:
                self.log(
                    f"[Accessibility] Full scan: "
                    f"{len(current_action.accessibility_violations)} violation(s) on {current_action.value}"
                )
        elif new_url_action is not None:
            new_url_action.accessibility_violations = await run_full_scan(page)
            if new_url_action.accessibility_violations:
                self.log(
                    f"[Accessibility] Full scan: "
                    f"{len(new_url_action.accessibility_violations)} violation(s) on {post_url}"
                )
        elif new_actions:
            selectors = [a.selector for a in new_actions]
            current_action.accessibility_violations = await run_partial_scan(
                page, selectors
            )
            if current_action.accessibility_violations:
                self.log(
                    f"[Accessibility] Partial scan: "
                    f"{len(current_action.accessibility_violations)} violation(s) "
                    f"after action {current_action.id}"
                )

    # endregion
    # region processing action

    async def _process_action(
        self,
        page: Any,
        current_action: Action,
        start_action: Action,
        axe_ready: bool,
        current_url: str,
    ) -> Tuple[Action, str]:
        """
        Execute one action and handle all side effects.
        Returns the (possibly updated) start_action for depth tracking.
        """
        # Robots check for URL actions
        if current_action.type == ActionType.URL and not self._is_allowed(
            current_action.value
        ):
            self.log(f"[Robots] Blocked by robots.txt: {current_action.value}")
            return start_action, current_url

        # Ensure the element is still visible; backtrack if needed
        if current_action.type != ActionType.URL:
            if not await current_action.get_locator(page).is_visible():
                success, new_start = await self._backtrack_to_element(
                    page, current_action
                )
                if new_start:
                    start_action = new_start
                if not success:
                    return start_action, current_url

        pre_url = (
            current_action.value if current_action.type == ActionType.URL else page.url
        )
        pre_selectors = await self._snapshot_selectors(page)

        # Execute
        await page.evaluate("console.clear()")
        self.captured_errors = []
        await current_action.execute(page)
        await self._wait_for_page(page)
        current_action.errors = self.captured_errors.copy()

        post_url = (
            current_action.value if current_action.type == ActionType.URL else page.url
        )
        parent_for_new_actions = current_action
        new_url_action = None

        # Handle navigation to a new URL
        if post_url != pre_url and current_action.type != ActionType.URL:
            new_url_action, parent_for_new_actions, start_action, blocked = (
                await self._resolve_url_navigation(
                    page, current_action, post_url, start_action
                )
            )
            if blocked:
                return start_action, current_url

        # Detect elements that appeared after the action
        post_actions = await self.detector.detect(page, self.current_id_counter)
        new_actions = [
            a
            for a in post_actions
            if a.selector not in pre_selectors
            and a.selector not in self.known_selectors
        ]

        await self._scan_accessibility(
            page, current_action, new_url_action, new_actions, post_url, axe_ready
        )

        for action in reversed(new_actions):
            parent_for_new_actions.add_successor(action.id)
            action.add_predecessor(parent_for_new_actions.id)
            self.add_action(action, post_url)

        return start_action, post_url

    # endregion
    # region browser setup

    async def _setup_browser(self, playwright):
        headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
        user_agent = os.getenv("BOT_USER_AGENT")
        browser = await playwright.chromium.launch(headless=headless)
        page = await browser.new_page(
            **({"user_agent": user_agent} if user_agent else {})
        )
        page.on("console", self._on_console_message)
        return browser, page

    async def _init_accessibility(self, page: Any) -> bool:
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
    # region main loop
    async def run(self):
        async with async_playwright() as p:
            browser, page = await self._setup_browser(p)
            self._load_robots()
            axe_ready = await self._init_accessibility(page)

            self.current_id_counter += 1
            start_action = URLAction(
                id=self.current_id_counter, url=self.start_url, depth=0
            )
            self.add_action(start_action, self.start_url)

            current_url = self.start_url
            while True:
                if len(self.actions_graph) >= self.max_actions:
                    break

                current_action, current_url = self._next_action(current_url)
                if current_action is None:
                    break

                self.log(
                    f"Processing Action ID {current_action.id} "
                    f"({current_action.type.name}): "
                    f"{current_action.selector or current_action.value}"
                )

                try:
                    start_action, current_url = await self._process_action(
                        page, current_action, start_action, axe_ready, current_url
                    )
                except ActionRetryError:
                    self._get_queue(current_url).append(current_action)
                    current_url = page.url
                except Exception as e:
                    self.log(f"Error on action {current_action.id}: {e}")
                    current_url = page.url

            await browser.close()
            self.unify_urls()
            self.save_graph()

    async def _get_ia_elements(
        self, page: Any
    ) -> Tuple[List[Dict[str, Any]], Dict[int, Action]]:
        """Detect interactive elements and return a simplified list for the AI prompt
        alongside a mapping from temp sequential id to Action object."""
        detected = await self.detector.detect(page, self.current_id_counter)
        elements: List[Dict[str, Any]] = []
        action_map: Dict[int, Action] = {}

        for temp_id, action in enumerate(detected):
            info: Dict[str, Any] = {"id": temp_id}
            try:
                if action.type == ActionType.FORM:
                    info["type"] = "input"
                    fields = await page.evaluate(
                        """
                        (sel) => {
                            const el = document.querySelector(sel);
                            if (!el) return [];
                            const SEL = 'input:not([type="hidden"]):not([type="submit"])'
                                      + ':not([type="button"]):not([type="image"])'
                                      + ':not([type="reset"]), textarea, select';
                            return Array.from(el.querySelectorAll(SEL))
                                .filter(i => i.checkVisibility({ visibilityProperty: true }))
                                .map(i => ({
                                    type: i.getAttribute('type') || i.tagName.toLowerCase(),
                                    name: i.getAttribute('name') || i.getAttribute('id') || '',
                                    placeholder: i.getAttribute('placeholder') || '',
                                    label: (document.querySelector('label[for="' + i.id + '"]')
                                            || {}).textContent || ''
                                })).slice(0, 6);
                        }
                        """,
                        action.selector,
                    )
                    field_desc = ", ".join(
                        f"{f['type']}:{(f['label'] or f['placeholder'] or f['name'] or '?').strip()[:20]}"
                        for f in fields
                    )
                    info["text"] = f"Form({field_desc})" if field_desc else "Form"
                    info["context"] = f"selector:{action.selector[:60]}"

                elif action.type == ActionType.BUTTON:
                    info["type"] = "button"
                    locator = action.get_locator(page)
                    text = await locator.inner_text()
                    aria = await locator.get_attribute("aria-label") or ""
                    info["text"] = (text.strip() or aria.strip() or "button")[:60]
                    info["context"] = action.selector[:60]

                elif action.type == ActionType.LINK:
                    info["type"] = "link"
                    locator = page.locator(action.selector).first
                    text = await locator.inner_text()
                    href = await locator.get_attribute("href") or ""
                    info["text"] = text.strip()[:60] or href[:60]
                    info["context"] = f"href:{href[:60]}"

                else:
                    info["type"] = "other"
                    info["text"] = action.selector[:60]
                    info["context"] = ""

            except Exception:
                info["type"] = "unknown"
                info["text"] = action.selector[:60]
                info["context"] = ""

            elements.append(info)
            action_map[temp_id] = action

        return elements, action_map

    async def _execute_ia_action(
        self,
        page: Any,
        decision: "AgentDecision",
        action_map: Dict[int, Action],
    ) -> bool:
        """Execute the action chosen by the AI. Returns True on success."""
        action = action_map.get(decision.element_id)
        if action is None:
            self.log(f"[IA] Invalid element_id {decision.element_id}.")
            return False

        action.log_fn = self.log
        try:
            await action.execute(page)
            return True
        except ActionRetryError:
            self.log(f"[IA] Element {decision.element_id} not ready, skipping.")
            return False
        except Exception as e:
            self.log(f"[IA] Execution error on element {decision.element_id}: {e}")
            return False

    async def run_IA(self, target_goal: str, max_steps: int = 15):
        """AI-driven scan: Gemini picks one action per step toward target_goal."""
        self.ia_history: List[str] = []

        async with async_playwright() as p:
            browser, page = await self._setup_browser(p)
            self._load_robots()
            axe_ready = await self._init_accessibility(page)

            # Seed the graph with the start URL node
            self.current_id_counter += 1
            start_action = URLAction(
                id=self.current_id_counter, url=self.start_url, depth=0
            )
            self.processed_urls.add(self.start_url)
            self._register_action(start_action)
            current_url_action: Action = start_action

            await start_action.execute(page)
            await self._wait_for_page(page)

            if not await self._check_ownership(page):
                self.log("[IA] Ownership verification failed. Aborting run_IA.")
                await browser.close()
                return

            client = genai.Client()

            for step in range(max_steps):
                self.log(f"[IA] Step {step + 1}/{max_steps} — {page.url}")

                elements, action_map = await self._get_ia_elements(page)
                if not elements:
                    self.log("[IA] No interactive elements found. Stopping.")
                    break

                history_text = (
                    "\n".join(self.ia_history) if self.ia_history else "None"
                )
                prompt = (
                    f"OBJECTIVE: {target_goal}\n\n"
                    f"CURRENT URL: {page.url}\n\n"
                    f"AVAILABLE ELEMENTS:\n"
                    f"{json.dumps(elements, ensure_ascii=False, indent=2)}\n\n"
                    f"PREVIOUS STEPS:\n{history_text}"
                )

                config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=AgentDecision,
                    system_instruction=(
                        "Eres un agente de QA automatizado táctico. Tu fin es interactuar "
                        "con la web para cumplir el objetivo del usuario. Analiza los "
                        "elementos disponibles, evita repetir acciones del historial y "
                        "razona tu respuesta."
                    ),
                )

                try:
                    response = await client.aio.models.generate_content(
                        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
                        contents=prompt,
                        config=config,
                    )
                    decision = AgentDecision.model_validate_json(response.text)
                except Exception as e:
                    self.log(f"[IA] Gemini API error at step {step + 1}: {e}")
                    break

                self.log(
                    f"[IA] → {decision.action_type} on element {decision.element_id} | "
                    f"{decision.reasoning[:120]}"
                )

                if decision.goal_achieved:
                    self.log(
                        f"[IA] Goal achieved after {step + 1} step(s): {target_goal}"
                    )
                    break

                pre_url = page.url
                await page.evaluate("console.clear()")
                self.captured_errors = []

                success = await self._execute_ia_action(page, decision, action_map)
                if not success:
                    continue

                await self._wait_for_page(page)
                post_url = page.url

                # Register executed action in the graph
                chosen = action_map.get(decision.element_id)
                if chosen is not None:
                    self._register_action(chosen)
                    current_url_action.add_successor(chosen.id)
                    chosen.add_predecessor(current_url_action.id)
                    chosen.errors = self.captured_errors.copy()

                    # If navigation occurred, create a URL node for the new page
                    if post_url != pre_url:
                        self.current_id_counter += 1
                        nav_action = URLAction(
                            id=self.current_id_counter,
                            url=post_url,
                            depth=current_url_action.depth + 1,  # type: ignore[attr-defined]
                        )
                        self._register_action(nav_action)
                        chosen.add_successor(nav_action.id)
                        nav_action.add_predecessor(chosen.id)
                        current_url_action = nav_action
                        self.processed_urls.add(post_url)

                # Accessibility scan after each interaction
                if axe_ready:
                    violations = await run_full_scan(page)
                    if chosen is not None:
                        chosen.accessibility_violations = violations
                    if violations:
                        self.log(
                            f"[IA] Accessibility: {len(violations)} violation(s) on {page.url}"
                        )

                elem_info = next(
                    (e for e in elements if e["id"] == decision.element_id), {}
                )
                history_line = (
                    f"Step {step + 1}: {decision.action_type} on "
                    f"[{elem_info.get('type', '?')}] "
                    f"'{elem_info.get('text', '?')[:40]}'"
                    + (
                        f" typed:'{decision.text_to_type}'"
                        if decision.text_to_type
                        else ""
                    )
                    + f" → {post_url}"
                )
                self.ia_history.append(history_line)
            else:
                self.log(
                    f"[IA] max_steps={max_steps} reached without achieving goal."
                )

            await browser.close()
            self.unify_urls()
            self.save_graph()

    # endregion
    # region POST PROCESSING

    def unify_urls(self):
        """Merge duplicate URLAction nodes that share the same URL."""
        url_groups: Dict[str, List[int]] = {}
        for action_id, action in self.actions_graph.items():
            if action.type == ActionType.URL:
                url_groups.setdefault(action.value, []).append(action_id)

        for ids in url_groups.values():
            if len(ids) <= 1:
                continue

            canonical_id = ids[0]
            canonical = self.actions_graph[canonical_id]

            for other_id in ids[1:]:
                other = self.actions_graph[other_id]

                for pred_id in other.predecessors:
                    if pred_id != canonical_id:
                        canonical.add_predecessor(pred_id)
                for succ_id in other.successors:
                    if succ_id != canonical_id:
                        canonical.add_successor(succ_id)

                for action in self.actions_graph.values():
                    if other_id in action.predecessors:
                        action.predecessors = list(
                            dict.fromkeys(
                                canonical_id if x == other_id else x
                                for x in action.predecessors
                            )
                        )
                    if other_id in action.successors:
                        action.successors = list(
                            dict.fromkeys(
                                canonical_id if x == other_id else x
                                for x in action.successors
                            )
                        )

                del self.actions_graph[other_id]

        # Remove self-references introduced by merging
        for action in self.actions_graph.values():
            action.predecessors = [x for x in action.predecessors if x != action.id]
            action.successors = [x for x in action.successors if x != action.id]

    def save_graph(self):
        output = {"actions": [a.to_dict() for a in self.actions_graph.values()]}
        backend_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        out_path = os.path.join(backend_dir, "result.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=4)
        self.log(f"Engine finished. Graph saved to {out_path}")


# endregion
