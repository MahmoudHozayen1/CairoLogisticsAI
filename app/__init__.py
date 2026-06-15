"""Application factory for the SwiftRoute logistics platform."""
import os
import click
from flask import Flask, render_template

from .extensions import db, migrate, login_manager, csrf
from . import models  # noqa: F401  (ensure models are registered)


def create_app(config_name=None):
    config_name = config_name or os.environ.get("FLASK_CONFIG", "default")
    from config import config_by_name

    app = Flask(__name__)
    app.config.from_object(config_by_name[config_name])

    # Make sure instance/upload folders exist.
    os.makedirs(os.path.join(app.root_path, "..", "instance"), exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    # Extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    _register_blueprints(app)
    _register_template_helpers(app)
    _register_error_handlers(app)
    _register_cli(app)

    _bootstrap_database(app)

    return app


def _bootstrap_database(app):
    """Create tables (and optionally seed) on startup for shell-less deploys.

    Controlled by AUTO_INIT_DB / SEED_DEMO so it is a no-op in normal local use.
    """
    if not app.config.get("AUTO_INIT_DB"):
        return
    with app.app_context():
        try:
            db.create_all()
        except Exception as exc:  # pragma: no cover - concurrent worker race
            db.session.rollback()
            app.logger.warning("create_all() skipped: %s", exc)
        if app.config.get("SEED_DEMO"):
            from .models import User
            try:
                if db.session.query(User.id).first() is None:
                    from seed import seed_data
                    seed_data()
                    app.logger.info("Seeded demo data on first boot.")
            except Exception as exc:  # pragma: no cover - concurrent seed race
                db.session.rollback()
                app.logger.warning("Demo seed skipped (already running?): %s", exc)


def _register_blueprints(app):
    from .blueprints.main import bp as main_bp
    from .blueprints.auth import bp as auth_bp
    from .blueprints.admin import bp as admin_bp
    from .blueprints.courier import bp as courier_bp
    from .blueprints.merchant import bp as merchant_bp
    from .blueprints.tracking import bp as tracking_bp
    from .blueprints.api import bp as api_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(courier_bp, url_prefix="/courier")
    app.register_blueprint(merchant_bp, url_prefix="/merchant")
    app.register_blueprint(tracking_bp, url_prefix="/track")
    app.register_blueprint(api_bp, url_prefix="/api")
    csrf.exempt(api_bp)  # API uses token/JSON, not browser forms


def _register_template_helpers(app):
    from .models import ShipmentStatus

    @app.context_processor
    def inject_globals():
        return {
            "ShipmentStatus": ShipmentStatus,
            "app_name": "SwiftRoute",
        }


def _register_error_handlers(app):
    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("errors/error.html", code=403,
                               message="You don't have permission to view this page."), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("errors/error.html", code=404,
                               message="The page you're looking for was not found."), 404

    @app.errorhandler(500)
    def server_error(_e):
        db.session.rollback()
        return render_template("errors/error.html", code=500,
                               message="Something went wrong on our side."), 500


def _register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create all database tables."""
        db.create_all()
        click.echo("Database tables created.")

    @app.cli.command("seed")
    def seed_command():
        """Populate the database with demo users, hubs and shipments."""
        from seed import seed_data
        seed_data()
        click.echo("Demo data seeded.")

    @app.cli.command("create-admin")
    @click.option("--name", prompt=True)
    @click.option("--email", prompt=True)
    @click.password_option()
    def create_admin(name, email, password):
        """Create an administrator account."""
        from .models import User, Role
        if User.query.filter_by(email=email).first():
            click.echo("A user with that email already exists.")
            return
        user = User(name=name, email=email, role=Role.ADMIN)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Admin {email} created.")
