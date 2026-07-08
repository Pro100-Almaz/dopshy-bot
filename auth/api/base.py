from flask import Blueprint, request

auth_api = Blueprint("auth", __name__)

from auth.api.commands import register, login