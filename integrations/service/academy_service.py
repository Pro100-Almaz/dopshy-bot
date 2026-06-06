import datetime

from integrations.repo.academy_repo import create_group, setting_training_time, deactivate_group_repo, \
    get_group_info_repo
from integrations.repo.postgres import draft_types, _DRAFTS_BY_BOTS


_GROUP_TYPES = {
    'football' : 'FOOTBALL',
    'boxing' : 'BOXING'
}

def create_academy_group(group_name: str, group_type: str, max_cap: int | None = None) -> None:
    if group_type not in _GROUP_TYPES or not group_name:
        raise ValueError(f"Invalid group type: {group_type}")

    group_type = _GROUP_TYPES[group_type]
    group_id = create_group(group_name, group_type, max_cap)


def set_update_training_time(group_id: int, date: str, start_time: str, end_time: str):
    setting_training_time(group_id, date, start_time, end_time)


def deactivate_group(group_id: int):
    deactivate_group_repo(group_id)

def create_academy_user():
    pass


def create_student_trial():
    pass


def get_group_info(bot_name:str) -> dict | None:
    table_name = "academy_groups" if bot_name != "dopsy_bot" else None
    if not table_name:
        return None

    curriculum = 'football' if bot_name == "chatbot_2" else 'boxing'

    return get_group_info_repo(table_name, curriculum)
