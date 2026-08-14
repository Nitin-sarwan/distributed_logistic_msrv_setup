from datetime import datetime 
from sqlalchemy import Boolean,String,func,DateTime,Text
from sqlalchemy.orm import Mapped,mapped_column

from src.services.userServices.database.base import Base

class User(Base):
    __tablename__="users"
    id:Mapped[int]=mapped_column(primary_key=True,autoincrement=True,)
    name:Mapped[str]=mapped_column(String(100),nullable=False,)
    email:Mapped[str]=mapped_column(String(255),nullable=False,unique=True,index=True,)
    phone:Mapped[str]=mapped_column(String(10),nullable=False,unique=True,)
    token_secret:Mapped[str]=mapped_column(Text,nullable=False,)
    password:Mapped[str]=mapped_column(Text,nullable=False)
    is_deleted:Mapped[bool]=mapped_column(Boolean,default=False,)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False,server_default=func.now())
    updated_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True,server_default=func.now(),onupdate=func.now())