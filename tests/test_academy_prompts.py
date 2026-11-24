"""The academy prompts answer and invite to the trial; the signup flow, not
the free-text LLM, asks the child's details — each exactly once."""

import pytest

from chat.system_prompts import sp_2, sp_3

pytestmark = pytest.mark.no_db

_COLLECTING = (
    "уточнить год рождения, опыт и школьную смену",
    "Подскажите возраст участника",
    "Қатысушының жасын айтсаңыз",
    "аренде полей",
    "имя и год рождения",
)


@pytest.mark.parametrize("prompt", [sp_2.SYSTEM_PROMPT, sp_3.SYSTEM_PROMPT])
def test_prompt_invites_instead_of_collecting(prompt):
    assert not [line for line in _COLLECTING if line in prompt]
    assert any("пробное" in line and "НЕ спрашивай" in line for line in prompt.splitlines())


@pytest.mark.parametrize("prompt", [sp_2.SYSTEM_PROMPT, sp_3.SYSTEM_PROMPT])
def test_prompt_never_invents_a_price(prompt):
    # The football KB has no trial price; the bot once made up «2000 тг».
    assert "Называй только цены из предоставленной информации" in prompt
    assert "Тек берілген ақпараттағы бағаларды ғана айт" in prompt


@pytest.mark.parametrize("prompt, bot", [(sp_2.SYSTEM_PROMPT, "dopsy_fs_school"), (sp_3.SYSTEM_PROMPT, "dopsy_boxing")])
def test_prompt_admin_phone_comes_from_config(prompt, bot):
    from chat.system_prompts.academy_rules import ACADEMY_ADMIN_PHONES

    assert "{admin_phone}" not in prompt
    assert ACADEMY_ADMIN_PHONES[bot] in prompt
