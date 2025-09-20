import datetime as dt
from sqlalchemy import String, Integer, BigInteger, DateTime, Boolean, ForeignKey, Text, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    tos_accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user")

class Panel(Base):
    __tablename__ = "panels"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(100))
    base_url: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(128))
    password: Mapped[str] = mapped_column(String(128))
    domain: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(32), default="active")
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    user: Mapped["User"] = relationship(back_populates="subscriptions")

class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    provider: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(100), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

class Broadcast(Base):
    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

class Tariff(Base):
    __tablename__ = "tariffs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(64))
    days: Mapped[int] = mapped_column(Integer)
    price_rub: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
