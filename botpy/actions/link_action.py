import asyncio
from typing import Any

from botpy.models.action import Action, ActionType

NAV_TIMEOUT_MS = 8000


class LinkAction(Action):
    """Action that clicks a link and follows wherever it navigates."""

    def __init__(self, id: int, selector: str, href: str, custom_id: str = ""):
        super().__init__(
            id, ActionType.LINK, selector=selector, value=href, custom_id=custom_id
        )
        self.name = href

    async def execute(self, page: Any):
        """Click the link and resolve whether it navigated in place or opened a new tab."""
        self.log_fn(f"Executing LinkAction: Navigating to {self.value} via click")
        pre_url = page.url

        # Start listening before the click so a target="_blank" tab isn't missed.
        popup_task = asyncio.create_task(page.wait_for_event("popup"))
        try:
            await page.click(self.selector, timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            popup_task.cancel()
            self.log_fn(f"  -> Click failed: {e}")
            return

        nav_task = asyncio.create_task(
            page.wait_for_url(
                lambda url: url != pre_url,
                wait_until="load",
                timeout=NAV_TIMEOUT_MS,
            )
        )
        done, pending = await asyncio.wait(
            {popup_task, nav_task},
            timeout=NAV_TIMEOUT_MS / 1000,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

        def _succeeded(task):
            return task in done and not task.cancelled() and task.exception() is None

        navigated = _succeeded(nav_task)
        popup = popup_task.result() if _succeeded(popup_task) else None

        if popup is not None:
            if navigated:
                # Navigated in place AND opened a tab: keep the main-tab
                # navigation and just discard the stray tab.
                try:
                    await popup.close()
                except Exception:
                    pass
            else:
                await self._follow_popup(page, popup)
        elif navigated:
            return
        elif page.url != pre_url:
            self.log_fn(
                "  -> Navigated, but the page didn't finish loading in time; proceeding."
            )
        else:
            self.log_fn("  -> No navigation after link click.")

    async def _follow_popup(self, page: Any, popup: Any):
        """Grab the new tab's URL, close it, and continue in the main tab."""
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:
            pass
        popup_url = popup.url
        try:
            await popup.close()
        except Exception:
            pass

        if not popup_url or popup_url == "about:blank":
            self.log_fn("  -> Link opened a new tab with no usable URL; closed it.")
            return

        self.log_fn(
            f"  -> Link opened a new tab to {popup_url}; following it in the main tab."
        )
        try:
            await page.goto(popup_url)
        except Exception as e:
            self.log_fn(f"  -> Failed to open {popup_url} in the main tab: {e}")
