from dataclasses import asdict

import bcrypt
from flask import request, jsonify

from auth.api.base import auth_api
from auth.repo import get_user_by_email, get_user_credentials
from auth.parsers import parse_login, ValidationError


def _verify_password(password: str, password_hash: str) -> bool:
    password_bytes = password.encode('utf-8')
    hash_bytes = password_hash.encode('utf-8')

    return bcrypt.checkpw(password_bytes, hash_bytes)


class LoginRequest:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password


@auth_api.post("/api/auth/login")
def login():
    body = request.get_json(silent=True) or {}

    try:
        cmd = LoginRequest(**parse_login(body))
    except ValidationError as e:
        return jsonify({
            "ok": False,
            "data": None,
            "message": "invalid email or password"
        }), 401

    user = get_user_credentials(cmd.email)
    if user and _verify_password(cmd.password, user.password_hash):
        user_data = get_user_by_email(cmd.email)
    else:
        return jsonify({
            "ok": False,
            "data": None,
            "message": "invalid email or password"
        }), 401

    return jsonify({
        "ok": user_data is not None,
        "data": asdict(user_data) if user_data else None,
        "message": "success"
    }), 200
