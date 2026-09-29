import pytest

from handlers.llm_trial_flow import _normalize_manual_value, _signup_actor


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A parent also says «маған/мне» — a mentioned child means not self.
        ("маған сол баламды пробный сабаққа жаздыру керек еді\n2015 жылғы", None),
        ("мне нужно записать сына на пробное", None),
        ("хочу записаться, ребенку 10 лет", None),
        ("маған ұлымды жаздыру керек", None),
        ("қызымды жаздырғым келеді", None),
        # «сынақ» (trial) is not «сын» (son).
        ("маған сынақ сабағына жазылу керек", "self"),
        ("мне 15 лет, хочу записаться", "self"),
        ("өзім жазылғым келеді", "self"),
        ("когда пробное?", None),
    ],
)
def test_signup_actor(text, expected):
    assert _signup_actor(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Айдос", "Айдос"),
        ("менің атым Айдос", "Айдос"),
        ("есімім Махамбет", "Махамбет"),
        ("атым Айдос.", "Айдос"),
        ("Мен Айдоспын", "Айдос"),
        ("мен Айдос", "Айдос"),
        ("баламның аты Айдос", "Айдос"),
        ("Айдос менің атым", "Айдос"),
        ("меня зовут Алихан", "Алихан"),
        ("моё имя Алихан", "Алихан"),
        ("сына зовут Алихан", "Алихан"),
        ("я Алихан", "Алихан"),
        ("Ян", "Ян"),
        ("Анна Мария", "Анна Мария"),
    ],
)
def test_child_name_strips_lead_in(text, expected):
    assert _normalize_manual_value("child_name", text) == expected
