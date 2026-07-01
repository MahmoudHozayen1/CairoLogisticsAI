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
                    # Train the predictive models once so the AI dashboard is
                    # populated immediately. Best-effort: never block boot.
                    try:
                        from .ml import get_service
                        get_service().ensure_trained()
                        app.logger.info("Trained predictive models on first boot.")
                    except Exception as exc:  # pragma: no cover
                        app.logger.warning("Model training skipped: %s", exc)
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

    @app.cli.command("train-ml")
    @click.option("--regenerate", is_flag=True,
                  help="Rebuild the synthetic history before training.")
    def train_ml(regenerate):
        """Train the predictive models (drop-off, pickup, late-risk, forecast)."""
        from .ml.train import train_all
        m = train_all(regenerate=regenerate)
        mm = m["models"]
        click.echo(f"Trained on {m['n_rows']} records.")
        click.echo(f"  Drop-off ETA : MAE {mm['dropoff']['mae']} min, R2 {mm['dropoff']['r2']}")
        click.echo(f"  Pickup time  : MAE {mm['pickup']['mae']} min, R2 {mm['pickup']['r2']}")
        click.echo(f"  Late-risk    : AUC {mm['late']['roc_auc']}, base {mm['late']['base_rate']}")
        click.echo(f"  Forecast     : orders MAPE {mm['forecast']['orders_mape']}%, "
                   f"cost MAPE {mm['forecast']['cost_mape']}%")

    @app.cli.command("train-router")
    def train_router_cmd():
        """Train the learning-to-route pointer policy (neural router)."""
        from .ml.neural_router import train_router, save_router
        bundle, m = train_router()
        save_router(bundle, m)
        click.echo("Route policy trained.")
        click.echo(f"  vs nearest-neighbour : {m['val_improve_vs_nn_pct']:+.2f}% shorter")
        click.echo(f"  gap to 2-opt (sample): {m['val_sampled_gap_vs_two_opt_pct']:.2f}%")

    @app.cli.command("train-behavior")
    def train_behavior_cmd():
        """Train the courier behaviour persona model."""
        from .ml.behavior import train_behavior, save_behavior
        bundle, m = train_behavior()
        save_behavior(bundle, m)
        click.echo("Courier behaviour model trained.")
        click.echo(f"  shifts               : {m['n_shifts']}")
        click.echo(f"  silhouette           : {m['silhouette']}")
        click.echo(f"  ARI vs archetypes    : {m['adjusted_rand_vs_archetypes']}")
        click.echo(f"  persona sizes        : {m['persona_sizes']}")
