import re
from typing import List, Any

from botpy.models.action import Action
from botpy.actions.button_action import ButtonAction
from botpy.actions.link_action import LinkAction
from botpy.actions.form_action import FormAction

# JS function injected into evaluate() calls to compute a CSS-path for an element.
_DOM_PATH_FN = """
    function getDomPath(node) {
        if (!node || node.tagName === 'BODY') return 'body';
        let part = node.tagName.toLowerCase();
        if (node.id) return part + '#' + CSS.escape(node.id);
        const classes = Array.from(node.classList).filter(c => c).slice(0, 2).join('.');
        if (classes) part += '.' + classes;
        const siblings = Array.from(node.parentElement?.children || [])
            .filter(c => c.tagName === node.tagName);
        if (siblings.length > 1) part += ':nth-child(' + (siblings.indexOf(node) + 1) + ')';
        return getDomPath(node.parentElement) + ' > ' + part;
    }
"""


# Do not consider elements with the same base path (ignoring nth-child) as separate actions,
def _sibling_key(dom_path: str) -> str:
    return re.compile(r":nth-child\(\d+\)").sub("", dom_path)


class Detector:
    def __init__(self, form_data: dict = None):
        self.img_extensions = [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp"]
        self.form_data = form_data or {}

    def _sanitize_text(self, text: str) -> str:
        if not text:
            return ""
        cleaned = " ".join(text.split())
        return cleaned.replace("'", "\\'")

    def _entries_to_button_actions(
        self,
        entries: List[str],
        id_offset: int,
    ) -> List[ButtonAction]:
        """Convert a list of unique selectors ('#id' or DOM path) to ButtonActions."""
        actions: List[ButtonAction] = []
        for selector in entries:
            action_id = id_offset + len(actions) + 1
            actions.append(
                ButtonAction(
                    id=action_id,
                    selector=selector,
                    index=0,
                    custom_id=f"btn_{action_id}",
                )
            )
        return actions

    def _remove_descendants(self, raw: List[dict]) -> List[dict]:
        """Drop elements whose DOM path starts with another element's path.

        If element A contains element B (both detected as cursor:pointer),
        B only inherits the style from A — keep A (the actual clickable container)
        and discard B.
        """
        paths = [item["path"] for item in raw]
        return [
            item
            for i, item in enumerate(raw)
            if not any(
                paths[i].startswith(paths[j] + " > ")
                for j in range(len(paths))
                if j != i
            )
        ]

    def _deduplicate_siblings(
        self,
        raw: List[dict],
        processed_ids: set,
    ) -> List[str]:
        """Filter JS element dicts by sibling key and return unique selectors.

        Returns '#id' for elements with an id, or the full DOM path otherwise.
        """
        raw = self._remove_descendants(raw)
        seen_siblings: set = set()
        selectors: List[str] = []

        for item in raw:
            elem_id = item.get("id", "")
            if elem_id and elem_id in processed_ids:
                continue

            key = _sibling_key(item["path"])
            if key in seen_siblings:
                continue
            seen_siblings.add(key)

            if elem_id:
                processed_ids.add(elem_id)
                selectors.append(f"#{elem_id}")
            else:
                selectors.append(item["path"])

        return selectors

    # region detectors

    async def _detect_buttons(
        self, page: Any, processed_ids: set, id_offset: int
    ) -> List[ButtonAction]:
        raw = await page.evaluate(
            f"""
            () => {{
                {_DOM_PATH_FN}
                function isVisible(el) {{
                    if (!el.offsetParent) return false;
                    const s = getComputedStyle(el);
                    return s.visibility !== 'hidden' && s.display !== 'none';
                }}
                const SEL = "button, input[type='button'], input[type='submit'], [role='button'], [onclick]";
                return Array.from(document.querySelectorAll(SEL))
                    .filter(isVisible)
                    .map(el => ({{
                        id: el.id || '',
                        tag: el.tagName.toLowerCase(),
                        text: (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60),
                        path: getDomPath(el),
                    }}));
            }}
            """
        )
        entries = self._deduplicate_siblings(raw, processed_ids)
        return self._entries_to_button_actions(entries, id_offset)

    async def _detect_pointer_elements(
        self, page: Any, processed_ids: set, id_offset: int
    ) -> List[ButtonAction]:
        raw = await page.evaluate(
            f"""
            () => {{
                {_DOM_PATH_FN}
                const COVERED = "button, input, [role='button'], [onclick], a";
                function isVisible(el) {{
                    if (!el.offsetParent) return false;
                    const s = getComputedStyle(el);
                    return s.visibility !== 'hidden' && s.display !== 'none';
                }}
                return Array.from(document.querySelectorAll('*'))
                    .filter(el =>
                        !el.matches(COVERED) &&
                        !el.closest(COVERED) &&
                        isVisible(el) &&
                        getComputedStyle(el).cursor === 'pointer'
                    )
                    .map(el => ({{
                        id: el.id || '',
                        tag: el.tagName.toLowerCase(),
                        text: (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60),
                        path: getDomPath(el),
                    }}));
            }}
            """
        )
        entries = self._deduplicate_siblings(raw, processed_ids)
        return self._entries_to_button_actions(entries, id_offset)

    async def _detect_links(
        self, page: Any, processed_ids: set, id_offset: int
    ) -> List[LinkAction]:
        links = await page.locator("a[href]").all()
        actions: List[LinkAction] = []
        for link in links:
            if not await link.is_visible():
                continue
            href = await link.get_attribute("href")
            if (
                not href
                or href.startswith("#")
                or any(ext in href for ext in self.img_extensions)
            ):
                continue
            raw_text = await link.text_content()
            safe_text = self._sanitize_text(raw_text[:20])
            selector = (
                f"a:has-text('{safe_text}')" if safe_text else f"a[href='{href}']"
            )
            action_id = id_offset + len(actions) + 1
            actions.append(
                LinkAction(
                    id=action_id,
                    selector=selector,
                    href=href,
                    custom_id=f"lnk_{action_id}",
                )
            )
        return actions

    async def _detect_forms(
        self, page: Any, processed_ids: set, id_offset: int
    ) -> List[FormAction]:
        actions: List[FormAction] = []

        forms = await page.locator("form, [role='form'], [role='search']").all()
        for i, form in enumerate(forms):
            if not await form.is_visible():
                continue
            elem_id = await form.get_attribute("id")
            if elem_id and elem_id not in processed_ids:
                selector = f"#{elem_id}"
                processed_ids.add(elem_id)
            else:
                selector = f"form:nth-of-type({i + 1})"
            action_id = id_offset + len(actions) + 1
            actions.append(
                FormAction(
                    id=action_id,
                    selector=selector,
                    form_data=self.form_data,
                    custom_id=f"frm_{action_id}",
                )
            )

        implicit_forms = await page.evaluate(
            """
            () => {
                const INPUT_SEL = 'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]), textarea, select';
                function isVisible(el) {
                    return el.offsetParent !== null && getComputedStyle(el).visibility !== 'hidden';
                }
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
                const orphanInputs = Array.from(document.querySelectorAll(INPUT_SEL))
                    .filter(el => !el.closest('form') && isVisible(el));
                const seen = new Map();
                for (const input of orphanInputs) {
                    let container = input.parentElement;
                    while (container && container !== document.body) {
                        if (container.querySelectorAll(INPUT_SEL).length > 1) break;
                        container = container.parentElement;
                    }
                    if (!container || container === document.body) continue;
                    const sel = buildSelector(container);
                    if (!seen.has(sel)) {
                        seen.set(sel, { selector: sel });
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
            action_id = id_offset + len(actions) + 1
            actions.append(
                FormAction(
                    id=action_id,
                    selector=selector,
                    form_data=self.form_data,
                    custom_id=f"frm_{action_id}",
                )
            )

        return actions

    async def detect(self, page: Any, current_id_counter: int) -> List[Action]:
        new_actions: List[Action] = []
        processed_ids: set = set()

        for detect_fn in (
            self._detect_forms,
            self._detect_pointer_elements,
            self._detect_buttons,
            self._detect_links,
        ):
            batch = await detect_fn(
                page, processed_ids, current_id_counter + len(new_actions)
            )
            new_actions.extend(batch)

        return new_actions
