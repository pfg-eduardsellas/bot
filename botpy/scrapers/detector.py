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
# TEMP: id-shortcut disabled so ids are never used as selectors. Revert by
# restoring the `if (node.id) return ...` line below.
_CSS_PATH_FN = """
    function getCssPath(node) {
        if (!node || node.tagName === 'BODY') return 'body';
        let part = node.tagName.toLowerCase();
        const siblings = Array.from(node.parentElement?.children || [])
            .filter(c => c.tagName === node.tagName);
        if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
        return getCssPath(node.parentElement) + ' > ' + part;
    }
"""


def _sibling_key(dom_path: str) -> str:
    """Strip positional pseudo-classes so sibling elements collapse into one key."""
    return re.compile(r":nth-(?:child|of-type)\(\d+\)").sub("", dom_path)


class Detector:
    """Finds the interactive elements of a page and turns them into actions."""

    def __init__(self, form_data: dict = None, simplified: bool = True):
        self.img_extensions = [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp"]
        self.form_data = form_data or {}
        self.simplified = simplified

    def _build_stable_selector(self, item: dict) -> str:
        """Build a stable selector, preferring data-testid, then aria-label, then the CSS path."""
        # TEMP: id branch disabled — never use an element's id as selector.
        # Revert by restoring: `if item.get("id"): return f"#{item['id']}"`
        testid = item.get("data_testid", "")
        if testid:
            return f'[data-testid="{testid}"]'
        aria = item.get("aria_label", "").replace('"', "").strip()
        if aria:
            return f'[aria-label="{aria}"]'
        return item["css_path"]

    def _entries_to_button_actions(
        self,
        entries: List[dict],
        id_offset: int,
    ) -> List[ButtonAction]:
        """Turn detected entries into button actions, indexing repeated selectors."""
        actions: List[ButtonAction] = []
        selector_counts: dict = {}
        for entry in entries:
            selector = entry["selector"]
            name = entry["name"]
            index = selector_counts.get(selector, 0)
            selector_counts[selector] = index + 1
            action_id = id_offset + len(actions) + 1
            actions.append(
                ButtonAction(
                    id=action_id,
                    selector=selector,
                    index=index,
                    custom_id=f"btn_{action_id}",
                    name=name,
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
    ) -> List[dict]:
        """Deduplicate by sibling key (using class-aware path) and return stable selectors with names."""
        raw = self._remove_descendants(raw)
        seen_siblings: set = set()
        entries: List[dict] = []

        for item in raw:
            elem_id = item.get("id", "")
            if elem_id and elem_id in processed_ids:
                continue

            if self.simplified:
                key = _sibling_key(item["path"])
                if key in seen_siblings:
                    continue
                seen_siblings.add(key)

            if elem_id:
                processed_ids.add(elem_id)

            selector = self._build_stable_selector(item)
            text = (item.get("text") or "").strip()
            entries.append({"selector": selector, "name": text or selector})

        return entries

    # region detectors

    async def _detect_clickable(
        self, page: Any, processed_ids: set, id_offset: int
    ) -> List[ButtonAction]:
        """Detect buttons and any other clickable element on the page."""
        raw = await page.evaluate(
            f"""
            () => {{
                {_DOM_PATH_FN}
                {_CSS_PATH_FN}
                const SEMANTIC = "button, input[type='button'], input[type='submit'], [role='button'], [onclick]";
                const INSIDE   = "button, input, [role='button'], [onclick], a";
                return Array.from(document.querySelectorAll('*'))
                    .filter(el =>
                        el.checkVisibility({{ visibilityProperty: true }}) &&
                        (
                            el.matches(SEMANTIC) ||
                            (
                                !el.matches(INSIDE) &&
                                !el.closest(INSIDE) &&
                                getComputedStyle(el).cursor === 'pointer'
                            )
                        )
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
        """Detect navigable links, ignoring anchors and image URLs."""
        raw = await page.evaluate(
            f"""
            () => {{
                {_CSS_PATH_FN}
                return Array.from(document.querySelectorAll('a[href]'))
                    .filter(el => el.checkVisibility({{ visibilityProperty: true }}))
                    .map(el => ({{
                        id: el.id || '',
                        href: el.getAttribute('href') || '',
                        aria_label: el.getAttribute('aria-label') || '',
                        data_testid: el.getAttribute('data-testid') || '',
                        css_path: getCssPath(el),
                    }}));
            }}
            """
        )
        actions: List[LinkAction] = []
        seen_hrefs: set = set()
        for item in raw:
            href = item.get("href", "")
            if (
                not href
                or href.startswith("#")
                or any(ext in href for ext in self.img_extensions)
            ):
                continue
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            elem_id = item.get("id", "")
            if elem_id and elem_id in processed_ids:
                continue
            if elem_id:
                processed_ids.add(elem_id)

            selector = self._build_stable_selector(item)
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
        """Detect forms and the input groups that behave like one."""
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
                function addInput(groups, key, groupData, input) {{
                    if (!groups.has(key)) groups.set(key, {{ ...groupData, inputs: [] }});
                    const label = input.id || input.getAttribute('name') || input.type || input.tagName.toLowerCase();
                    groups.get(key).inputs.push(label);
                }}
                for (const input of visibleInputs) {{
                    const formEl = input.closest('form, [role="form"], [role="search"]');
                    if (formEl) {{
                        const key = formEl.id || getDomPath(formEl);
                        const sel = getCssPath(formEl);
                        addInput(groups, key, {{ selector: sel, elemId: formEl.id || '' }}, input);
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
                        const sel = getCssPath(container);
                        addInput(groups, key, {{ selector: sel, elemId: container.id || '' }}, input);
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
            inputs = group.get("inputs", [])
            form_name = "[" + ", ".join(inputs) + "]" if inputs else selector
            actions.append(
                FormAction(
                    id=action_id,
                    selector=selector,
                    form_data=self.form_data,
                    custom_id=f"frm_{action_id}",
                    name=form_name,
                )
            )

        return actions

    async def detect(self, page: Any, current_id_counter: int) -> List[Action]:
        """Detect every form, clickable element and link on the page as actions."""
        new_actions: List[Action] = []
        processed_ids: set = set()

        for detect_fn in (
            self._detect_forms,
            self._detect_clickable,
            self._detect_links,
        ):
            batch = await detect_fn(
                page, processed_ids, current_id_counter + len(new_actions)
            )
            new_actions.extend(batch)

        return new_actions
