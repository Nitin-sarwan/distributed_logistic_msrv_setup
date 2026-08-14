from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator


class RegisterUser(BaseModel):
    name: str
    email: EmailStr
    phone: str | None = None
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().lower()

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.isdigit():
            raise ValueError("Phone must contain only digits")
        if len(value) != 10:
            raise ValueError("Phone must of 10 digits")
        return value


class UserResponse(BaseModel):
    # Never expose password or token_secret.
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: EmailStr
    phone: str | None
    created_at: datetime


class LoginUser(BaseModel):
    # No from_attributes here: this is client input, never built from an ORM row.
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        # Must match RegisterUser's normalisation, or a user who registered as
        # "Aer@Gmail.com" could never log in as "aer@gmail.com".
        return str(value).strip().lower()


class AuthResponse(BaseModel):
    """Returned by both register and login — same shape either way."""

    model_config = ConfigDict(from_attributes=True)

    user: UserResponse
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    device_session: str
    device_id: str

