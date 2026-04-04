from typing import Any
from botpy.models.action import Action, ActionType

class LinkAction(Action):
    def __init__(self, id: int, selector: str, href: str, custom_id: str = ""):
        super().__init__(id, ActionType.LINK, selector=selector, value=href, custom_id=custom_id)

    async def execute(self, page: Any):
        print(f"Executing LinkAction: Navigating to {self.value} via click")
        # Same pattern: expect_navigation wraps the click so we don't miss the load event.
        try:
            async with page.expect_navigation(wait_until="networkidle", timeout=8000):
                await page.click(self.selector)
            print(f"  -> Page loaded: {page.url}")
        except Exception:
            # Some links open in a new tab or do nothing — handle gracefully.
            print(f"  -> No navigation after link click (may have opened a new tab or failed).")
