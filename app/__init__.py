"""应用工厂。

只做装配：读配置、注册蓝图。业务逻辑一律下沉到 services 层。
"""

from flask import Flask

from app.config import Config


def create_app(config_class: type = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    from app.routes.health import bp as health_bp

    app.register_blueprint(health_bp)

    return app
