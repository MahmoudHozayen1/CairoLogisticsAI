"""WTForms definitions (server-side validation + CSRF protection)."""
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from wtforms import (
    StringField, PasswordField, SelectField, FloatField, TextAreaField,
    IntegerField, BooleanField, SubmitField,
)
from wtforms.validators import (
    DataRequired, Email, Length, EqualTo, Optional, NumberRange, Regexp,
)

from .models import Role

PHONE_RE = r"^[0-9+\-\s]{7,20}$"


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember me")
    submit = SubmitField("Sign In")


class RegisterForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(2, 120)])
    business_name = StringField("Business name", validators=[Optional(), Length(0, 160)])
    email = StringField("Email", validators=[DataRequired(), Email()])
    phone = StringField("Phone", validators=[Optional(), Regexp(PHONE_RE, message="Enter a valid phone number.")])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=8, message="Use at least 8 characters.")])
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password", message="Passwords must match.")])
    submit = SubmitField("Create merchant account")


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
        choices=[("Motorcycle", "Motorcycle"), ("Car", "Car"), ("Van", "Van"), ("Bicycle", "Bicycle")],
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
    hub_id = SelectField("Origin hub", coerce=int, validators=[Optional()])
    submit = SubmitField("Create shipment")


class DeliveryUpdateForm(FlaskForm):
    note = StringField("Note", validators=[Optional(), Length(0, 255)])
    proof = FileField("Proof of delivery (photo)", validators=[
        FileAllowed(["png", "jpg", "jpeg", "webp", "gif"], "Images only."),
    ])
    submit = SubmitField("Confirm")


class RoadClosureForm(FlaskForm):
    name = StringField("Name / road", validators=[DataRequired(), Length(2, 120)])
    reason = StringField("Reason", validators=[Optional(), Length(0, 255)])
    lat = FloatField("Latitude", validators=[DataRequired(), NumberRange(-90, 90)])
    lon = FloatField("Longitude", validators=[DataRequired(), NumberRange(-180, 180)])
    radius_m = IntegerField("Radius (m)", validators=[DataRequired(), NumberRange(20, 3000)], default=150)
    submit = SubmitField("Add closure")
