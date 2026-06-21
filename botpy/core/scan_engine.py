from collections import deque
from typing import List, Set, Dict, Any, Optional, Tuple
from playwright.async_api import async_playwright

from botpy.core.base_engine import BaseEngine
from botpy.models.action import Action, ActionType, ActionRetryError
from botpy.actions.url_action import URLAction
from botpy.core.accessibility import run_full_scan, run_partial_scan


class ScanEngine(BaseEngine):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.page_queues: Dict[str, deque[Action]] = {}

    # region queue management

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

    # endregion

    # region helpers

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

    async def _snapshot_selectors(self, page: Any) -> Set[str]:
        """Return the set of selectors currently detectable on the page."""
        detected = await self.detector.detect(page, self.current_id_counter)
        return {a.selector for a in detected}

    # endregion

    # region url navigation

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

        for action in self.actions_graph.values():
            if action.type == ActionType.URL and action.value == post_url:
                current_action.add_successor(action.id)
                action.add_predecessor(current_action.id)
                return None, current_action, start_action, False

        over_limit = (
            len(self.processed_urls) >= self.max_pages
            and start_action.depth >= self.max_depth
        )
        if over_limit or not self._is_allowed(post_url):
            if not self._is_allowed(post_url):
                self.log(f"[Robots] Blocked discovered URL: {post_url}")
            return None, current_action, start_action, False

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

    # region process action

    async def _process_action(
        self,
        page: Any,
        current_action: Action,
        start_action: Action,
        axe_ready: bool,
        current_url: str,
    ) -> Tuple[Action, str]:
        """Execute one action and handle all side effects."""
        if current_action.type == ActionType.URL and not self._is_allowed(
            current_action.value
        ):
            self.log(f"[Robots] Blocked by robots.txt: {current_action.value}")
            return start_action, current_url

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

        if post_url != pre_url and current_action.type != ActionType.URL:
            new_url_action, parent_for_new_actions, start_action, blocked = (
                await self._resolve_url_navigation(
                    page, current_action, post_url, start_action
                )
            )
            if blocked:
                return start_action, current_url

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
            # self.unify_urls()
            self.save_graph()

    # endregion
