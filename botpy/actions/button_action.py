import asyncio
import os
from typing import Any
from botpy.models.action import Action, ActionType, ActionRetryError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"


class ButtonAction(Action):
    def __init__(self, id: int, selector: str, index: int = 0, custom_id: str = ""):
        unique_selector = f"{selector}::nth={index}" if index > 0 else selector
        super().__init__(id, ActionType.BUTTON, selector=unique_selector, custom_id=custom_id)
        self._base_selector = selector
        self.index = index

    def get_locator(self, page: Any):
        return page.locator(self._base_selector).nth(self.index)

    async def execute(self, page: Any):
        self.log_fn(f"Executing ButtonAction: Clicking {self._base_selector}[{self.index}]")
        locator = self.get_locator(page)

        if not _HEADLESS:
            await locator.scroll_into_view_if_needed()
            await locator.evaluate(
                """
                (el) => {
                    if (!el) return;
                    el.style.outline = '5px solid red';
                    el.style.outlineOffset = '2px';
                    el.style.transition = 'outline 0.1s ease-in-out';
                    let count = 0;
                    const interval = setInterval(() => {
                        el.style.outlineColor = count % 2 === 0 ? 'yellow' : 'red';
                        count++;
                        if (count > 6) {
                            clearInterval(interval);
                            el.style.outline = '';
                        }
                    }, 150);
                }
            """
            )
            await asyncio.sleep(1.0)
        try:
            click_timeout = 11000 if self.retry_count > 0 else 5000
            await locator.click(timeout=click_timeout)
        except Exception as e:
            self.errors.append(f"Click failed: {str(e)}")
            if self.retry_count == 0:
                self.retry_count += 1
                self.log_fn(f"Action {self.id} not available, queued for retry.")
                raise ActionRetryError(f"Click failed on first attempt: {str(e)}")
            else:
                self.errors.append("Action permanently failed after second attempt.")
                self.log_fn(f"Action {self.id} not available. Marked as failed after retry.")
                raise e
