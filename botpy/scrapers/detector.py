from typing import List, Any
from botpy.models.action import Action
from botpy.actions.button_action import ButtonAction
from botpy.actions.link_action import LinkAction
from botpy.actions.form_action import FormAction

class Detector:
    def __init__(self):
        self.img_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp']

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
        processed_ids = set() # prevent duplicate selectors
        
        # Detect Buttons
        buttons = await page.locator("button, input[type='button'], input[type='submit']").all()
        for i, btn in enumerate(buttons):
            if await btn.is_visible():
                elem_id = await btn.get_attribute("id")
                if elem_id and elem_id not in processed_ids:
                    selector = f"#{elem_id}"
                    processed_ids.add(elem_id)
                else:
                    raw_text = await btn.text_content()
                    safe_text = self._sanitize_text(raw_text)
                    selector = f"button:has-text('{safe_text}')" if safe_text else f"button:nth-of-type({i+1})"
                
                action_id = current_id_counter + len(new_actions) + 1
                new_actions.append(ButtonAction(id=action_id, selector=selector, custom_id=f"btn_{action_id}"))

        # 2. Detect Links
        links = await page.locator("a[href]").all()
        for i, link in enumerate(links):
            if await link.is_visible():
                href = await link.get_attribute("href")
                if href and not href.startswith("#") and not any(ext in href for ext in self.img_extensions):
                    raw_text = await link.text_content()
                    safe_text = self._sanitize_text(raw_text[:20]) # Truncate then sanitize
                    selector = f"a:has-text('{safe_text}')" if safe_text else f"a[href='{href}']"
                    
                    action_id = current_id_counter + len(new_actions) + 1
                    new_actions.append(LinkAction(id=action_id, selector=selector, href=href, custom_id=f"lnk_{action_id}"))

        # 3. Detect Forms
        forms = await page.locator("form").all()
        for i, form in enumerate(forms):
            if await form.is_visible():
                elem_id = await form.get_attribute("id")
                selector = f"#{elem_id}" if elem_id else f"form:nth-of-type({i+1})"
                
                action_id = current_id_counter + len(new_actions) + 1
                # Dummy form data for now
                new_actions.append(FormAction(id=action_id, selector=selector, form_data={"input": "test"}, custom_id=f"frm_{action_id}"))

        return new_actions
