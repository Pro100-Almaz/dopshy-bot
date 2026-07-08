from dataclasses import dataclass
from datetime import datetime
from typing import Literal

USER_ROLES = Literal['super_admin', 'admin', 'manager']

@dataclass
class UserModel:
    id: int
    email: str
    password_hash: str
    phone_number: str
    first_name: str
    last_name: str
    role: USER_ROLES
    created_at: datetime
    updated_at: datetime
    is_active: bool = True
