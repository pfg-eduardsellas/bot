import json
import os
from typing import List, Dict, Any, Tuple
from pydantic import BaseModel
from google import genai
from google.genai import types
from playwright.async_api import async_playwright

from botpy.core.base_engine import BaseEngine
from botpy.models.action import Action, ActionType, ActionRetryError
from botpy.actions.url_action import URLAction
from botpy.core.accessibility import run_full_scan


class AgentDecision(BaseModel):
    reasoning: str
    element_id: int
    action_type: str  # "click" or "type"
    text_to_type: str | None
    goal_achieved: bool


class IAScanEngine(BaseEngine):

    def __init__(self, *args, objective: str, max_steps: int = 15, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_goal = objective
        self.max_steps = max_steps
        self.ia_history: List[str] = []

    # region element detection

    async def _get_ia_elements(
        self, page: Any, pre_selectors: set = None
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[int, Action]]:
        """
        Detect all interactive elements with full metadata for the AI prompt.

        Returns (new_elements, existing_elements, action_map).
        new_elements: appeared after the last action (not in pre_selectors).
        existing_elements: were already present before the last action.
        When pre_selectors is empty/None (first step or after navigation), all
        elements go into existing_elements so the prompt uses a flat list.
        """
        self.detector.simplified = False
        try:
            detected = await self.detector.detect(page, self.current_id_counter)
        finally:
            self.detector.simplified = True

        new_elements: List[Dict[str, Any]] = []
        existing_elements: List[Dict[str, Any]] = []
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

            is_new = bool(pre_selectors) and action.selector not in pre_selectors
            if is_new:
                new_elements.append(info)
            else:
                existing_elements.append(info)
            action_map[temp_id] = action

        return new_elements, existing_elements, action_map

    # endregion

    # region action execution

    async def _execute_ia_action(
        self,
        page: Any,
        decision: AgentDecision,
        action_map: Dict[int, Action],
    ) -> bool:
        """Execute the action chosen by the AI. Returns True on success."""
        action = action_map.get(decision.element_id)
        if action is None:
            self.log(f"[IA] Invalid element_id {decision.element_id}.")
            return False

        action.log_fn = self.log

        if action.type != ActionType.URL:
            try:
                if not await action.get_locator(page).is_visible():
                    self.log(
                        f"[IA] Element {decision.element_id} "
                        f"({action.selector[:40]}) not visible, skipping."
                    )
                    return False
            except Exception:
                pass  # If visibility check fails, attempt the action anyway

        try:
            await action.execute(page)
            return True
        except ActionRetryError:
            # IA mode does not re-queue actions, so a retry-requested failure is
            # simply treated as a (recoverable) miss for this step.
            self.log(
                f"[IA] Element {decision.element_id} not ready "
                f"(not retried in IA mode), skipping."
            )
            return False
        except Exception as e:
            self.log(f"[IA] Execution error on element {decision.element_id}: {e}")
            return False

    # endregion

    # region main loop

    async def run(self):
        """AI-driven scan: Gemini picks one action per step toward target_goal."""
        self.ia_history = []

        async with async_playwright() as p:
            browser, page = await self._setup_browser(p)
            try:
                await self._run_loop(page)
            finally:
                # Always persist whatever graph we managed to build, even if the
                # loop aborted on an unexpected error.
                await browser.close()
                self.unify_urls()
                self.save_graph()

    async def _run_loop(self, page: Any):
        self._load_robots()
        axe_ready = await self._init_accessibility(page)

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
            self.log("[IA] Ownership verification failed. Aborting.")
            return

        client = genai.Client()
        pre_selectors: set = set()
        consecutive_api_errors = 0
        # current_url_action: last URLAction — used for depth tracking on navigation.
        # current_parent: last executed action of any type — used for graph linking.
        current_parent: Action = start_action

        for step in range(self.max_steps):
            if len(self.actions_graph) >= self.max_actions:
                self.log(f"[IA] max_actions={self.max_actions} reached. Stopping.")
                break

            self.log(f"[IA] Step {step + 1}/{self.max_steps} — {page.url}")

            new_elements, existing_elements, action_map = await self._get_ia_elements(
                page, pre_selectors
            )
            all_elements = new_elements + existing_elements
            # Snapshot current selectors so next step can detect what's new.
            current_selectors = {a.selector for a in action_map.values()}

            if not all_elements:
                self.log("[IA] No interactive elements found. Stopping.")
                break

            history_text = "\n".join(self.ia_history) if self.ia_history else "None"

            if pre_selectors and new_elements:
                prompt = (
                    f"OBJECTIVE: {self.target_goal}\n\n"
                    f"CURRENT URL: {page.url}\n\n"
                    f"NEW ELEMENTS (generated by your last action — prioritize these):\n"
                    f"{json.dumps(new_elements, ensure_ascii=False, indent=2)}\n\n"
                    f"OTHER AVAILABLE ELEMENTS (existed before your last action):\n"
                    f"{json.dumps(existing_elements, ensure_ascii=False, indent=2)}\n\n"
                    f"PREVIOUS STEPS:\n{history_text}"
                )
            else:
                prompt = (
                    f"OBJECTIVE: {self.target_goal}\n\n"
                    f"CURRENT URL: {page.url}\n\n"
                    f"AVAILABLE ELEMENTS:\n"
                    f"{json.dumps(all_elements, ensure_ascii=False, indent=2)}\n\n"
                    f"PREVIOUS STEPS:\n{history_text}"
                )

            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AgentDecision,
                system_instruction=(
                    "You are a tactical automated QA agent. Your goal is to interact "
                    "with the web to fulfill the user's objective. "
                    "When NEW ELEMENTS are listed separately, they appeared as a result "
                    "of your last action — prioritize interacting with them unless they "
                    "are not relevant to the objective. "
                    "Avoid repeating actions already listed in PREVIOUS STEPS. "
                    "Reason through your response before choosing."
                ),
            )

            try:
                response = await client.aio.models.generate_content(
                    model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
                    contents=prompt,
                    config=config,
                )
                decision = AgentDecision.model_validate_json(response.text)
                consecutive_api_errors = 0
            except Exception as e:
                consecutive_api_errors += 1
                self.log(
                    f"[IA] Gemini API error at step {step + 1} "
                    f"({consecutive_api_errors} in a row): {e}"
                )
                if consecutive_api_errors >= 3:
                    self.log("[IA] Too many consecutive Gemini errors. Stopping.")
                    break
                continue

            self.log(
                f"[IA] → {decision.action_type} on element {decision.element_id} | "
                f"{decision.reasoning}"
            )

            if decision.goal_achieved:
                self.log(
                    f"[IA] Goal achieved after {step + 1} step(s): {self.target_goal}"
                )
                break

            pre_url = page.url
            try:
                await page.evaluate("console.clear()")
            except Exception:
                pass  # console.clear is best-effort; never abort a step over it.
            self.captured_errors = []

            success = await self._execute_ia_action(page, decision, action_map)
            if not success:
                pre_selectors = current_selectors
                elem_info = next(
                    (e for e in all_elements if e["id"] == decision.element_id), {}
                )
                self.ia_history.append(
                    f"Step {step + 1}: FAILED {decision.action_type} on "
                    f"[{elem_info.get('type', '?')}] "
                    f"'{elem_info.get('text', '?')[:40]}' (element not accessible)"
                )
                continue

            await self._wait_for_page(page)
            post_url = page.url

            chosen = action_map.get(decision.element_id)
            if chosen is not None:
                self._register_action(chosen)
                current_parent.add_successor(chosen.id)
                chosen.add_predecessor(current_parent.id)
                chosen.errors = self.captured_errors.copy()

                if post_url != pre_url:
                    existing = next(
                        (a for a in self.actions_graph.values()
                         if a.type == ActionType.URL and a.value == post_url),
                        None,
                    )
                    if existing is not None:
                        chosen.add_successor(existing.id)
                        existing.add_predecessor(chosen.id)
                        current_url_action = existing
                        current_parent = existing
                    else:
                        new_depth = current_url_action.depth + 1
                        over_limit = (
                            len(self.processed_urls) >= self.max_pages
                            or new_depth > self.max_depth
                        )
                        if over_limit:
                            self.log(
                                f"[IA] Navigation limit reached "
                                f"(pages={len(self.processed_urls)}/{self.max_pages}, "
                                f"depth={new_depth}/{self.max_depth}). Stopping."
                            )
                            break
                        self.current_id_counter += 1
                        nav_action = URLAction(
                            id=self.current_id_counter,
                            url=post_url,
                            depth=new_depth,
                        )
                        self._register_action(nav_action)
                        chosen.add_successor(nav_action.id)
                        nav_action.add_predecessor(chosen.id)
                        current_url_action = nav_action
                        current_parent = nav_action
                        self.processed_urls.add(post_url)
                else:
                    current_parent = chosen

            if axe_ready:
                try:
                    violations = await run_full_scan(page)
                except Exception as e:
                    self.log(f"[IA] Accessibility scan failed: {e}")
                    violations = []
                if chosen is not None:
                    chosen.accessibility_violations = violations
                if violations:
                    self.log(
                        f"[IA] Accessibility: {len(violations)} violation(s) on {page.url}"
                    )

            elem_info = next(
                (e for e in all_elements if e["id"] == decision.element_id), {}
            )
            history_line = (
                f"Step {step + 1}: {decision.action_type} on "
                f"[{elem_info.get('type', '?')}] "
                f"'{elem_info.get('text', '?')[:40]}'"
                + (f" typed:'{decision.text_to_type}'" if decision.text_to_type else "")
                + f" → {post_url}"
            )
            self.ia_history.append(history_line)

            pre_selectors = set() if post_url != pre_url else current_selectors
        else:
            self.log(
                f"[IA] max_steps={self.max_steps} reached without achieving goal."
            )

    # endregion
