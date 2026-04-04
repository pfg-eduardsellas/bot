from typing import Any
from botpy.models.action import Action, ActionType

class FormAction(Action):
    def __init__(self, id: int, selector: str, form_data: dict = None, custom_id: str = ""):
        super().__init__(id, ActionType.FORM, selector=selector, custom_id=custom_id)
        self.form_data = form_data or {}
        # We can store the form data in the 'value' attribute as a string representation 
        # or just keep it separate. For consistency with to_dict we might want to serialization strategy.
        self.value = str(self.form_data)

    async def execute(self, page: Any):
        print(f"Executing FormAction: Filling form {self.selector}")
        
        # 1. Fill inputs
        # This is a simplified logic. In a real scenario we'd iterate through inputs in the form.
        # For this example, we assume self.form_data keys match input selectors or names.
        
        if self.form_data:
            for key, value in self.form_data.items():
                try:
                    await page.fill(key, value)
                except Exception as e:
                    print(f"Error filling {key}: {e}")

        # 2. Submit
        # Check if the selector itself is a form or a submit button.
        # Use locator to find the submit button within the form if selector is the form tag.
        
        try:
             # Try to submit via press 'Enter' on one of the fields if no submit button logic is robust
             # Or just find a submit button inside.
             # For now, let's assume valid submit triggers by clicking the form's submit button / pressing enter.
             # Or we can just use page.locator(self.selector).evaluate("form => form.submit()")
             
             await page.locator(self.selector).evaluate("form => form.submit()")
             # Alternatively act on a specific submit button if we had its selector
        except Exception as e:
             print(f"Error submitting form: {e}")
