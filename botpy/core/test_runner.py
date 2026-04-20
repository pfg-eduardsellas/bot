import asyncio
import os
from typing import Any, Dict, List

from playwright.async_api import async_playwright

from botpy.actions.button_action import ButtonAction
from botpy.actions.form_action import FormAction
from botpy.actions.link_action import LinkAction
from botpy.actions.url_action import URLAction


def reconstruct_action(record: Any, form_data: dict):
    """Rebuild an Action object from a stored ActionRecord."""
    if record.type == "URL":
        return URLAction(id=record.action_id, url=record.value)
    elif record.type == "BUTTON":
        return ButtonAction(
            id=record.action_id, selector=record.selector, custom_id=record.custom_id
        )
    elif record.type == "LINK":
        return LinkAction(
            id=record.action_id,
            selector=record.selector,
            href=record.value,
            custom_id=record.custom_id,
        )
    elif record.type == "FORM":
        return FormAction(
            id=record.action_id,
            selector=record.selector,
            form_data=form_data,
            custom_id=record.custom_id,
        )
    else:
        raise ValueError(f"Unknown action type: {record.type}")


async def execute_test_path(
    node_ids: List[int],
    action_map: Dict[int, Any],
    form_data: dict,
    scan_id: int,
) -> Dict[str, Any]:

    steps = []
    passed = True

    try:
        async with async_playwright() as p:
            headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
            browser = await p.chromium.launch(headless=headless)
            page = await browser.new_page()

            for node_id in node_ids:
                record = action_map.get(node_id)
                if not record:
                    steps.append(
                        {
                            "action_id": node_id,
                            "type": "UNKNOWN",
                            "status": "error",
                            "error": f"Action {node_id} not found in scan {scan_id}",
                        }
                    )
                    passed = False
                    continue

                try:
                    action = reconstruct_action(record, form_data)

                    if record.type != "URL" and record.selector:
                        visible = await page.locator(record.selector).first.is_visible()
                        if not visible:
                            steps.append(
                                {
                                    "action_id": node_id,
                                    "type": record.type,
                                    "status": "fail",
                                    "error": f"Element '{record.selector}' not visible",
                                }
                            )
                            passed = False
                            continue

                    await action.execute(page)
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(1)

                    if action.errors:
                        steps.append(
                            {
                                "action_id": node_id,
                                "type": record.type,
                                "status": "fail",
                                "error": "; ".join(action.errors),
                            }
                        )
                        passed = False
                    else:
                        steps.append(
                            {
                                "action_id": node_id,
                                "type": record.type,
                                "status": "pass",
                                "error": None,
                            }
                        )

                except Exception as e:
                    steps.append(
                        {
                            "action_id": node_id,
                            "type": record.type if record else "UNKNOWN",
                            "status": "error",
                            "error": str(e),
                        }
                    )
                    passed = False

            await browser.close()

    except Exception as e:
        print(f"[TestRunner] Playwright error: {e}")
        passed = False

    return {"passed": passed, "steps": steps}
