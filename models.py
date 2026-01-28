from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import BigInteger, Text, DateTime, Integer, func
from datetime import datetime

class Base(DeclarativeBase):
    pass

db = SQLAlchemy(model_class=Base)

class User(db.Model):
    __tablename__ = "users"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=True)
    first_name: Mapped[str] = mapped_column(Text, nullable=True)
    lang: Mapped[str] = mapped_column(Text, default="hinglish")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

class Request(db.Model):
    __tablename__ = "requests"
    request_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    thread_id: Mapped[int] = mapped_column(Integer, nullable=True)
    part_no: Mapped[int] = mapped_column(Integer, nullable=True)

class Ban(db.Model):
    __tablename__ = "bans"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=True)
    banned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())

class Setting(db.Model):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=True)
