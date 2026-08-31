"""Report extraction data endpoints for downstream document generation."""

from flask import Blueprint, jsonify, request

from blueprints.manager_api import _authenticate
from integrations.document_service import DocumentValidationError, build_arena_extract_data


document_api = Blueprint("document_api", __name__)
document_api.before_request(_authenticate)


@document_api.post("/api/manager/documents/extract-data")
def extract_document_data():
    try:
        data = build_arena_extract_data(request.get_json(silent=True))
    except DocumentValidationError as exc:
        return jsonify({"detail": exc.detail}), 400
    return jsonify({"ok": True, "data": data}), 200
