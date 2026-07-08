from dataclasses import dataclass
from datetime import datetime


@dataclass
class UserQm:
    id: int
    email:str
    phone_number: str
    first_name: str
    last_name: str
    is_active: bool
    role: str
    updated_at: datetime

def row_to_qm(row: dict) -> UserQm:
    return UserQm(
        row['id'],
        row['email'],
        row['phone_number'],
        row['first_name'],
        row['last_name'],
        row['is_active'],
        row['role'],
        row['updated_at'],
    )