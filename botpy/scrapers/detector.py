import re
from typing import List, Any

from botpy.models.action import Action
from botpy.actions.button_action import ButtonAction
from botpy.actions.link_action import LinkAction
from botpy.actions.form_action import FormAction

# DOM path with classes — used only for deduplication (more specific matching).
_DOM_PATH_FN = """
    function getDomPath(node) {
        if (!node || node.tagName === 'BODY') return 'body';
        let part = node.tagName.toLowerCase();
        if (node.id) return part + '#' + CSS.escape(node.id);
        const classes = Array.from(node.classList).filter(c => c).slice(0, 2).map(c => CSS.escape(c)).join('.');
        if (classes) part += '.' + classes;
        const siblings = Array.from(node.parentElement?.children || [])
            .filter(c => c.tagName === node.tagName);
        if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
        return getDomPath(node.parentElement) + ' > ' + part;
    }
"""

# CSS path without classes — used as the stable fallback selector.
_CSS_PATH_FN = """
    function getCssPath(node) {
        if (!node || node.tagName === 'BODY') return 'body';
        let part = node.tagName.toLowerCase();
        if (node.id) return part + '#' + CSS.escape(node.id);
        const siblings = Array.from(node.parentElement?.children || [])
            .filter(c => c.tagName === node.tagName);
        if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
        return getCssPath(node.parentElement) + ' > ' + part;
    }
"""


def _sibling_key(dom_path: str) -> str:
    return re.compile(r":nth-(?:child|of-type)\(\d+\)").sub("", dom_path)


class Detector:
    def __init__(self, form_data: dict = None):
        self.img_extensions = [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp"]
        self.form_data = form_data or {}

    def _sanitize_text(self, text: str) -> str:
        if not text:
            return ""
        cleaned = " ".join(text.split())
        return cleaned.replace("'", "\\'")

    def _build_stable_selector(self, item: dict) -> str:
        """Build a stable Playwright-compatible selector from element attributes.

        Priority: id > data-testid > aria-label > tag:has-text > CSS path (no classes).
        """
        if item.get("id"):
            return f"#{item['id']}"
        testid = item.get("data_testid", "")
        if testid:
            return f'[data-testid="{testid}"]'
        aria = item.get("aria_label", "")
        if aria:
            return f'[aria-label="{aria}"]'
        text = item.get("text", "").replace('"', "").strip()
        if text:
            return f"{item['tag']}:has-text(\"{text}\")"
        return item["css_path"]

    def _entries_to_button_actions(
        self,
        entries: List[str],
        id_offset: int,
    ) -> List[ButtonAction]:
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
        """Drop elements whose DOM path starts with another element's path."""
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
        """Deduplicate by sibling key (using class-aware path) and return stable selectors."""
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

            selectors.append(self._build_stable_selector(item))

        return selectors

    # region detectors

    async def _detect_buttons(
        self, page: Any, processed_ids: set, id_offset: int
    ) -> List[ButtonAction]:
        raw = await page.evaluate(
            f"""
            () => {{
                {_DOM_PATH_FN}
                {_CSS_PATH_FN}
                const SEL = "button, input[type='button'], input[type='submit'], [role='button'], [onclick]";
                return Array.from(document.querySelectorAll(SEL))
                    .filter(el => el.checkVisibility({{ visibilityProperty: true }}))
                    .map(el => ({{
                        id: el.id || '',
                        tag: el.tagName.toLowerCase(),
                        text: (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 40),
                        aria_label: el.getAttribute('aria-label') || '',
                        data_testid: el.getAttribute('data-testid') || '',
                        path: getDomPath(el),
                        css_path: getCssPath(el),
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
                {_CSS_PATH_FN}
                const COVERED = "button, input, [role='button'], [onclick], a";
                return Array.from(document.querySelectorAll('*'))
                    .filter(el =>
                        !el.matches(COVERED) &&
                        !el.closest(COVERED) &&
                        el.checkVisibility({{ visibilityProperty: true }}) &&
                        getComputedStyle(el).cursor === 'pointer'
                    )
                    .map(el => ({{
                        id: el.id || '',
                        tag: el.tagName.toLowerCase(),
                        text: (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 40),
                        aria_label: el.getAttribute('aria-label') || '',
                        data_testid: el.getAttribute('data-testid') || '',
                        path: getDomPath(el),
                        css_path: getCssPath(el),
                    }}));
            }}
            """
        )
        entries = self._deduplicate_siblings(raw, processed_ids)
        return self._entries_to_button_actions(entries, id_offset)

    async def _detect_links(
        self, page: Any, processed_ids: set, id_offset: int
    ) -> List[LinkAction]:
        links = await page.locator("a[href]:visible").all()
        actions: List[LinkAction] = []
        for link in links:
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

        groups = await page.evaluate(
            f"""
            () => {{
                {_DOM_PATH_FN}
                {_CSS_PATH_FN}
                const INPUT_SEL = 'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]), textarea, select';
                const visibleInputs = Array.from(document.querySelectorAll(INPUT_SEL))
                    .filter(el => el.checkVisibility({{ visibilityProperty: true }}));
                const groups = new Map();
                for (const input of visibleInputs) {{
                    const formEl = input.closest('form, [role="form"], [role="search"]');
                    if (formEl) {{
                        const key = formEl.id || getDomPath(formEl);
                        const sel = formEl.id ? '#' + CSS.escape(formEl.id) : getCssPath(formEl);
                        if (!groups.has(key)) groups.set(key, {{ selector: sel, elemId: formEl.id || '' }});
                    }} else {{
                        let container = input.parentElement;
                        while (container && container !== document.body) {{
                            const cnt = Array.from(container.querySelectorAll(INPUT_SEL))
                                .filter(el => el.checkVisibility({{ visibilityProperty: true }})).length;
                            if (cnt > 1) break;
                            container = container.parentElement;
                        }}
                        if (!container || container === document.body) continue;
                        const key = container.id || getDomPath(container);
                        const sel = container.id ? '#' + CSS.escape(container.id) : getCssPath(container);
                        if (!groups.has(key)) groups.set(key, {{ selector: sel, elemId: container.id || '' }});
                    }}
                }}
                return Array.from(groups.values());
            }}
            """
        )

        for group in groups:
            selector = group["selector"]
            elem_id = group.get("elemId", "")
            if selector in processed_ids or (elem_id and elem_id in processed_ids):
                continue
            processed_ids.add(selector)
            if elem_id:
                processed_ids.add(elem_id)
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
