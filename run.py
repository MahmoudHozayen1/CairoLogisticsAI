"""Development entry point.

Run with::

    python run.py

or, using the Flask CLI::

    flask --app run run
"""
from app import create_app
from app.extensions import db

app = create_app()


@app.shell_context_processor
def shell_context():
    from app import models
    return {"db": db, **{n: getattr(models, n) for n in (
        "User", "Hub", "Shipment", "ShipmentEvent", "RouteStop", "Role", "ShipmentStatus",
    )}}


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
