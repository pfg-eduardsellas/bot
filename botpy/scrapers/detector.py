from typing import List, Any
from botpy.models.action import Action
from botpy.actions.button_action import ButtonAction
from botpy.actions.link_action import LinkAction
from botpy.actions.form_action import FormAction


class Detector:
    def __init__(self, form_data: dict = None):
        self.img_extensions = [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp"]
        self.form_data = form_data or {}

    def _sanitize_text(self, text: str) -> str:
        """Cleans up text for use in a Playwright has-text selector."""
        if not text:
            return ""
        # 1. Replace newlines and tabs with spaces
        # 2. Collapse multiple spaces into one
        # 3. Strip leading/trailing whitespace
        # 4. Escape single quotes for use in the selector string
        cleaned = " ".join(text.split())
        return cleaned.replace("'", "\\'")

    async def detect(self, page: Any, current_id_counter: int) -> List[Action]:
        new_actions = []
        processed_ids = set()  # prevent duplicate selectors

        # Detect Buttons
        buttons = await page.locator(
            "button, input[type='button'], input[type='submit']"
        ).all()
        for i, btn in enumerate(buttons):
            if await btn.is_visible():
                elem_id = await btn.get_attribute("id")
                if elem_id and elem_id not in processed_ids:
                    selector = f"#{elem_id}"
                    processed_ids.add(elem_id)
                else:
                    raw_text = await btn.text_content()
                    safe_text = self._sanitize_text(raw_text)
                    selector = (
                        f"button:has-text('{safe_text}')"
                        if safe_text
                        else f"button:nth-of-type({i+1})"
                    )

                action_id = current_id_counter + len(new_actions) + 1
                new_actions.append(
                    ButtonAction(
                        id=action_id, selector=selector, custom_id=f"btn_{action_id}"
                    )
                )

        # 2. Detect Links
        links = await page.locator("a[href]").all()
        for i, link in enumerate(links):
            if await link.is_visible():
                href = await link.get_attribute("href")
                if (
                    href
                    and not href.startswith("#")
                    and not any(ext in href for ext in self.img_extensions)
                ):
                    raw_text = await link.text_content()
                    safe_text = self._sanitize_text(
                        raw_text[:20]
                    )  # Truncate then sanitize
                    selector = (
                        f"a:has-text('{safe_text}')"
                        if safe_text
                        else f"a[href='{href}']"
                    )

                    action_id = current_id_counter + len(new_actions) + 1
                    new_actions.append(
                        LinkAction(
                            id=action_id,
                            selector=selector,
                            href=href,
                            custom_id=f"lnk_{action_id}",
                        )
                    )

        # 3. Detect Forms (explicit <form> tags + implicit form-like containers)
        forms = await page.locator("form, [role='form'], [role='search']").all()
        for i, form in enumerate(forms):
            if await form.is_visible():
                elem_id = await form.get_attribute("id")
                if elem_id and elem_id not in processed_ids:
                    selector = f"#{elem_id}"
                    processed_ids.add(elem_id)
                else:
                    selector = f"form:nth-of-type({i+1})"

                action_id = current_id_counter + len(new_actions) + 1
                new_actions.append(
                    FormAction(
                        id=action_id,
                        selector=selector,
                        form_data=self.form_data,
                        custom_id=f"frm_{action_id}",
                    )
                )

        # 4. Detect implicit forms (inputs grouped by closest common ancestor, outside <form>)
        implicit_forms = await page.evaluate(
            """
            () => {
                // Select all fillable inputs
                const INPUT_SEL = 'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]), textarea, select';

                // Check if it is visible
                function isVisible(el) {
                    return el.offsetParent !== null && getComputedStyle(el).visibility !== 'hidden';
                }

                // get unique selector (recursive)
                function buildSelector(el) {
                    if (el.id) return '#' + CSS.escape(el.id);
                    const tag = el.tagName.toLowerCase();
                    const siblings = Array.from(el.parentElement?.children || []).filter(c => c.tagName === el.tagName);
                    const nth = siblings.indexOf(el) + 1;
                    if (el.parentElement && el.parentElement !== document.body) {
                        return buildSelector(el.parentElement) + ' > ' + tag + ':nth-of-type(' + nth + ')';
                    }
                    return tag + ':nth-of-type(' + nth + ')';
                }

                // Get only the visible inputs that are not inside a form
                const orphanInputs = Array.from(document.querySelectorAll(INPUT_SEL))
                    .filter(el => !el.closest('form') && isVisible(el));

                const seen = new Map();

                for (const input of orphanInputs) {
                    // find containers with multiple inputs to group them as implicit forms
                    let container = input.parentElement;
                    while (container && container !== document.body) {
                        const count = container.querySelectorAll(INPUT_SEL).length;
                        if (count > 1) break;  
                        container = container.parentElement;
                    }

                    if (!container || container === document.body) continue;

                    const sel = buildSelector(container);
                    // Add container only once
                    if (!seen.has(sel)) {
                        seen.set(sel, {
                            selector: sel,
                            inputCount: container.querySelectorAll(INPUT_SEL).length
                        });
                    }
                }

                return Array.from(seen.values());
            }
        """
        )

        for form_info in implicit_forms:
            selector = form_info["selector"]
            if selector in processed_ids:
                continue
            processed_ids.add(selector)

            action_id = current_id_counter + len(new_actions) + 1
            new_actions.append(
                FormAction(
                    id=action_id,
                    selector=selector,
                    form_data=self.form_data,
                    custom_id=f"frm_{action_id}",
                )
            )

        return new_actions
