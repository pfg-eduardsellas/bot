import asyncio
import json
import os
from collections import deque
from typing import List, Set, Dict, Any
from urllib.parse import urlparse
from playwright.async_api import async_playwright

from botpy.models.action import Action, ActionType, ActionRetryError
from botpy.scrapers.detector import Detector
from botpy.actions.url_action import URLAction
from botpy.core.accessibility import setup_axe, run_full_scan, run_partial_scan


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
        log_fn=None,
    ):
        self.start_url = start_url
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.max_actions = max_actions
        self.in_domain = in_domain
        self.accessibility = accessibility
        self.start_domain = urlparse(start_url).netloc
        self.queue: deque[Action] = deque()
        self.actions_graph: Dict[int, Action] = {}
        self.detector = Detector(form_data=form_data or {})
        self.current_id_counter = 0
        self.processed_urls: Set[str] = set()

        self.known_selectors: Set[str] = set()
        self.captured_errors: List[str] = []
        self.log = log_fn if log_fn is not None else print

    def _is_same_domain(self, url: str) -> bool:
        return urlparse(url).netloc == self.start_domain

    def _on_console_message(self, msg):
        if msg.type == "error":
            self.captured_errors.append(msg.text)

    def add_action(self, action: Action, to_front: bool = False):
        action.log_fn = self.log
        self.actions_graph[action.id] = action
        self.known_selectors.add(action.selector)
        to_front = True
        if to_front:
            self.queue.appendleft(action)
        else:
            self.queue.append(action)
        self.current_id_counter = max(self.current_id_counter, action.id)

    async def execute_path(self, page: Any, path: List[Action]):
        """Executes a list of actions in order to reach a specific state."""
        for action in path:
            try:
                await action.execute(page)
                await page.wait_for_load_state("networkidle")
            except Exception as e:
                self.log(f"Path execution failed at action {action.id}: {e}")
                break

    def get_path_to_action(self, target_id: int) -> List[Action]:
        """Return path of actions to reach the target action."""
        # Simple BFS since it's a DAG (mostly)
        # Find the first URLAction (root) to start from?
        # Actually, we can just look for the path in the actions_graph
        # starting from the target and going backwards via predecessors.

        path = []
        curr_id = target_id
        while curr_id in self.actions_graph:
            action = self.actions_graph[curr_id]
            if action.id != target_id:
                path.append(action)
            if not action.predecessors or action.type == ActionType.URL:
                break
            # Pick the first predecessor for simplicity (assuming a tree-like flow)
            curr_id = action.predecessors[0]  # is [0] the sortest?

        # remove target action, we want to execute the path to the target action on engine
        return list(reversed(path))

    async def _snapshot_selectors(self, page: Any) -> Set[str]:
        """Return the set of selectors currently visible on the page."""
        detected = await self.detector.detect(page, self.current_id_counter)
        return {a.selector for a in detected}

    async def run(self):
        async with async_playwright() as p:
            headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
            browser = await p.chromium.launch(headless=headless)
            page = await browser.new_page()
            page.on("console", self._on_console_message)
            if self.accessibility:
                axe_ready = await setup_axe(page)
                if axe_ready:
                    self.log("[Accessibility] axe-core loaded and registered.")
                else:
                    self.log("[Accessibility] axe-core unavailable — accessibility scans skipped.")
            else:
                axe_ready = False
                self.log("[Accessibility] Accessibility analysis disabled for this scan.")

            # Initialize with the root URL action
            self.current_id_counter += 1
            start_action = URLAction(
                id=self.current_id_counter, url=self.start_url, depth=0
            )
            self.add_action(start_action)

            while self.queue:
                if len(self.actions_graph) >= self.max_actions:
                    break

                current_action = self.queue.popleft()
                self.log(
                    f"Processing Action ID {current_action.id} ({current_action.type.name}): {current_action.selector or current_action.value}"
                )

                try:
                    if current_action.type != ActionType.URL:
                        is_visible = await page.locator(
                            current_action.selector
                        ).first.is_visible()
                        if not is_visible:
                            # Backtracking
                            path = self.get_path_to_action(current_action.id)

                            start_action = path[0]  # update start action for depth
                            await self.execute_path(page, path)

                            # Re-verify visibility
                            if not await page.locator(
                                current_action.selector
                            ).first.is_visible():
                                self.log(
                                    f"Failed to recover element for Action {current_action.id}."
                                )
                                continue

                    # To check if the URL changed
                    if current_action.type == ActionType.URL:
                        pre_url = current_action.value
                    else:
                        pre_url = page.url

                    # DOM DIFF: snapshot BEFORE
                    pre_selectors = await self._snapshot_selectors(page)

                    # Clear captured errors before executing action
                    await page.evaluate("console.clear()")
                    self.captured_errors = []

                    # Execute the action
                    await current_action.execute(page)

                    # Wait for the page to settle
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(2)

                    # Capture errors after action settle
                    current_action.errors = self.captured_errors.copy()

                    if current_action.type == ActionType.URL:
                        post_url = current_action.value
                    else:
                        post_url = page.url
                    parent_for_new_actions = current_action

                    # URL CHANGED (New page)
                    if post_url != pre_url and current_action.type != ActionType.URL:

                        # Check if this URL already exists in our graph
                        existing_url_id = None
                        for aid, action in self.actions_graph.items():
                            if (
                                action.type == ActionType.URL
                                and action.value == post_url
                            ):
                                existing_url_id = aid
                                break

                        if existing_url_id:
                            current_action.add_successor(existing_url_id)
                            self.actions_graph[existing_url_id].add_predecessor(
                                current_action.id
                            )
                        else:
                            if (
                                len(self.processed_urls) < self.max_pages
                                or start_action.depth >= self.max_depth
                            ):
                                self.current_id_counter += 1
                                new_depth = start_action.depth + 1
                                url_action = URLAction(
                                    id=self.current_id_counter,
                                    url=post_url,
                                    depth=new_depth,
                                )
                                self.processed_urls.add(post_url)

                                # Link current_action -> url_action
                                current_action.add_successor(url_action.id)
                                url_action.add_predecessor(current_action.id)

                                if self.in_domain and not self._is_same_domain(
                                    post_url
                                ):
                                    self.log(f"Skipping out-of-domain URL: {post_url}")
                                    self.actions_graph[url_action.id] = url_action
                                    self.known_selectors.add(url_action.selector)
                                else:
                                    start_action = url_action
                                    self.add_action(url_action, to_front=True)
                                    parent_for_new_actions = url_action

                    # DIFF: detect AFTER
                    post_actions = await self.detector.detect(
                        page, self.current_id_counter
                    )

                    new_actions = [
                        a
                        for a in post_actions
                        if a.selector not in pre_selectors
                        and a.selector not in self.known_selectors
                    ]

                    # Accessibility scan: full on URL actions, partial on interactions with DOM changes
                    if axe_ready and current_action.type == ActionType.URL:
                        current_action.accessibility_violations = await run_full_scan(page)
                        if current_action.accessibility_violations:
                            self.log(f"[Accessibility] Full scan found {len(current_action.accessibility_violations)} violation(s) on {current_action.value}")
                    elif axe_ready and new_actions:
                        new_selectors = [a.selector for a in new_actions]
                        current_action.accessibility_violations = await run_partial_scan(page, new_selectors)
                        if current_action.accessibility_violations:
                            self.log(f"[Accessibility] Partial scan found {len(current_action.accessibility_violations)} violation(s) after action {current_action.id}")

                    for action in reversed(new_actions):
                        parent_for_new_actions.add_successor(action.id)
                        action.add_predecessor(parent_for_new_actions.id)
                        self.add_action(action, to_front=True)

                except ActionRetryError as e:
                    self.log(f"Action {current_action.id} not available,  queued for retry: {e}")
                    self.queue.append(current_action)
                except Exception as e:
                    self.log(f"Error on action {current_action.id}: {e}")

            await browser.close()
            self.unify_urls()
            self.save_graph()

    def unify_urls(self):
        """Unifies multiple URLActions with the same URL (value) into a single node."""
        from botpy.models.action import ActionType

        # 1. Group URL actions by their value
        url_groups: Dict[str, List[int]] = {}
        for action_id, action in self.actions_graph.items():
            if action.type == ActionType.URL:
                url_groups.setdefault(action.value, []).append(action_id)

        # 2. For each URL with multiple IDs, unify them
        for url_value, ids in url_groups.items():
            if len(ids) <= 1:
                continue

            canonical_id = ids[0]
            canonical_action = self.actions_graph[canonical_id]
            others = ids[1:]

            for other_id in others:
                other_action = self.actions_graph[other_id]

                for pred_id in other_action.predecessors:
                    if pred_id != canonical_id:
                        canonical_action.add_predecessor(pred_id)
                for succ_id in other_action.successors:
                    if succ_id != canonical_id:
                        canonical_action.add_successor(succ_id)

                # Update references in all other actions
                for action in self.actions_graph.values():
                    # Update predecessors lists
                    if other_id in action.predecessors:
                        action.predecessors = [
                            canonical_id if x == other_id else x
                            for x in action.predecessors
                        ]
                        action.predecessors = list(dict.fromkeys(action.predecessors))

                    # Update successors lists
                    if other_id in action.successors:
                        action.successors = [
                            canonical_id if x == other_id else x
                            for x in action.successors
                        ]
                        action.successors = list(dict.fromkeys(action.successors))

                # Remove the redundant action from the graph AFTER updating references
                if other_id in self.actions_graph:
                    del self.actions_graph[other_id]

        # Cleanup: Ensure no node has itself as predecessor/successor due to merging
        for action in self.actions_graph.values():
            if action.id in action.predecessors:
                action.predecessors.remove(action.id)
            if action.id in action.successors:
                action.successors.remove(action.id)

    def save_graph(self):
        import os

        output = {
            "actions": [action.to_dict() for action in self.actions_graph.values()]
        }
        # Write next to the backend root (two levels up from core/engine.py)
        backend_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        out_path = os.path.join(backend_dir, "result.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=4)
        self.log(f"Engine finished. Graph saved to {out_path}")
