"""WTForms definitions (server-side validation + CSRF protection)."""
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from wtforms import (
    StringField, PasswordField, SelectField, FloatField, TextAreaField,
    IntegerField, BooleanField, SubmitField,
)
from wtforms.validators import (
    DataRequired, Email, Length, EqualTo, Optional, NumberRange, Regexp,
    ValidationError,
)

from .models import Role, User, Vehicle

PHONE_RE = r"^[0-9+\-\s]{7,20}$"


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember me")
    submit = SubmitField("Sign In")


class RegisterForm(FlaskForm):
    # Public self-service signup roles. NOTE: exposing ADMIN here lets anyone
    # self-provision full admin access — keep this behind a trusted environment.
    ROLE_CHOICES = [
        (Role.MERCHANT, "Merchant"),
        (Role.COURIER, "Courier"),
        (Role.ADMIN, "Admin"),
    ]

    name = StringField("Full name", validators=[DataRequired(), Length(2, 120)])
    role = SelectField("I am a", choices=ROLE_CHOICES, default=Role.MERCHANT, validators=[DataRequired()])
    business_name = StringField("Business name", validators=[Optional(), Length(0, 160)])
    vehicle_type = SelectField("Vehicle", choices=Vehicle.CHOICES, validators=[Optional()])
    email = StringField("Email", validators=[DataRequired(), Email()])
    phone = StringField("Phone", validators=[Optional(), Regexp(PHONE_RE, message="Enter a valid phone number.")])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=8, message="Use at least 8 characters.")])
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password", message="Passwords must match.")])
    submit = SubmitField("Create account")

    def validate_email(self, field):
        """Reject duplicate emails with a field-level error (case-insensitive)."""
        if User.query.filter_by(email=field.data.lower().strip()).first():
            raise ValidationError("An account with that email already exists.")


class HubForm(FlaskForm):
    name = StringField("Hub name", validators=[DataRequired(), Length(2, 120)])
    address = StringField("Address", validators=[Optional(), Length(0, 255)])
    lat = FloatField("Latitude", validators=[DataRequired(), NumberRange(-90, 90)])
    lon = FloatField("Longitude", validators=[DataRequired(), NumberRange(-180, 180)])
    submit = SubmitField("Save hub")


class CourierForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(2, 120)])
    email = StringField("Email", validators=[DataRequired(), Email()])
    phone = StringField("Phone", validators=[Optional(), Regexp(PHONE_RE)])
    hub_id = SelectField("Assigned hub", coerce=int, validators=[DataRequired()])
    vehicle_type = SelectField(
        "Vehicle",
        choices=Vehicle.CHOICES,
        validators=[DataRequired()],
    )
    password = PasswordField("Password", validators=[Optional(), Length(min=8)])
    submit = SubmitField("Save courier")


class ShipmentForm(FlaskForm):
    receiver_name = StringField("Receiver name", validators=[DataRequired(), Length(2, 120)])
    receiver_phone = StringField("Receiver phone", validators=[DataRequired(), Regexp(PHONE_RE)])
    district = StringField("District / Area", validators=[Optional(), Length(0, 120)])
    address = StringField("Street address", validators=[Optional(), Length(0, 255)])
    landmark = StringField("Landmark", validators=[Optional(), Length(0, 255)])
    lat = FloatField("Latitude", validators=[DataRequired(), NumberRange(-90, 90)])
    lon = FloatField("Longitude", validators=[DataRequired(), NumberRange(-180, 180)])
    package_description = StringField("Package description", validators=[Optional(), Length(0, 255)])
    weight_kg = FloatField("Weight (kg)", validators=[Optional(), NumberRange(0, 1000)], default=1.0)
    cod_amount = FloatField("Cash on delivery (EGP)", validators=[Optional(), NumberRange(0, 1_000_000)], default=0.0)
    delivery_notes = TextAreaField(
        "Delivery notes / handling instructions",
        validators=[Optional(), Length(0, 500)],
        description="e.g. fragile, don't stack, deliver 6–9 pm, leave with doorman",
    )
    hub_id = SelectField("Origin hub", coerce=int, validators=[Optional()])
    submit = SubmitField("Create shipment")


class AdminShipmentForm(ShipmentForm):
    """Shipment form for admins, who must also choose the owning merchant."""
    merchant_id = SelectField("Merchant", coerce=int, validators=[DataRequired()])


class DeliveryUpdateForm(FlaskForm):
    note = StringField("Note", validators=[Optional(), Length(0, 255)])
    proof = FileField("Proof of delivery (photo)", validators=[
        FileAllowed(["png", "jpg", "jpeg", "webp", "gif"], "Images only."),
    ])
    # Populated client-side from the browser Geolocation API for the GIS
    # delivery confirmation. Optional: if absent, a point near the destination
    # is simulated so the geofence check still runs.
    deliver_lat = StringField("Delivery latitude", validators=[Optional()])
    deliver_lon = StringField("Delivery longitude", validators=[Optional()])
    submit = SubmitField("Confirm")


class RoadClosureForm(FlaskForm):
    name = StringField("Name / road", validators=[DataRequired(), Length(2, 120)])
    reason = StringField("Reason", validators=[Optional(), Length(0, 255)])
    lat = FloatField("Latitude", validators=[DataRequired(), NumberRange(-90, 90)])
    lon = FloatField("Longitude", validators=[DataRequired(), NumberRange(-180, 180)])
    radius_m = IntegerField("Radius (m)", validators=[DataRequired(), NumberRange(20, 3000)], default=150)
    submit = SubmitField("Add closure")
