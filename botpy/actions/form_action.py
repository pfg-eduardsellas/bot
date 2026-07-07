from typing import Any
from botpy.models.action import Action, ActionType

DEFAULT_VALUES = {
    "text": "This is a text input",
    "textarea": "This is a textarea input",
    "number": "123",
    "email": "test@example.com",
    "password": "Password123!",
    "date": "2024-01-01",
    "tel": "123456789",
    "url": "https://example.com",
    "search": "search test",
    "color": "#ff0000",
}


class FormAction(Action):
    def __init__(
        self, id: int, selector: str, form_data: dict = None, custom_id: str = "", name: str = ""
    ):
        super().__init__(id, ActionType.FORM, selector=selector, custom_id=custom_id)
        self.form_data = form_data or {}
        self.value = str(self.form_data)
        self.name = name or selector

    async def execute(self, page: Any):
        self.log_fn(f"Executing FormAction: Filling form {self.selector}")

        form_locator = page.locator(self.selector)

        # fit inputs
        inputs = await form_locator.locator("input, textarea").all()

        for input_element in inputs:
            if not await input_element.is_visible():
                continue
            if not await input_element.is_editable():
                continue

            tag_name = await input_element.evaluate(
                "el => el.tagName.toLowerCase()"
            )

            if tag_name == "textarea":
                input_type = "text"
            else:
                input_type = await input_element.get_attribute("type") or "text"

            # Check if user sets a value
            name_attr = await input_element.get_attribute("name")
            id_attr = await input_element.get_attribute("id")

            value_to_fill = None
            user_value = None

            if self.form_data and name_attr in self.form_data:
                user_value = str(self.form_data[name_attr])
            elif self.form_data and id_attr in self.form_data:
                user_value = str(self.form_data[id_attr])

            # checkbox / radio logic
            if input_type in ["checkbox", "radio"]:
                if user_value is not None:
                    should_check = user_value in ("True", "true", "1", "yes")
                else:
                    should_check = True  # default to true
                try:
                    if should_check:
                        await input_element.check(force=True)
                    else:
                        await input_element.uncheck(force=True)
                except Exception as e:
                    err = f"Error checking input type {input_type}: {e}"
                    self.log_fn(err)
                    self.errors.append(err)
                continue

            # if no user value, use default
            elif user_value is not None:
                value_to_fill = user_value
            elif (
                input_type == "submit"
                or input_type == "button"
                or input_type == "hidden"
                or input_type == "image"
                or input_type == "reset"
            ):
                continue  # Ignore these types as they are not meant to be filled
            else:
                value_to_fill = DEFAULT_VALUES.get(input_type, "Default")

            if value_to_fill is not None:
                try:
                    await input_element.fill(value_to_fill)
                except Exception as e:
                    err = f"Error filling {input_type}: {e}"
                    self.log_fn(err)
                    self.errors.append(err)

        # Send form: explicit submit > any single button > form.submit() > Enter key
        try:
            explicit_submit = form_locator.locator(
                "button[type='submit'], input[type='submit']"
            )
            all_buttons = form_locator.locator("button:visible, input[type='button']:visible")

            if await explicit_submit.count() > 0:
                await explicit_submit.first.click()
            elif await all_buttons.count() == 1:
                await all_buttons.first.click()
            else:
                submitted = await form_locator.evaluate(
                    "el => { if (el.tagName === 'FORM') { el.submit(); return true; } return false; }"
                )
                if not submitted:
                    # Implicit form (div container) with no button — press Enter on last visible input
                    visible_inputs = [i for i in inputs if await i.is_visible()]
                    if visible_inputs:
                        await visible_inputs[-1].press("Enter")
        except Exception as e:
            err = f"Error submitting form {self.selector}: {e}"
            self.log_fn(err)
            self.errors.append(err)
