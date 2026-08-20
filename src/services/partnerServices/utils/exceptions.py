class PartnerServiceError(Exception):
    """Base for partner-service domain errors."""


class InvalidCredentialsError(PartnerServiceError):
    # Deliberately does not say whether the phone or the password was wrong —
    # distinguishing them lets an attacker enumerate registered partners.
    def __init__(self, message: str = "Invalid phone or password"):
        self.message = message
        super().__init__(message)


class PhoneAlreadyExistsError(PartnerServiceError):
    def __init__(self, message: str = "Phone already registered"):
        self.message = message
        super().__init__(message)


class EmailAlreadyExistsError(PartnerServiceError):
    def __init__(self, message: str = "Email already registered"):
        self.message = message
        super().__init__(message)


class RegistrationFailedError(PartnerServiceError):
    def __init__(
        self,
        message: str = "Password could not be verified after registration",
    ):
        self.message = message
        super().__init__(message)


class PartnerNotFoundError(PartnerServiceError):
    def __init__(self, message: str = "Partner not found"):
        self.message = message
        super().__init__(message)


class VehicleNotFoundError(PartnerServiceError):
    # Raised both when the vehicle does not exist and when it belongs to another
    # partner. The caller maps both to 404: a 403 for someone else's vehicle
    # would confirm that the id is real.
    def __init__(self, message: str = "Vehicle not found"):
        self.message = message
        super().__init__(message)


class RegistrationNumberExistsError(PartnerServiceError):
    # A number plate identifies one physical vehicle. Two partners claiming the
    # same plate means one of them is lying, and it is not this service's job to
    # guess which — both are refused until operations resolves it.
    def __init__(self, message: str = "Vehicle registration number already in use"):
        self.message = message
        super().__init__(message)


class InvalidStatusTransitionError(PartnerServiceError):
    def __init__(self, message: str = "That status change is not allowed"):
        self.message = message
        super().__init__(message)


class NotVerifiedError(PartnerServiceError):
    def __init__(self, message: str = "Partner is not verified yet"):
        self.message = message
        super().__init__(message)


class NoActiveVehicleError(PartnerServiceError):
    def __init__(
        self,
        message: str = "Set an active verified vehicle before going online",
    ):
        self.message = message
        super().__init__(message)
