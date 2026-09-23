"""Flask 应用入口。"""

from flask import Flask, jsonify
from sqlalchemy import text

from .database import create_database_engine


def create_app(engine=None) -> Flask:
    app = Flask(__name__)
    storage = engine or create_database_engine()

    @app.get("/health")
    def health():
        with storage.connect() as connection:
            connection.execute(text("SELECT 1"))
        return jsonify(status="ok", storage="sqlite")

    return app
