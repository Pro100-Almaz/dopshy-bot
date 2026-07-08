import re

class ValidationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def require(payload: dict, fields: tuple[str, ...]) -> None:
    if not isinstance(payload, dict):
        raise ValidationError("Request body is not JSON object")

    missing = [f for f in fields if not payload.get(f)]
    if missing:
        raise ValidationError(f"Missing required fields: {','.join(missing)}")

def clean_email(raw) -> str:
    email = str(raw).strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValidationError("Invalid email")
    return email

def clean_password(raw, *, min_len:int = 0) -> str:
    pw = str(raw)
    if len(pw) < min_len:
        raise ValidationError("Password must be at least 8 characters")
    return pw


# presence --> normalization --> field-level rules
def parse_register(payload: dict) -> dict:
    require(payload, ('email', 'password', 'phone_number', 'first_name', 'last_name', 'role'))
    return {
        "email": clean_email(payload['email']),
        "password": clean_password(payload['password']),
        "phone_number" : str(payload['phone_number']).strip(),
        "first_name" : str(payload['first_name']).strip(),
        "last_name" : str(payload['last_name']).strip(),
        "role": str(payload['role']).strip()
    }


def parse_login(payload: dict) -> dict:
    require(payload, ("email", 'password'))
    return {
        "email": clean_email(payload['email']),
        "password": clean_password(payload['password'])
    }
