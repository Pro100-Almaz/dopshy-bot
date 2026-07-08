from dataclasses import asdict

import logging

import bcrypt
from flask import request, jsonify

from auth.api.base import auth_api
from auth.query_models import row_to_qm
from auth.repo import insert_user, get_user_by_email, has_no_duplicate_phone, has_no_duplicate_email
from auth.parsers import parse_register, ValidationError

logger = logging.getLogger(__name__)


def _hash_password(password: str) -> str:
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt(rounds=12)

    password_hash = bcrypt.hashpw(password_bytes, salt)
    return password_hash.decode('utf-8')

def _available_email(email: str) -> bool:
    return has_no_duplicate_email(email)

def _available_phone_number(phone_number: str) -> bool:
    return has_no_duplicate_phone(phone_number)


class RegisterRequest:
    def __init__(
            self,
            email,
            password,
            phone_number,
            first_name,
            last_name,
            role,
    ):
        self.email = email
        self.password = password
        self.phone_number = phone_number
        self.first_name = first_name
        self.last_name = last_name
        self.role = role


@auth_api.post("/api/auth/register")
def register():
    body = request.get_json(silent=True) or {}
    try:
        cmd = RegisterRequest(**parse_register(body))
    except ValidationError as e:
        return jsonify({
            "ok": False,
            "data": None,
            "message": e.message
        }), 400

    pw_hash = _hash_password(cmd.password)
    email = cmd.email
    phone_number = cmd.phone_number
    first_name = cmd.first_name
    last_name = cmd.last_name
    role = cmd.role

    if _available_email(email) and _available_phone_number(phone_number):
        insert_user(email, phone_number, first_name, last_name, pw_hash, role)
        user_data = get_user_by_email(email)
    else:
        user_data = None

    return jsonify({
        "ok" : user_data is not None,
        "data": asdict(user_data) if user_data else None,
        "message": "success" if user_data else "email or phone_number is already registered"
    }), (201 if user_data else 409)
