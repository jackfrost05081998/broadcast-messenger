from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    facebook_user_id = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    meta_app_id = Column(String(32), nullable=True)
    meta_app_secret = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    facebook_account = relationship(
        "FacebookAccount", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    pages = relationship("FacebookPage", back_populates="user", cascade="all, delete-orphan")
    broadcasts = relationship("Broadcast", back_populates="user", cascade="all, delete-orphan")
    message_templates = relationship(
        "MessageTemplate", back_populates="user", cascade="all, delete-orphan"
    )
    page_automations = relationship(
        "PageAutomation", back_populates="user", cascade="all, delete-orphan"
    )


class FacebookAccount(Base):
    __tablename__ = "facebook_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    facebook_user_id = Column(String(64), nullable=False, index=True)
    access_token = Column(Text, nullable=False)
    token_expires_at = Column(DateTime, nullable=True)
    name = Column(String(255), nullable=True)
    connected_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="facebook_account")


class FacebookPage(Base):
    __tablename__ = "facebook_pages"
    __table_args__ = (UniqueConstraint("user_id", "page_id", name="uq_user_page"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    page_id = Column(String(64), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    access_token = Column(Text, nullable=False)
    picture_url = Column(String(512), nullable=True)
    category = Column(String(255), nullable=True)
    connected_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="pages")


class PageContact(Base):
    __tablename__ = "page_contacts"
    __table_args__ = (UniqueConstraint("user_id", "page_id", "psid", name="uq_page_contact"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    page_id = Column(String(64), nullable=False, index=True)
    psid = Column(String(64), nullable=False, index=True)
    name = Column(String(255), nullable=False, default="Unknown")
    updated_time = Column(String(64), nullable=True)
    message_count = Column(Integer, default=0)
    synced_at = Column(DateTime, default=datetime.utcnow)
    last_inbound_at = Column(DateTime, nullable=True)
    auto_reply_sent_at = Column(DateTime, nullable=True)


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    page_id = Column(String(64), nullable=False)
    page_name = Column(String(255), nullable=False)
    message_text = Column(Text, nullable=False)
    messaging_type = Column(String(32), nullable=False, default="MESSAGE_TAG")
    message_tag = Column(String(64), nullable=True)
    total_recipients = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    status = Column(String(32), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="broadcasts")
    recipients = relationship(
        "BroadcastRecipient", back_populates="broadcast", cascade="all, delete-orphan"
    )


class BroadcastRecipient(Base):
    __tablename__ = "broadcast_recipients"

    id = Column(Integer, primary_key=True, index=True)
    broadcast_id = Column(Integer, ForeignKey("broadcasts.id"), nullable=False, index=True)
    recipient_psid = Column(String(64), nullable=False)
    recipient_name = Column(String(255), nullable=True)
    success = Column(Boolean, default=False)
    error_message = Column(Text, nullable=True)

    broadcast = relationship("Broadcast", back_populates="recipients")


class MessageTemplate(Base):
    __tablename__ = "message_templates"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    body = Column(Text, nullable=False)
    kind = Column(String(32), nullable=False, default="general")  # follow_up, reply, general
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="message_templates")


class PageAutomation(Base):
    __tablename__ = "page_automations"
    __table_args__ = (UniqueConstraint("user_id", "page_id", name="uq_user_page_automation"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    page_id = Column(String(64), nullable=False, index=True)
    follow_up_enabled = Column(Boolean, default=False)
    follow_up_days = Column(Integer, default=7)
    follow_up_template_id = Column(Integer, ForeignKey("message_templates.id"), nullable=True)
    reply_enabled = Column(Boolean, default=False)
    reply_template_id = Column(Integer, ForeignKey("message_templates.id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="page_automations")
    follow_up_template = relationship("MessageTemplate", foreign_keys=[follow_up_template_id])
    reply_template = relationship("MessageTemplate", foreign_keys=[reply_template_id])


class ScheduledFollowUp(Base):
    __tablename__ = "scheduled_follow_ups"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    page_id = Column(String(64), nullable=False, index=True)
    recipient_psid = Column(String(64), nullable=False, index=True)
    recipient_name = Column(String(255), nullable=True)
    template_id = Column(Integer, ForeignKey("message_templates.id"), nullable=True)
    message_text = Column(Text, nullable=False)
    scheduled_at = Column(DateTime, nullable=False, index=True)
    status = Column(String(32), default="pending")  # pending, sent, failed, cancelled
    source_broadcast_id = Column(Integer, ForeignKey("broadcasts.id"), nullable=True)
    sent_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
