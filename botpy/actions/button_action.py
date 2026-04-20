from typing import Any
from botpy.models.action import Action, ActionType, ActionRetryError
import asyncio


class ButtonAction(Action):
    def __init__(self, id: int, selector: str, custom_id: str = ""):
        super().__init__(id, ActionType.BUTTON, selector=selector, custom_id=custom_id)

    async def execute(self, page: Any):
        self.log_fn(f"Executing ButtonAction: Clicking {self.selector}")
        try:
            # Use .first() to handle cases where the selector matches multiple elements
            locator = page.locator(self.selector).first

            # Ensure it's in view
            await locator.scroll_into_view_if_needed()

            # Custom high-visibility highlight using JS
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

            await asyncio.sleep(1.0)  # Wait for highlight to be noticed

            try:
                click_timeout = 11000 if self.retry_count > 0 else 5000
                async with page.expect_navigation(
                    wait_until="networkidle", timeout=2000
                ):
                    await locator.click(timeout=click_timeout)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                self.errors.append(f"Click failed: {str(e)}")

                if self.retry_count == 0:
                    self.retry_count += 1
                    raise ActionRetryError(
                        f"First click attempt failed. Retrying later."
                    )
                else:
                    self.errors.append(
                        "Action permanently failed after second attempt."
                    )
                    raise e

        except ActionRetryError as e:
            raise e
        except Exception as e:
            self.log_fn(f"Error on ButtonAction: {e}")
            raise e  # Raise to trigger backtracking in engine if needed
