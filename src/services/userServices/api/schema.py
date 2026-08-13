from pydantic import BaseModel
class Address(BaseModel):
    address_line1:str,
    address_line2:str,
    city:str,
    country:str,
    pincode:str

    
class RegisterUser(BaseModel):
    name:str,
    email:str,
    phone:str,
    password:str,
    tokenSecret:str,
    created_at:str,
    updated_at:str,
    is_deleted:bool,
    saved_address:Address