import asyncio
import os
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright


ASSERTION_TYPES = {
    "url_equals",
    "url_contains",
    "visible",
    "not_visible",
    "text_equals",
    "text_contains",
    "value_equals",
    "count_equals",
}


async def _evaluate_assertion(page, assertion: dict) -> dict:
    """Evaluate a single assertion against the current page state."""
    t = assertion.get("type", "")
    selector = assertion.get("selector", "")
    expected = assertion.get("expected", "")

    if t not in ASSERTION_TYPES:
        return {"type": t, "passed": False, "error": f"Unknown assertion type: {t}"}

    try:
        if t == "url_equals":
            passed = page.url == expected
            actual = page.url
        elif t == "url_contains":
            passed = expected in page.url
            actual = page.url
        elif t == "visible":
            passed = await page.locator(selector).first.is_visible()
            actual = None
        elif t == "not_visible":
            passed = not await page.locator(selector).first.is_visible()
            actual = None
        elif t == "text_equals":
            actual = (await page.locator(selector).first.inner_text()).strip()
            passed = actual == expected
        elif t == "text_contains":
            actual = (await page.locator(selector).first.inner_text()).strip()
            passed = expected in actual
        elif t == "value_equals":
            actual = await page.locator(selector).first.input_value()
            passed = actual == expected
        elif t == "count_equals":
            actual = await page.locator(selector).count()
            passed = actual == int(expected)
        else:
            passed, actual = False, None

        return {
            "type": t,
            "selector": selector or None,
            "expected": expected or None,
            "actual": str(actual) if actual is not None else None,
            "passed": passed,
            "error": None if passed else f"Expected '{expected}', got '{actual}'" if actual is not None else f"Assertion '{t}' failed",
        }
    except Exception as e:
        return {
            "type": t,
            "selector": selector or None,
            "expected": expected or None,
            "actual": None,
            "passed": False,
            "error": str(e),
        }


async def execute_test_path(
    node_ids: List[int],
    action_map: Dict[int, Any],
    form_data: dict,
    scan_id: int,
    assertions: Optional[Dict[str, List[dict]]] = None,
) -> Dict[str, Any]:
    """Replay a saved path in a fresh browser and check its assertions."""
    assertions = assertions or {}
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
                    steps.append({
                        "action_id": node_id,
                        "type": "UNKNOWN",
                        "status": "error",
                        "error": f"Action {node_id} not found in scan {scan_id}",
                        "assertions": [],
                    })
                    passed = False
                    continue

                action_error = None
                try:
                    action = record.to_action(form_data)

                    await action.execute(page)
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(1)

                    if action.errors:
                        action_error = "; ".join(action.errors)
                        passed = False

                except Exception as e:
                    steps.append({
                        "action_id": node_id,
                        "type": record.type if record else "UNKNOWN",
                        "status": "error",
                        "error": str(e),
                        "assertions": [],
                    })
                    passed = False
                    continue

                # Evaluate assertions for this node
                node_assertions = assertions.get(str(node_id), [])
                assertion_results = []
                assertion_failed = False
                for assertion in node_assertions:
                    result = await _evaluate_assertion(page, assertion)
                    assertion_results.append(result)
                    if not result["passed"]:
                        assertion_failed = True
                        passed = False

                step_passed = not action_error and not assertion_failed
                steps.append({
                    "action_id": node_id,
                    "type": record.type,
                    "status": "pass" if step_passed else "fail",
                    "error": action_error,
                    "assertions": assertion_results,
                })

            await browser.close()

    except Exception as e:
        print(f"[TestRunner] Playwright error: {e}")
        passed = False

    return {"passed": passed, "steps": steps}
