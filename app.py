
from datetime import date, datetime, timezone, timedelta
import re, os
from flask import Flask, jsonify, request, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, join_room, disconnect, emit
from werkzeug.utils import secure_filename
from flask import request, jsonify
import traceback
import jwt
from flask_migrate import Migrate
import enum
from sqlalchemy.orm import validates
from PIL import Image                    # ✅ NEW: For image resizing
from functools import lru_cache
from xml.etree.ElementTree import Comment
import secrets       # built-in (used for qr_token generation)
from flask_bcrypt import Bcrypt
import uuid
import os
import requests


app = Flask(__name__)
app.config[
    'SQLALCHEMY_DATABASE_URI'] = "postgresql://kidplay_render_database_2_user:K9usug15F8K7KDqe7bgthxFPczhW5l8g@dpg-d8i493uk1jcs739sc8d0-a.frankfurt-postgres.render.com/kidplay_render_database_2"
socketio = SocketIO(app)
db = SQLAlchemy(app)
migrate = Migrate(app, db)  # 2️⃣ migrate second, now db exists
bcrypt = Bcrypt()

app.config['SECRET_KEY'] = 'a8f4c2e1b5d6f7a8c9e0d1f2b3a4c5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2'
SECRET_KEY = app.config['SECRET_KEY']

# SWISH_CERT          = (os.environ["SWISH_CERT_PATH"], os.environ["SWISH_KEY_PATH"])
# YOUR_SWISH_NUMBER   = os.environ["SWISH_PLATFORM_NUMBER"]   # e.g. "1231234567"
# SWISH_CALLBACK_URL  = os.environ["SWISH_CALLBACK_URL"]       # public HTTPS URL Swish can reach
# PLATFORM_FEE_RATE   = float(os.environ.get("PLATFORM_FEE_RATE", "0.10"))  # default 10 %
 
# SWISH_PAYMENT_URL   = "https://cpc.getswish.net/swish-cpcapi/api/v2/paymentrequests"
# SWISH_PAYOUT_URL    = "https://cpc.getswish.net/swish-cpcapi/api/v1/payouts"


class GenderEnum(enum.Enum):
    Male = "Male"
    Female = "Female"


class ChildEnum(enum.Enum):
    Boy = "Boy"
    Girl = "Girl"


class User(db.Model):
    __tablename__ = 'user_credentials'

    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(200), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    profile               = db.relationship('ParentsProfile',      back_populates='user', uselist=False, cascade='all, delete-orphan')
    parent_profile_images = db.relationship('ParentsProfileImages', back_populates='user', cascade='all, delete-orphan', lazy=True)
    kids_profile          = db.relationship('KidsProfile',          back_populates='user', cascade='all, delete-orphan', lazy=True)  # list now
    attendances           = db.relationship('Attendance', back_populates='user')
    checkins              = db.relationship('CheckIn',    back_populates='user')


class ParentsProfile(db.Model):
    __tablename__ = 'parents_profile'

    id = db.Column(db.Integer, primary_key=True)
    user_auth_id = db.Column(db.Integer,db.ForeignKey('user_credentials.id', ondelete='CASCADE'),nullable=False,unique=True)
    first_name = db.Column(db.String(100))
    last_name = db.Column(db.String(100))
    date_of_birth = db.Column(db.Date)
    gender = db.Column(db.Enum(GenderEnum))
    phone_number = db.Column(db.String(20))
    bio = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, onupdate=lambda: datetime.now(timezone.utc))

    user = db.relationship('User', back_populates='profile') 


class ParentsProfileImages(db.Model):
    __tablename__ = 'parents_profile_images'

    id           = db.Column(db.Integer, primary_key=True)
    user_auth_id = db.Column(db.Integer, db.ForeignKey('user_credentials.id', ondelete='CASCADE'), nullable=False, index=True)
    image_url    = db.Column(db.String(500), nullable=False)
    created_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship('User', back_populates='parent_profile_images')


class KidsProfile(db.Model):
    __tablename__ = 'kids_profile'

    id                     = db.Column(db.Integer, primary_key=True)
    user_auth_id           = db.Column(db.Integer, db.ForeignKey('user_credentials.id', ondelete='CASCADE'), nullable=False, index=True)  # no unique=True
    first_name             = db.Column(db.String(100))
    last_name              = db.Column(db.String(100))
    social_security_number = db.Column(db.String(13), unique=True, nullable=True)
    date_of_birth          = db.Column(db.Date)
    gender                 = db.Column(db.Enum(ChildEnum))
    grade_level            = db.Column(db.String(100), nullable=True)
    hobbies                = db.Column(db.ARRAY(db.String), nullable=True)
    allergies              = db.Column(db.ARRAY(db.String), nullable=True)
    individual_needs       = db.Column(db.ARRAY(db.String), nullable=True)
    created_at             = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at             = db.Column(db.DateTime, onupdate=lambda: datetime.now(timezone.utc))

    user   = db.relationship('User', back_populates='kids_profile')
    images = db.relationship('KidsProfileImages', back_populates='kid', cascade='all, delete-orphan')


class KidsProfileImages(db.Model):
    __tablename__ = 'kids_profile_images'

    id              = db.Column(db.Integer, primary_key=True)
    kids_profile_id = db.Column(db.Integer, db.ForeignKey('kids_profile.id', ondelete='CASCADE'), nullable=False, index=True)
    image_url       = db.Column(db.String(500), nullable=False)
    created_at      = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    kid = db.relationship('KidsProfile', back_populates='images')


class HostVerificationStatus(enum.Enum):
    pending  = 'pending'   # applied, waiting for review
    approved = 'approved'  # verified, can create events
    rejected = 'rejected'  # denied, cannot create events    


# Observe!! total events and participants are calculated properties, not stored in DB. This is to avoid denormalization issues and ensure real-time accuracy.
class EventHost(db.Model):
    """Created when a user chooses to become a host. One-to-one with User."""
    __tablename__ = 'event_hosts'

    id      = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user_credentials.id', ondelete='SET NULL'), nullable=True, unique=True)
    verified_at = db.Column(db.DateTime, nullable=True)  # set only when status → approved
    name    = db.Column(db.String(100), unique=True, nullable=False)

    host_bio           = db.Column(db.Text, nullable=True)
    top_event_hashtags = db.Column(db.ARRAY(db.String), nullable=True)

    verification_status = db.Column(
        db.Enum(HostVerificationStatus),
        default=HostVerificationStatus.pending,
        nullable=False
    )

    # Relationships
    owner  = db.relationship('User', backref=db.backref('event_host', uselist=False))
    images = db.relationship('EventHostImage', back_populates='host', cascade='all, delete-orphan', order_by='EventHostImage.display_order')
    events = db.relationship('EventLocation', back_populates='event_host')

    # ── Derived from ParentsProfile via owner ─────────────────────────────

    @property
    def _user_profile(self):
        return self.owner.profile if self.owner else None

    @property
    def first_name(self):
        return self._user_profile.first_name if self._user_profile else None

    @property
    def last_name(self):
        return self._user_profile.last_name if self._user_profile else None

    @property
    def date_of_birth(self):
        return self._user_profile.date_of_birth if self._user_profile else None

    @property
    def gender(self):
        return self._user_profile.gender if self._user_profile else None

    @property
    def phone_number(self):
        return self._user_profile.phone_number if self._user_profile else None

    # ── Derived from related tables ────────────────────────────────────────

    @property
    def follower_count(self) -> int:
        return Follow.query.filter_by(following_id=self.user_id).count()

    @property
    def total_events_created(self):
        return len(self.events)

    @property
    def total_participants(self):
        return (
            Attendance.query
            .join(EventLocation, EventLocation.id == Attendance.location_id)
            .filter(EventLocation.event_host_id == self.id)
            .count()
        )

    @property
    def is_approved(self):
        return self.verification_status == HostVerificationStatus.approved


class EventHostImage(db.Model):
    """
    Portfolio images the host chooses to display (max 3).
    FK points at event_hosts, not event_host_profiles.
    Enforce the max-3 rule at the service layer before inserting.
    """
    __tablename__ = 'event_host_images'

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    host_id       = db.Column(db.Integer, db.ForeignKey('event_hosts.id', ondelete='CASCADE'), nullable=False)
    
    cover_image_url = db.Column(db.String(500), nullable=True)  # one image per event, no separate table needed

    display_order = db.Column(db.Integer, default=0)
    uploaded_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    host = db.relationship('EventHost', back_populates='images')


class EventCategory(db.Model):
    __tablename__ = 'event_categories'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), unique=True, nullable=False)


class Venue(db.Model):
    """The physical place. Reusable across events."""
    __tablename__ = 'venues'

    id        = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name      = db.Column(db.String(200), nullable=False)
    address   = db.Column(db.String(300), nullable=True)
    latitude  = db.Column(db.Float)
    longitude = db.Column(db.Float)

    events = db.relationship('EventLocation', back_populates='venue')


class EventLocation(db.Model):
    """One specific event instance at a venue."""
    __tablename__ = 'event_locations'

    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    venue_id          = db.Column(db.Integer, db.ForeignKey('venues.id'), nullable=False)
    event_category_id = db.Column(db.Integer, db.ForeignKey('event_categories.id'), nullable=False)
    event_host_id     = db.Column(db.Integer, db.ForeignKey('event_hosts.id'), nullable=False)

    # Event config
    start_time    = db.Column(db.DateTime(timezone=True), nullable=False)
    end_time      = db.Column(db.DateTime(timezone=True), nullable=False)
    event_description   = db.Column(db.String(500))
    max_attendees = db.Column(db.Integer, nullable=False)
    girls_attendees = db.Column(db.Integer, nullable=True)
    boys_attendees  = db.Column(db.Integer, nullable=True)
    base_price    = db.Column(db.Numeric(10, 2))
    currency      = db.Column(db.String(10), default='SEK', nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))

    # Operational state
    is_checkin_closed = db.Column(db.Boolean, default=False, nullable=False)

    # Relationships
    venue               = db.relationship('Venue', back_populates='events')
    event_category      = db.relationship('EventCategory', lazy='selectin')
    event_host          = db.relationship('EventHost', back_populates='events', lazy='selectin')
    attendances         = db.relationship('Attendance', back_populates='location', lazy=True, cascade='all, delete-orphan')
    checkins            = db.relationship('CheckIn', back_populates='location', lazy=True, cascade='all, delete-orphan')
    transactions        = db.relationship('EventTransaction', back_populates='event', lazy=True, cascade='all, delete-orphan')
    # ── Validators ─────────────────────────────────────────────────────────────

    @validates('end_time')
    def validate_end_time(self, key, value):
        if self.start_time and value <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return value

    @validates('girls_attendees', 'boys_attendees')
    def validate_gender_limits(self, key, value):
        if value is not None and value < 0:
            raise ValueError(f"{key} cannot be negative")
        return value

    def validate_attendee_totals(self):
        validate_attendee_totals(self.max_attendees, self.girls_attendees, self.boys_attendees)

    # ── State properties ───────────────────────────────────────────────────────

    @property
    def is_ongoing(self):
        now = datetime.now(timezone.utc)
        return self.start_time <= now <= self.end_time

    @property
    def is_past(self):
        return datetime.now(timezone.utc) > self.end_time

    @property
    def is_upcoming(self):
        return datetime.now(timezone.utc) < self.start_time

    # ── Gender counting ────────────────────────────────────────────────────────

    def _count_by_gender(self, gender: GenderEnum) -> int:
        return (
            Attendance.query
            .join(User, User.id == Attendance.user_id)
            .join(ParentsProfile, ParentsProfile.user_auth_id == User.id)
            .filter(Attendance.location_id == self.id, ParentsProfile.gender == gender)
            .count()
        )

    def can_register(self, gender: GenderEnum) -> tuple[bool, str]:
        total = Attendance.query.filter_by(location_id=self.id).count()
        if total >= self.max_attendees:
            return False, "Event is fully booked"
        if gender == GenderEnum.Male and self.boys_attendees is not None:
            if self._count_by_gender(GenderEnum.Male) >= self.boys_attendees:
                return False, f"No male spots remaining ({self.boys_attendees} max)"
        if gender == GenderEnum.Female and self.girls_attendees is not None:
            if self._count_by_gender(GenderEnum.Female) >= self.girls_attendees:
                return False, f"No female spots remaining ({self.girls_attendees} max)"
        return True, ""


def generate_short_code() -> str:
    """Human-readable fallback code printed on tickets. e.g. TKT-A3FX92"""
    return "TKT-" + secrets.token_hex(3).upper()


class Ticket(db.Model):
    """User-facing proof of purchase. Created when registration is confirmed."""
    __tablename__ = 'tickets'

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ticket_uid    = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    ticket_code   = db.Column(db.String(10), unique=True, nullable=False, default=generate_short_code)
    attendance_id = db.Column(db.Integer, db.ForeignKey('user_attendance.id', ondelete='CASCADE'), nullable=False, unique=True)
    ticket_type   = db.Column(db.String(30), default='standard')
    issued_at     = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Payment snapshot
    amount_paid  = db.Column(db.Numeric(10, 2), nullable=True)
    currency     = db.Column(db.String(10), default='SEK')
    payment_ref  = db.Column(db.String(100), nullable=True)
    paid_at      = db.Column(db.DateTime, nullable=True)

    # Lifecycle
    is_void             = db.Column(db.Boolean, default=False)
    cancelled_at        = db.Column(db.DateTime, nullable=True)
    cancellation_reason = db.Column(db.String(200), nullable=True)

    attendance = db.relationship('Attendance', back_populates='ticket')

    @property
    def is_expired(self) -> bool:
        event_time = self.attendance.location.start_time
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > event_time

    @property
    def is_checked_in(self) -> bool:
        return CheckIn.query.filter_by(
            user_id=self.attendance.user_id,
            location_id=self.attendance.location_id
        ).first() is not None

    @property
    def status(self) -> str:
        if self.is_void:       return "void"
        if self.is_expired:    return "expired"
        if self.is_checked_in: return "used"
        return "active"

    def __repr__(self):
        return f"<Ticket {self.ticket_code} [{self.status}]>"


class CheckIn(db.Model):
    __tablename__ = 'user_checkins'

    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user_credentials.id', ondelete='CASCADE'), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey('event_locations.id', ondelete='CASCADE'), nullable=False)
    timestamp   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user     = db.relationship('User', back_populates='checkins')
    location = db.relationship('EventLocation', back_populates='checkins')

    __table_args__ = (
        db.UniqueConstraint('user_id', 'location_id', name='unique_user_location_checkin'),
    )


class Attendance(db.Model):
    __tablename__ = 'user_attendance'

    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user_credentials.id', ondelete='CASCADE'), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey('event_locations.id', ondelete='CASCADE'), nullable=False)
    timestamp   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user     = db.relationship('User', back_populates='attendances')
    location = db.relationship('EventLocation', back_populates='attendances')
    ticket   = db.relationship('Ticket', back_populates='attendance', uselist=False)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'location_id', name='unique_user_location_attendance'),
    )


class Message(db.Model):
    __tablename__ = 'chat_messages'

    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    sender_id   = db.Column(db.Integer, db.ForeignKey('user_credentials.id'), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('user_credentials.id'), nullable=False)
    message     = db.Column(db.Text, nullable=False)
    reply_to_id = db.Column(db.Integer, db.ForeignKey('chat_messages.id'), nullable=True)
    timestamp   = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    image_url   = db.Column(db.String(), nullable=True)
    is_read     = db.Column(db.Boolean, default=False, nullable=False)

    sender   = db.relationship('User', foreign_keys=[sender_id],   backref=db.backref('sent_messages',     lazy=True))
    receiver = db.relationship('User', foreign_keys=[receiver_id], backref=db.backref('received_messages', lazy=True))
    reply_to = db.relationship('Message', remote_side=[id],        backref=db.backref('replies', lazy=True))

    # ── Derived properties ─────────────────────────────────────────────────────

    @property
    def time_ago(self) -> str:
        """Returns human-readable string like '32 min ago', '2 hrs ago', 'just now'."""
        now  = datetime.now(timezone.utc)
        diff = now - self.timestamp.replace(tzinfo=timezone.utc)
        seconds = int(diff.total_seconds())

        if seconds < 60:
            return "just now"
        if seconds < 3600:
            mins = seconds // 60
            return f"{mins} min ago"
        if seconds < 86400:
            hrs = seconds // 3600
            return f"{hrs} hr ago"
        days = seconds // 86400
        return f"{days} days ago"

    @staticmethod
    def unread_count(user_id: int, other_user_id: int) -> int:
        """Returns number of unread messages from other_user_id to user_id."""
        return (
            Message.query
            .filter_by(sender_id=other_user_id, receiver_id=user_id, is_read=False)
            .count()
        )

    @staticmethod
    def latest_message(user_id: int, other_user_id: int) -> 'Message | None':
        """Returns the most recent message in a conversation between two users."""
        return (
            Message.query
            .filter(
                db.or_(
                    db.and_(Message.sender_id == user_id,       Message.receiver_id == other_user_id),
                    db.and_(Message.sender_id == other_user_id, Message.receiver_id == user_id)
                )
            )
            .order_by(Message.timestamp.desc())
            .first()
        )


class EventHostPaymentDetails(db.Model):
    __tablename__ = 'event_host_payment_details'

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    event_host_id = db.Column(db.Integer, db.ForeignKey('event_hosts.id', ondelete='CASCADE'), nullable=False, unique=True)

    organisation_number = db.Column(db.String(11), nullable=True)   # XXXXXX-XXXX Swedish format
    swish_number        = db.Column(db.String(11), nullable=False)   # 10 digits for Swish för företag

    swish_verified = db.Column(db.Boolean, default=False, nullable=False)  # must be True before first payout

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    event_host = db.relationship('EventHost', backref=db.backref('payment_details', uselist=False))


class TransactionStatus(enum.Enum):
    pending   = 'pending'
    paid      = 'paid'
    declined  = 'declined'
    refunded  = 'refunded'


class EventTransaction(db.Model):
    __tablename__ = 'event_transactions'

    id               = db.Column(db.Integer, primary_key=True, autoincrement=True)
    event_id         = db.Column(db.Integer, db.ForeignKey('event_locations.id', ondelete='RESTRICT'), nullable=False)
    attendee_user_id = db.Column(db.Integer, db.ForeignKey('user_credentials.id', ondelete='RESTRICT'), nullable=False)
    amount           = db.Column(db.Numeric(10, 2), nullable=False)
    currency         = db.Column(db.String(10), default='SEK', nullable=False)
    swish_reference  = db.Column(db.String(100), unique=True, nullable=False)
    status           = db.Column(db.Enum(TransactionStatus), default=TransactionStatus.pending, nullable=False)
    created_at       = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    event    = db.relationship('EventLocation')
    attendee = db.relationship('User')


class PayoutStatus(enum.Enum):
    processing = 'processing'
    completed  = 'completed'
    failed     = 'failed'


class EventPayout(db.Model):
    __tablename__ = 'event_payouts'

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    event_id      = db.Column(db.Integer, db.ForeignKey('event_locations.id', ondelete='RESTRICT'), nullable=False)
    event_host_id = db.Column(db.Integer, db.ForeignKey('event_hosts.id',     ondelete='RESTRICT'), nullable=False)

    gross_amount  = db.Column(db.Numeric(10, 2), nullable=False)
    platform_fee  = db.Column(db.Numeric(10, 2), nullable=False)
    payout_amount = db.Column(db.Numeric(10, 2), nullable=False)

    currency        = db.Column(db.String(10), default='SEK', nullable=False)
    swish_reference = db.Column(db.String(100), unique=True, nullable=False)
    status          = db.Column(db.Enum(PayoutStatus), default=PayoutStatus.processing, nullable=False)

    initiated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = db.Column(db.DateTime, nullable=True)

    event      = db.relationship('EventLocation')
    event_host = db.relationship('EventHost')
    

class Follow(db.Model):
    __tablename__ = 'follows'

    follower_id  = db.Column(db.Integer, db.ForeignKey('user_credentials.id', ondelete='CASCADE'), primary_key=True)
    following_id = db.Column(db.Integer, db.ForeignKey('user_credentials.id', ondelete='CASCADE'), primary_key=True)
    created_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    follower  = db.relationship('User', foreign_keys=[follower_id],  backref='following')
    following = db.relationship('User', foreign_keys=[following_id], backref='followers')

    __table_args__ = (
        db.CheckConstraint('follower_id != following_id', name='no_self_follow'),
    )


class EventLike(db.Model):
    """User saves an event to their favourites list."""
    __tablename__ = 'event_likes'

    user_id  = db.Column(db.Integer, db.ForeignKey('user_credentials.id', ondelete='CASCADE'), primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event_locations.id',  ondelete='CASCADE'), primary_key=True)
    liked_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    user  = db.relationship('User',          backref=db.backref('liked_events', lazy='dynamic'))
    event = db.relationship('EventLocation', backref=db.backref('likes',        lazy='dynamic'))


class ReportTargetType(enum.Enum):
    user  = 'user'
    event = 'event'


class ReportReason(enum.Enum):
    inappropriate_content = 'inappropriate_content'
    spam                  = 'spam'
    fake_profile          = 'fake_profile'
    harassment            = 'harassment'
    underage              = 'underage'
    other                 = 'other'


class ReportStatus(enum.Enum):
    pending   = 'pending'    # not yet reviewed
    reviewed  = 'reviewed'   # seen but no action taken
    resolved  = 'resolved'   # action taken
    dismissed = 'dismissed'  # reviewed, no violation found


class Report(db.Model):
    __tablename__ = 'reports'

    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey('user_credentials.id', ondelete='SET NULL'), nullable=True)

    # what is being reported
    target_type = db.Column(db.Enum(ReportTargetType), nullable=False)
    target_user_id  = db.Column(db.Integer, db.ForeignKey('user_credentials.id',  ondelete='SET NULL'), nullable=True)
    target_event_id = db.Column(db.Integer, db.ForeignKey('event_locations.id',   ondelete='SET NULL'), nullable=True)

    reason = db.Column(db.String(100), nullable=False)  # keep as string, validate at service layer
    details    = db.Column(db.Text, nullable=True)   # optional free-text from reporter
    status     = db.Column(db.Enum(ReportStatus), default=ReportStatus.pending, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    resolved_at = db.Column(db.DateTime, nullable=True)  # set when status → resolved/dismissed

    reporter     = db.relationship('User', foreign_keys=[reporter_id])
    target_user  = db.relationship('User', foreign_keys=[target_user_id])
    target_event = db.relationship('EventLocation', foreign_keys=[target_event_id])

    __table_args__ = (
        # exactly one target must be set, not both, not neither
        db.CheckConstraint(
            '(target_user_id IS NOT NULL AND target_event_id IS NULL) OR '
            '(target_user_id IS NULL AND target_event_id IS NOT NULL)',
            name='report_has_exactly_one_target'
        ),
    )



with app.app_context():
    db.create_all()


# DEF FUNCTIONS

def create_token(user):
    payload = {
        "user_id": user.id,
        "exp": datetime.utcnow() + timedelta(days=7)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def get_current_user_from_token():
    auth_header = request.headers.get('Authorization', None)
    if not auth_header or not auth_header.startswith("Bearer "):
        print("No Authorization header or wrong format")
        return None

    token = auth_header.split(" ")[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        print("Decoded JWT payload:", payload)
        user_id = payload.get('user_id')
        user = User.query.get(user_id)
        print("Fetched user from DB:", user)
        return user
    except jwt.ExpiredSignatureError:
        print("JWT expired")
        return None
    except jwt.InvalidTokenError as e:
        print("JWT invalid:", e)
        return None


def initiate_payout(event_id, host_id):
    payment_details = EventHostPaymentDetails.query.filter_by(event_host_id=host_id).first()

    if not payment_details:
        raise ValueError("Host has no payment details on file.")

    if not payment_details.swish_verified:
        raise ValueError("Host Swish number is not verified. Cannot initiate payout.")

    # safe to proceed


def validate_attendee_totals(max_attendees: int, max_male: int | None, max_female: int | None):
    male = max_male or 0
    female = max_female or 0

    if max_male is not None and male > max_attendees:
        raise ValueError(f"max_male_attendees ({male}) exceeds max_attendees ({max_attendees})")

    if max_female is not None and female > max_attendees:
        raise ValueError(f"max_female_attendees ({female}) exceeds max_attendees ({max_attendees})")

    if max_male is not None and max_female is not None:
        if male + female > max_attendees:
            raise ValueError(
                f"Combined gender limits ({male} + {female} = {male + female}) "
                f"exceed max_attendees ({max_attendees})"
            )
    
        
def create_report(reporter_id, target_type, target_id, reason, details=None):
    # validate reason is a known value
    valid_reasons = {r.value for r in ReportReason}
    if reason not in valid_reasons:
        raise ValueError(f"Invalid reason '{reason}'. Must be one of: {valid_reasons}")

    # validate target_type is a known value
    if not isinstance(target_type, ReportTargetType):
        raise ValueError(f"Invalid target_type '{target_type}'. Must be a ReportTargetType.")

    # prevent a user reporting themselves
    if target_type == ReportTargetType.user and target_id == reporter_id:
        raise ValueError("A user cannot report themselves.")

    report = Report(
        reporter_id=reporter_id,
        target_type=target_type,
        reason=reason,
        details=details,
        target_user_id=target_id  if target_type == ReportTargetType.user  else None,
        target_event_id=target_id if target_type == ReportTargetType.event else None
    )
    db.session.add(report)
    db.session.commit()
    return report  # return it so the caller can use the generated id if needed


def get_conversation_preview(current_user_id, other_user_id):
    latest  = Message.latest_message(current_user_id, other_user_id)
    unread  = Message.unread_count(current_user_id, other_user_id)

    return {
        "latest_message": latest.message if latest else None,
        "time_ago":       latest.time_ago if latest else None,
        "unread_count":   unread
    }


def like_event(user_id: int, event_id: int) -> EventLike:
    # guard: already liked
    existing = EventLike.query.filter_by(user_id=user_id, event_id=event_id).first()
    if existing:
        raise ValueError("Event already in favourites.")

    # guard: event must exist and not be in the past
    event = EventLocation.query.get(event_id)
    if not event:
        raise ValueError("Event does not exist.")
    if event.is_past:
        raise ValueError("Cannot save a past event to favourites.")

    like = EventLike(user_id=user_id, event_id=event_id)
    db.session.add(like)
    db.session.commit()
    return like


def unlike_event(user_id: int, event_id: int) -> None:
    like = EventLike.query.filter_by(user_id=user_id, event_id=event_id).first()
    if not like:
        raise ValueError("Event was not in favourites.")
    db.session.delete(like)
    db.session.commit()


def get_favourite_events(user_id: int) -> list[EventLocation]:
    """Returns all saved events for a user, most recently liked first."""
    return (
        EventLocation.query
        .join(EventLike, EventLike.event_id == EventLocation.id)
        .filter(EventLike.user_id == user_id)
        .order_by(EventLike.liked_at.desc())
        .all()
    )


def has_liked_event(user_id: int, event_id: int) -> bool:
    """Useful for showing a filled/unfilled heart icon on the frontend."""
    return EventLike.query.filter_by(
        user_id=user_id,
        event_id=event_id
    ).first() is not None


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
 
# def _make_payment_ref() -> str:
#     """UUID4 hex, uppercase — unique per Swish request."""
#     return uuid.uuid4().hex.upper()
 
 
# def _swish_put(url: str, payload: dict) -> requests.Response:
#     """PUT wrapper with cert and TLS verify."""
#     return requests.put(url, json=payload, cert=SWISH_CERT, verify=True, timeout=10)
 
 
# def _swish_post(url: str, payload: dict) -> requests.Response:
#     """POST wrapper with cert and TLS verify."""
#     return requests.post(url, json=payload, cert=SWISH_CERT, verify=True, timeout=10)


# ALL ENDPOINTS 

# USER SIGNIN METHOD
@app.route('/sign-in', methods=['POST'])
def sign_in():
    try:
        data = request.get_json()
        email = data.get('email')
        password = data.get('password')

        if not email or not password:
            return jsonify({'error': 'Email and password are required'}), 400

        user = User.query.filter_by(email=email).first()
        if not user or not bcrypt.check_password_hash(user.password_hash, password):
            return jsonify({'message': 'Invalid credentials'}), 401

        payload = {
            'user_id': user.id,
            'exp': datetime.utcnow() + timedelta(days=7)
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm='HS256')
        if isinstance(token, bytes):
            token = token.decode('utf-8')

        return jsonify({'message': 'Sign in successful', 'token': token}), 200

    except Exception as e:
        print("Sign-in error:", e)
        return jsonify({'error': str(e)}), 500


# Getting Sign-in DATA
@app.route('/sign-in', methods=['GET'])
def get_signin_data():
    signin = User.query.all()
    data = [
        {
            'id': rel.id,
            'email': rel.email,
            'password': rel.password,
        }
        for rel in signin
    ]
    return jsonify(data)


# Delete users from the app
@app.route('/delete_user/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    # 1. Verify the token and get the current user
    current_user = get_current_user_from_token()
    if not current_user:
        return jsonify({"error": "Unauthorized"}), 401

    # 2. Allow if it's the user themselves OR an admin
    if current_user.id != user_id and not current_user.is_admin:
        return jsonify({"error": "Forbidden: You can only delete your own account"}), 403

    # 3. Fetch the user to delete
    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    db.session.delete(user)
    db.session.commit()
    return jsonify({"message": "User and all related data deleted successfully"}), 200


# POST USER CREDENTIALS TO DATABASE ✅
@app.route('/userCredentials', methods=['POST'])
def postData():
    try:
        data = request.get_json()
        new_email = data.get('email')
        new_password = data.get('password')

        if not new_email or not new_password:
            return jsonify({'error': 'Email and password are required'}), 400

        # Validate email
        email_regex = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
        if not re.match(email_regex, new_email):
            return jsonify({'message': 'Invalid email format'}), 400

        # Check if email exists
        if User.query.filter_by(email=new_email).first():
            return jsonify({'message': 'Email already exists'}), 409  # ✅ 409 Conflict is more accurate than 400

        # Hash password before storing ✅
        hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
        new_user = User(email=new_email, password_hash=hashed_password)
        db.session.add(new_user)
        db.session.commit()

        # Create token
        payload = {
            'user_id': new_user.id,
            'exp': datetime.utcnow() + timedelta(days=7)
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm='HS256')
        if isinstance(token, bytes):
            token = token.decode('utf-8')

        return jsonify({'message': "New User added", 'token': token}), 201

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# METHOD TO GET AUTHENTICATED USERS LIST — admin only ✅
@app.route("/users", methods=["GET"])
def home():
    current_user = get_current_user_from_token()

    if not current_user:
        return jsonify({"error": "Unauthorized"}), 401

    # ✅ Only admins can list all users
    if not current_user.is_admin:
        return jsonify({"error": "Forbidden: Admins only"}), 403

    tasks = User.query.all()
    task_list = [
        {'id': task.id, 'email': task.email} for task in tasks  # ✅ Never expose passwords
    ]
    return jsonify({"user_details": task_list})


# ─────────────────────────────────────────────────────────────────────────────
# PARENTS PROFILE✅
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/parents/profile', methods=['GET'])
def get_parents_profile():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    profile = user.profile
    if not profile:
        return jsonify({'error': 'Profile not found'}), 404
 
    return jsonify({
        'id':           profile.id,
        'first_name':   profile.first_name,
        'last_name':    profile.last_name,
        'date_of_birth': profile.date_of_birth.isoformat() if profile.date_of_birth else None,
        'gender':       profile.gender.value if profile.gender else None,
        'phone_number': profile.phone_number,
        'bio':          profile.bio,
        'created_at':   profile.created_at.isoformat() if profile.created_at else None,
        'updated_at':   profile.updated_at.isoformat() if profile.updated_at else None,
    }), 200
 
 
@app.route('/parents/profile', methods=['POST'])
def post_parents_profile():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
 
    profile = user.profile
    if not profile:
        profile = ParentsProfile(user_auth_id=user.id)
        db.session.add(profile)
 
    if 'first_name' in data:
        profile.first_name = data['first_name']
    if 'last_name' in data:
        profile.last_name = data['last_name']
    if 'date_of_birth' in data:
        try:
            profile.date_of_birth = date.fromisoformat(data['date_of_birth'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid date_of_birth, expected YYYY-MM-DD'}), 400
    if 'gender' in data:
        gender_map = {'Male': GenderEnum.Male, 'Female': GenderEnum.Female}
        val = data['gender']  # no .lower()
        if val not in gender_map:
            return jsonify({'error': f'Invalid gender: {val}'}), 400
        profile.gender = gender_map[val]
    if 'phone_number' in data:
        profile.phone_number = data['phone_number']
    if 'bio' in data:
        profile.bio = data['bio']
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to save profile'}), 500
 
    return jsonify({'message': 'Parents profile saved'}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# PARENTS PROFILE IMAGES
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/parents/images', methods=['GET'])
def get_parents_images():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    images = user.images  # via back_populates='images' on User
    return jsonify([
        {
            'id':         img.id,
            'image_url':  img.image_url,
            'created_at': img.created_at.isoformat() if img.created_at else None,
        }
        for img in images
    ]), 200
 
 
@app.route('/parents/images', methods=['POST'])
def post_parents_image():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data or 'image_url' not in data:
        return jsonify({'error': 'image_url is required'}), 400
 
    image = ParentsProfileImages(
        user_auth_id=user.id,
        image_url=data['image_url'],
    )
    db.session.add(image)
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to save image'}), 500
 
    return jsonify({'message': 'Image added', 'id': image.id}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# KIDS PROFILE✅
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/kids/profile', methods=['GET'])
def get_kids_profile():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    profiles = user.kids_profile
    if not profiles:
        return jsonify({'error': 'No kids profiles found'}), 404

    return jsonify([
        {
            'id':               p.id,
            'first_name':       p.first_name,
            'last_name':        p.last_name,
            'date_of_birth':    p.date_of_birth.isoformat() if p.date_of_birth else None,
            'gender':           p.gender.value if p.gender else None,
            'grade_level':      p.grade_level,
            'hobbies':          p.hobbies or [],
            'allergies':        p.allergies or [],
            'individual_needs': p.individual_needs or [],
            'created_at':       p.created_at.isoformat() if p.created_at else None,
            'updated_at':       p.updated_at.isoformat() if p.updated_at else None,
        }
        for p in profiles
    ]), 200
    
 
 
@app.route('/kids/profile', methods=['POST'])
def post_kids_profile():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
 
    profile = user.kids_profile
    if not profile:
        profile = KidsProfile(user_auth_id=user.id)
        db.session.add(profile)
 
    if 'first_name' in data:
        profile.first_name = data['first_name']
    if 'last_name' in data:
        profile.last_name = data['last_name']
    if 'social_security_number' in data:
        try:
            profile.social_security_number = str(data['social_security_number'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid social_security_number'}), 400
    if 'date_of_birth' in data:
        try:
            profile.date_of_birth = date.fromisoformat(data['date_of_birth'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid date_of_birth, expected YYYY-MM-DD'}), 400
    if 'gender' in data:
        gender_map = {e.value: e for e in ChildEnum}
        val = data['gender']  # no .lower()
        if val not in gender_map:
            return jsonify({'error': f'Invalid gender: {val}'}), 400
        profile.gender = gender_map[val]
    if 'grade_level' in data:
        profile.grade_level = data['grade_level']
    if 'hobbies' in data:
        if not isinstance(data['hobbies'], list):
            return jsonify({'error': 'hobbies must be a list'}), 400
        profile.hobbies = data['hobbies']
    if 'allergies' in data:
        if not isinstance(data['allergies'], list):
            return jsonify({'error': 'allergies must be a list'}), 400
        profile.allergies = data['allergies']
    if 'individual_needs' in data:
        if not isinstance(data['individual_needs'], list):
            return jsonify({'error': 'individual_needs must be a list'}), 400
        profile.individual_needs = data['individual_needs']
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to save kids profile'}), 500
 
    return jsonify({'message': 'Kids profile saved'}), 201
 
 
@app.route('/kids/profile/<int:kid_id>', methods=['PUT'])
def update_kids_profile(kid_id):
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    profile = KidsProfile.query.filter_by(id=kid_id, user_auth_id=user.id).first()
    if not profile:
        return jsonify({'error': 'Kid profile not found'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    if 'first_name' in data:
        profile.first_name = data['first_name']
    if 'last_name' in data:
        profile.last_name = data['last_name']
    if 'social_security_number' in data:
        profile.social_security_number = str(data['social_security_number'])
    if 'date_of_birth' in data:
        try:
            profile.date_of_birth = date.fromisoformat(data['date_of_birth'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid date_of_birth, expected YYYY-MM-DD'}), 400
    if 'gender' in data:
        gender_map = {e.value: e for e in ChildEnum}
        val = data['gender']
        if val not in gender_map:
            return jsonify({'error': f'Invalid gender: {val}'}), 400
        profile.gender = gender_map[val]
    if 'grade_level' in data:
        profile.grade_level = data['grade_level']
    if 'hobbies' in data:
        if not isinstance(data['hobbies'], list):
            return jsonify({'error': 'hobbies must be a list'}), 400
        profile.hobbies = data['hobbies']
    if 'allergies' in data:
        if not isinstance(data['allergies'], list):
            return jsonify({'error': 'allergies must be a list'}), 400
        profile.allergies = data['allergies']
    if 'individual_needs' in data:
        if not isinstance(data['individual_needs'], list):
            return jsonify({'error': 'individual_needs must be a list'}), 400
        profile.individual_needs = data['individual_needs']

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to update kids profile'}), 500

    return jsonify({'message': 'Kid profile updated'}), 200
 
# ─────────────────────────────────────────────────────────────────────────────
# EVENT HOST ✅
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/host', methods=['GET'])
def get_event_host():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    host = user.event_host
    if not host:
        return jsonify({'error': 'Host profile not found'}), 404
 
    return jsonify({
        'id':                   host.id,
        'name':                 host.name,
        'host_bio':             host.host_bio,
        'top_event_hashtags':   host.top_event_hashtags or [],
        'verification_status':  host.verification_status.value,
        'verified_at':          host.verified_at.isoformat() if host.verified_at else None,
        'first_name':           host.first_name,
        'last_name':            host.last_name,
        'phone_number':         host.phone_number,
        'gender':               host.gender.value if host.gender else None,
        'total_events_created': host.total_events_created,
        'total_participants':   host.total_participants,
        'is_approved':          host.is_approved,
    }), 200
 
 
@app.route('/host', methods=['POST'])
def post_event_host():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
 
    host = user.event_host
 
    if not host:
        # Creating a new host — name is required
        if 'name' not in data:
            return jsonify({'error': 'name is required to register as a host'}), 400
        host = EventHost(user_id=user.id, name=data['name'])
        db.session.add(host)
    else:
        if 'name' in data:
            host.name = data['name']
 
    if 'host_bio' in data:
        host.host_bio = data['host_bio']
    if 'top_event_hashtags' in data:
        if not isinstance(data['top_event_hashtags'], list):
            return jsonify({'error': 'top_event_hashtags must be a list'}), 400
        host.top_event_hashtags = data['top_event_hashtags']
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to save host profile'}), 500
 
    return jsonify({'message': 'Host profile saved', 'id': host.id}), 201
 
 
 # Only an admin can approve a host. This is a separate endpoint to keep the workflow clear and auditable.
@app.route('/host/<int:host_id>/approve', methods=['POST'])
def approve_event_host(host_id):
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    if not user.is_admin:
        return jsonify({'error': 'Forbidden'}), 403

    host = EventHost.query.get(host_id)
    if not host:
        return jsonify({'error': 'Host not found'}), 404

    if host.is_approved:
        return jsonify({'error': 'Host is already approved'}), 409

    host.verification_status = HostVerificationStatus.approved
    host.verified_at = datetime.utcnow()

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to approve host'}), 500

    return jsonify({'message': 'Host approved', 'id': host.id}), 200
 
# ─────────────────────────────────────────────────────────────────────────────
# EVENT HOST IMAGES ✅
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/host/images', methods=['GET'])
def get_host_images():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    host = user.event_host
    if not host:
        return jsonify({'error': 'Host profile not found'}), 404
 
    return jsonify([
        {
            'id':              img.id,
            'cover_image_url': img.cover_image_url,
            'display_order':   img.display_order,
            'uploaded_at':     img.uploaded_at.isoformat() if img.uploaded_at else None,
        }
        for img in host.images
    ]), 200
 
 
@app.route('/host/images', methods=['POST'])
def post_host_image():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    host = user.event_host
    if not host:
        return jsonify({'error': 'Host profile not found'}), 404
 
    # Enforce max-3 rule
    if len(host.images) >= 3:
        return jsonify({'error': 'Maximum of 3 images allowed'}), 400
 
    data = request.get_json()
    if not data or 'cover_image_url' not in data:
        return jsonify({'error': 'cover_image_url is required'}), 400
 
    image = EventHostImage(
        host_id=host.id,
        cover_image_url=data['cover_image_url'],
        display_order=data.get('display_order', len(host.images)),
    )
    db.session.add(image)
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to save image'}), 500
 
    return jsonify({'message': 'Host image added', 'id': image.id}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# EVENT CATEGORIES ✅
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/event-categories', methods=['GET'])
def get_event_categories():
    try:
        categories = EventCategory.query.order_by(EventCategory.name.asc()).all()
        return jsonify([{'id': c.id, 'name': c.name} for c in categories]), 200
    except Exception:
        traceback.print_exc()
        return jsonify({'error': 'Internal server error'}), 500
 
 
@app.route('/event-categories', methods=['POST'])
def post_event_category():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data or 'name' not in data:
        return jsonify({'error': 'name is required'}), 400
 
    if EventCategory.query.filter_by(name=data['name']).first():
        return jsonify({'error': 'Category already exists'}), 409
 
    category = EventCategory(name=data['name'])
    db.session.add(category)
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to create category'}), 500
 
    return jsonify({'message': 'Category created', 'id': category.id}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# VENUES ✅
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/venues', methods=['GET'])
def get_venues():
    try:
        venues = Venue.query.order_by(Venue.name.asc()).all()
        return jsonify([
            {
                'id':        v.id,
                'name':      v.name,
                'address':   v.address,
                'latitude':  v.latitude,
                'longitude': v.longitude,
            }
            for v in venues
        ]), 200
    except Exception:
        traceback.print_exc()
        return jsonify({'error': 'Internal server error'}), 500
 
 
@app.route('/venues', methods=['POST'])
def post_venue():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data or 'name' not in data:
        return jsonify({'error': 'name is required'}), 400
 
    venue = Venue(
        name=data['name'],
        address=data.get('address'),
        latitude=data.get('latitude'),
        longitude=data.get('longitude'),
    )
    db.session.add(venue)
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to create venue'}), 500
 
    return jsonify({'message': 'Venue created', 'id': venue.id}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# EVENT LOCATIONS
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/events', methods=['GET'])
def get_events():
    try:
        events = EventLocation.query.order_by(EventLocation.start_time.asc()).all()
        return jsonify([
            {
                'id':               e.id,
                'venue_id':         e.venue_id,
                'event_category_id':e.event_category_id,
                'event_host_id':    e.event_host_id,
                'start_time':       e.start_time.isoformat(),
                'end_time':         e.end_time.isoformat(),
                'event_description':e.event_description,
                'max_attendees':    e.max_attendees,
                'girls_attendees':  e.girls_attendees,
                'boys_attendees':   e.boys_attendees,
                'base_price':       float(e.base_price) if e.base_price else None,
                'currency':         e.currency,
                'is_checkin_closed':e.is_checkin_closed,
                'is_upcoming':      e.is_upcoming,
                'is_ongoing':       e.is_ongoing,
                'is_past':          e.is_past,
            }
            for e in events
        ]), 200
    except Exception:
        traceback.print_exc()
        return jsonify({'error': 'Internal server error'}), 500
 
 
@app.route('/events', methods=['POST'])
def post_event():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    host = user.event_host
    if not host or not host.is_approved:
        return jsonify({'error': 'Only approved hosts can create events'}), 403
 
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
 
    required = ['venue_id', 'event_category_id', 'start_time', 'end_time', 'max_attendees']
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400
 
    try:
        start_time = datetime.fromisoformat(data['start_time'])
        end_time   = datetime.fromisoformat(data['end_time'])
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid datetime format. Use ISO 8601.'}), 400
 
    event = EventLocation(
        venue_id=data['venue_id'],
        event_category_id=data['event_category_id'],
        event_host_id=host.id,
        start_time=start_time,
        end_time=end_time,
        event_description=data.get('event_description'),
        max_attendees=data['max_attendees'],
        girls_attendees=data.get('girls_attendees'),
        boys_attendees=data.get('boys_attendees'),
        base_price=data.get('base_price'),
        currency=data.get('currency', 'SEK'),
    )
 
    try:
        event.validate_attendee_totals()
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
 
    db.session.add(event)
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to create event'}), 500
 
    return jsonify({'message': 'Event created', 'id': event.id}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ATTENDANCE
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/attendance', methods=['GET'])
def get_attendance():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    attendances = user.attendances
    return jsonify([
        {
            'id':          a.id,
            'location_id': a.location_id,
            'timestamp':   a.timestamp.isoformat() if a.timestamp else None,
        }
        for a in attendances
    ]), 200
 
 
@app.route('/attendance', methods=['POST'])
def post_attendance():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data or 'location_id' not in data:
        return jsonify({'error': 'location_id is required'}), 400
 
    event = EventLocation.query.get(data['location_id'])
    if not event:
        return jsonify({'error': 'Event not found'}), 404
 
    if event.is_checkin_closed or event.is_past:
        return jsonify({'error': 'Registration is closed for this event'}), 400
 
    # Check existing attendance
    existing = Attendance.query.filter_by(user_id=user.id, location_id=event.id).first()
    if existing:
        return jsonify({'error': 'Already registered for this event'}), 409
 
    # Check gender-based capacity
    profile = user.profile
    if profile and profile.gender:
        can_register, reason = event.can_register(profile.gender)
        if not can_register:
            return jsonify({'error': reason}), 400
 
    attendance = Attendance(user_id=user.id, location_id=event.id)
    db.session.add(attendance)
 
    try:
        db.session.flush()  # get attendance.id before creating ticket
        ticket = Ticket(attendance_id=attendance.id)
        db.session.add(ticket)
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to register attendance'}), 500
 
    return jsonify({'message': 'Registered successfully', 'attendance_id': attendance.id, 'ticket_code': ticket.ticket_code}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# CHECK-IN
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/checkin', methods=['GET'])
def get_checkins():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    return jsonify([
        {
            'id':          c.id,
            'location_id': c.location_id,
            'timestamp':   c.timestamp.isoformat() if c.timestamp else None,
        }
        for c in user.checkins
    ]), 200
 
 
@app.route('/checkin', methods=['POST'])
def post_checkin():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data or 'location_id' not in data:
        return jsonify({'error': 'location_id is required'}), 400
 
    event = EventLocation.query.get(data['location_id'])
    if not event:
        return jsonify({'error': 'Event not found'}), 404
 
    if event.is_checkin_closed:
        return jsonify({'error': 'Check-in is closed for this event'}), 400
 
    # Must be registered
    attendance = Attendance.query.filter_by(user_id=user.id, location_id=event.id).first()
    if not attendance:
        return jsonify({'error': 'Not registered for this event'}), 403
 
    # Already checked in?
    existing = CheckIn.query.filter_by(user_id=user.id, location_id=event.id).first()
    if existing:
        return jsonify({'error': 'Already checked in'}), 409
 
    checkin = CheckIn(user_id=user.id, location_id=event.id)
    db.session.add(checkin)
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to check in'}), 500
 
    return jsonify({'message': 'Checked in successfully', 'checkin_id': checkin.id}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# TICKETS
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/tickets', methods=['GET'])
def get_tickets():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    # Collect tickets through attendances
    tickets = [a.ticket for a in user.attendances if a.ticket]
    return jsonify([
        {
            'id':           t.id,
            'ticket_uid':   t.ticket_uid,
            'ticket_code':  t.ticket_code,
            'ticket_type':  t.ticket_type,
            'status':       t.status,
            'amount_paid':  float(t.amount_paid) if t.amount_paid else None,
            'currency':     t.currency,
            'issued_at':    t.issued_at.isoformat() if t.issued_at else None,
            'paid_at':      t.paid_at.isoformat() if t.paid_at else None,
            'is_void':      t.is_void,
        }
        for t in tickets
    ]), 200
 
 
@app.route('/tickets', methods=['POST'])
def post_ticket():
    """
    Tickets are normally auto-created during attendance registration.
    This endpoint handles manual issuance or updating payment details.
    """
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data or 'attendance_id' not in data:
        return jsonify({'error': 'attendance_id is required'}), 400
 
    attendance = Attendance.query.get(data['attendance_id'])
    if not attendance or attendance.user_id != user.id:
        return jsonify({'error': 'Attendance not found'}), 404
 
    ticket = attendance.ticket
    if not ticket:
        ticket = Ticket(attendance_id=attendance.id)
        db.session.add(ticket)
    
    # Update payment details if provided
    if 'amount_paid' in data:
        ticket.amount_paid = data['amount_paid']
    if 'payment_ref' in data:
        ticket.payment_ref = data['payment_ref']
    if 'paid_at' in data:
        try:
            ticket.paid_at = datetime.fromisoformat(data['paid_at'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid paid_at format. Use ISO 8601.'}), 400
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to save ticket'}), 500
 
    return jsonify({'message': 'Ticket saved', 'ticket_code': ticket.ticket_code}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# MESSAGES
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/messages/<int:other_user_id>', methods=['GET'])
def get_messages(other_user_id):
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    messages = (
        Message.query
        .filter(
            db.or_(
                db.and_(Message.sender_id == user.id,       Message.receiver_id == other_user_id),
                db.and_(Message.sender_id == other_user_id, Message.receiver_id == user.id),
            )
        )
        .order_by(Message.timestamp.asc())
        .all()
    )
 
    # Mark incoming messages as read
    for m in messages:
        if m.receiver_id == user.id and not m.is_read:
            m.is_read = True
    db.session.commit()
 
    return jsonify([
        {
            'id':          m.id,
            'sender_id':   m.sender_id,
            'receiver_id': m.receiver_id,
            'message':     m.message,
            'reply_to_id': m.reply_to_id,
            'timestamp':   m.timestamp.isoformat() if m.timestamp else None,
            'image_url':   m.image_url,
            'is_read':     m.is_read,
            'time_ago':    m.time_ago,
        }
        for m in messages
    ]), 200
 
 
@app.route('/messages', methods=['POST'])
def post_message():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    if 'receiver_id' not in data:
        return jsonify({'error': 'receiver_id is required'}), 400
    if 'message' not in data:
        return jsonify({'error': 'message is required'}), 400
 
    if data['receiver_id'] == user.id:
        return jsonify({'error': 'Cannot message yourself'}), 400
 
    message = Message(
        sender_id=user.id,
        receiver_id=data['receiver_id'],
        message=data['message'],
        reply_to_id=data.get('reply_to_id'),
        image_url=data.get('image_url'),
    )
    db.session.add(message)
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to send message'}), 500
 
    return jsonify({'message': 'Message sent', 'id': message.id}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# EVENT HOST PAYMENT DETAILS
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/host/payment-details', methods=['GET'])
def get_host_payment_details():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    host = user.event_host
    if not host:
        return jsonify({'error': 'Host profile not found'}), 404
 
    details = host.payment_details
    if not details:
        return jsonify({'error': 'Payment details not found'}), 404
 
    return jsonify({
        'id':                   details.id,
        'organisation_number':  details.organisation_number,
        'swish_number':         details.swish_number,
        'swish_verified':       details.swish_verified,
        'created_at':           details.created_at.isoformat() if details.created_at else None,
        'updated_at':           details.updated_at.isoformat() if details.updated_at else None,
    }), 200
 
 
@app.route('/host/payment-details', methods=['POST'])
def post_host_payment_details():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    host = user.event_host
    if not host:
        return jsonify({'error': 'Host profile not found'}), 404
 
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
 
    details = host.payment_details
    if not details:
        if 'swish_number' not in data:
            return jsonify({'error': 'swish_number is required'}), 400
        details = EventHostPaymentDetails(event_host_id=host.id, swish_number=data['swish_number'])
        db.session.add(details)
    else:
        if 'swish_number' in data:
            details.swish_number = data['swish_number']
 
    if 'organisation_number' in data:
        details.organisation_number = data['organisation_number']
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to save payment details'}), 500
 
    return jsonify({'message': 'Payment details saved'}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# FOLLOWS
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/follows', methods=['GET'])
def get_follows():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    return jsonify({
        'following': [{'user_id': f.following_id, 'since': f.created_at.isoformat()} for f in user.following],
        'followers': [{'user_id': f.follower_id,  'since': f.created_at.isoformat()} for f in user.followers],
    }), 200
 
 
@app.route('/follows', methods=['POST'])
def post_follow():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data or 'following_id' not in data:
        return jsonify({'error': 'following_id is required'}), 400
 
    if data['following_id'] == user.id:
        return jsonify({'error': 'Cannot follow yourself'}), 400
 
    existing = Follow.query.filter_by(follower_id=user.id, following_id=data['following_id']).first()
    if existing:
        return jsonify({'error': 'Already following this user'}), 409
 
    follow = Follow(follower_id=user.id, following_id=data['following_id'])
    db.session.add(follow)
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to follow user'}), 500
 
    return jsonify({'message': 'Now following user'}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# EVENT LIKES
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/likes', methods=['GET'])
def get_event_likes():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    likes = user.liked_events.all()
    return jsonify([
        {
            'event_id': like.event_id,
            'liked_at': like.liked_at.isoformat() if like.liked_at else None,
        }
        for like in likes
    ]), 200
 
 
@app.route('/likes', methods=['POST'])
def post_event_like():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data or 'event_id' not in data:
        return jsonify({'error': 'event_id is required'}), 400
 
    event = EventLocation.query.get(data['event_id'])
    if not event:
        return jsonify({'error': 'Event not found'}), 404
 
    existing = EventLike.query.filter_by(user_id=user.id, event_id=event.id).first()
    if existing:
        return jsonify({'error': 'Event already liked'}), 409
 
    like = EventLike(user_id=user.id, event_id=event.id)
    db.session.add(like)
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to like event'}), 500
 
    return jsonify({'message': 'Event liked'}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# REPORTS
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/reports', methods=['GET'])
def get_reports():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    reports = Report.query.filter_by(reporter_id=user.id).order_by(Report.created_at.desc()).all()
    return jsonify([
        {
            'id':              r.id,
            'target_type':     r.target_type.value,
            'target_user_id':  r.target_user_id,
            'target_event_id': r.target_event_id,
            'reason':          r.reason,
            'details':         r.details,
            'status':          r.status.value,
            'created_at':      r.created_at.isoformat() if r.created_at else None,
        }
        for r in reports
    ]), 200
 
 
@app.route('/reports', methods=['POST'])
def post_report():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
 
    required = ['target_type', 'reason']
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400
 
    # Validate target_type
    valid_target_types = {e.value: e for e in ReportTargetType}
    if data['target_type'] not in valid_target_types:
        return jsonify({'error': f'Invalid target_type. Choose from: {list(valid_target_types.keys())}'}), 400
 
    target_type = valid_target_types[data['target_type']]
    target_user_id  = data.get('target_user_id')
    target_event_id = data.get('target_event_id')
 
    # Enforce exactly-one-target constraint
    if target_type == ReportTargetType.user and not target_user_id:
        return jsonify({'error': 'target_user_id is required for user reports'}), 400
    if target_type == ReportTargetType.event and not target_event_id:
        return jsonify({'error': 'target_event_id is required for event reports'}), 400
    if target_user_id and target_event_id:
        return jsonify({'error': 'Provide only one of target_user_id or target_event_id'}), 400
 
    # Validate reason enum
    valid_reasons = {e.value for e in ReportReason}
    if data['reason'] not in valid_reasons:
        return jsonify({'error': f'Invalid reason. Choose from: {list(valid_reasons)}'}), 400
 
    report = Report(
        reporter_id=user.id,
        target_type=target_type,
        target_user_id=target_user_id,
        target_event_id=target_event_id,
        reason=data['reason'],
        details=data.get('details'),
    )
    db.session.add(report)
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to submit report'}), 500
 
    return jsonify({'message': 'Report submitted', 'id': report.id}), 201
 


# SWISH_CERT = ("/path/to/swish.crt", "/path/to/swish.key")  # from your bank
# SWISH_HANDEL_URL = "https://cpc.getswish.net/swish-cpcapi/api/v2/paymentrequests"
# YOUR_SWISH_NUMBER = "1231234567"  # your platform's Swish number

# @app.route("/ticket/pay", methods=["POST"])
# def pay_for_ticket():
#     data = request.json
#     event = EventLocation.query.get(data["event_id"])
#     payment_ref = uuid.uuid4().hex.upper()  # unique reference

#     payload = {
#         "payeePaymentReference": payment_ref,
#         "callbackUrl": "https://yourapp.com/swish/callback",  # Swish calls this
#         "payeeAlias": YOUR_SWISH_NUMBER,
#         "currency": "SEK",
#         "amount": str(event.ticket_price),
#         "message": f"Ticket: {event.name}"[:50],  # max 50 chars
#     }

#     response = requests.put(
#         f"{SWISH_HANDEL_URL}/{payment_ref}",
#         json=payload,
#         cert=SWISH_CERT,
#         verify=True
#     )

#     if response.status_code == 201:
#         # Save pending transaction
#         transaction = EventTransaction(
#             event_id=event.id,
#             attendee_user_id=current_user.id,
#             amount=event.ticket_price,
#             swish_reference=payment_ref,
#             status='pending'
#         )
#         db.session.add(transaction)
#         db.session.commit()
#         return jsonify({"payment_reference": payment_ref}), 201

#     return jsonify({"error": "Payment initiation failed"}), 400


# @app.route("/swish/callback", methods=["POST"])
# def swish_callback():
#     data = request.json
#     ref = data.get("payeePaymentReference")

#     transaction = EventTransaction.query.filter_by(swish_reference=ref).first()
#     if not transaction:
#         return "", 404

#     if data.get("status") == "PAID":
#         transaction.status = "paid"
#         db.session.commit()
#         # Optionally: confirm ticket, send confirmation email, etc.

#     elif data.get("status") == "DECLINED":
#         transaction.status = "declined"
#         db.session.commit()

#     return "", 200  # Always return 200 to Swish




# SWISH_PAYOUT_URL = "https://cpc.getswish.net/swish-cpcapi/api/v1/payouts"
# PLATFORM_FEE_PERCENT = 0.10  # your 10% cut

# @app.route("/event/<int:event_id>/payout", methods=["POST"])
# def trigger_payout(event_id):
#     event = EventLocation.query.get_or_404(event_id)
#     host = event.event_host
#     payment_details = host.payment_details

#     if not payment_details or not payment_details.swish_verified:
#         return jsonify({"error": "Host has no verified Swish number"}), 400

#     # Sum all paid transactions for this event
#     paid_transactions = EventTransaction.query.filter_by(
#         event_id=event_id,
#         status="paid"
#     ).all()

#     gross = sum(t.amount for t in paid_transactions)
#     fee = round(gross * PLATFORM_FEE_PERCENT, 2)
#     payout_amount = round(gross - fee, 2)

#     if payout_amount <= 0:
#         return jsonify({"error": "Nothing to pay out"}), 400

#     payout_ref = uuid.uuid4().hex.upper()

#     payload = {
#         "payoutInstructionUUID": payout_ref,
#         "payerPaymentReference":  payout_ref,
#         "payerAlias":  YOUR_SWISH_NUMBER,      # your platform
#         "payeeAlias":  payment_details.swish_number,  # host's number
#         "amount":      str(payout_amount),
#         "currency":    "SEK",
#         "message":     f"Payout: {event.name}"[:50],
#     }

#     response = requests.post(
#         SWISH_PAYOUT_URL,
#         json=payload,
#         cert=SWISH_CERT,
#         verify=True
#     )

#     if response.status_code in (200, 201):
#         payout = EventPayout(
#             event_id=event_id,
#             event_host_id=host.id,
#             gross_amount=gross,
#             platform_fee=fee,
#             payout_amount=payout_amount,
#             swish_reference=payout_ref,
#             status="processing"
#         )
#         db.session.add(payout)
#         db.session.commit()
#         return jsonify({"payout_reference": payout_ref}), 200

#     return jsonify({"error": "Payout failed"}), 400


# ─────────────────────────────────────────────────────────────────────────────
# POST /ticket/pay  —  initiate Swish payment for an event ticket
# ─────────────────────────────────────────────────────────────────────────────
 
# @app.route("/ticket/pay", methods=["POST"])
# def pay_for_ticket():
#     """
#     Initiates a Swish payment request for a registered attendee.
 
#     Expected JSON body:
#         { "event_id": <int> }
 
#     Flow:
#         1. Verify the caller is authenticated and has an attendance record.
#         2. Ensure no paid/pending transaction already exists (idempotency guard).
#         3. PUT the payment request to Swish.
#         4. Persist a pending EventTransaction row.
#         5. Return the payment_reference so the client can poll status.
#     """
#     user = get_current_user_from_token()
#     if not user:
#         return jsonify({"error": "Unauthorized"}), 401
 
#     data = request.get_json()
#     if not data or "event_id" not in data:
#         return jsonify({"error": "event_id is required"}), 400
 
#     event = EventLocation.query.get(data["event_id"])
#     if not event:
#         return jsonify({"error": "Event not found"}), 404
 
#     if event.base_price is None or event.base_price <= 0:
#         return jsonify({"error": "This event has no ticket price"}), 400
 
#     # Must be registered before paying
#     attendance = Attendance.query.filter_by(
#         user_id=user.id, location_id=event.id
#     ).first()
#     if not attendance:
#         return jsonify({"error": "You are not registered for this event"}), 403
 
#     # Idempotency — block duplicate payments
#     existing = EventTransaction.query.filter_by(
#         event_id=event.id,
#         attendee_user_id=user.id,
#     ).filter(
#         EventTransaction.status.in_([
#             TransactionStatus.pending.value,
#             TransactionStatus.paid.value,
#         ])
#     ).first()
#     if existing:
#         return jsonify({
#             "error": "A payment already exists for this registration",
#             "status": existing.status,
#             "payment_reference": existing.swish_reference,
#         }), 409
 
#     payment_ref = _make_payment_ref()
 
#     swish_payload = {
#         "payeePaymentReference": payment_ref,
#         "callbackUrl":           SWISH_CALLBACK_URL + "/swish/callback",
#         "payeeAlias":            YOUR_SWISH_NUMBER,
#         "currency":              event.currency or "SEK",
#         "amount":                str(event.base_price),
#         "message":               f"Ticket event {event.id}"[:50],
#     }
 
#     try:
#         response = _swish_put(
#             f"{SWISH_PAYMENT_URL}/{payment_ref}",
#             swish_payload,
#         )
#     except requests.RequestException as exc:
#         traceback.print_exc()
#         return jsonify({"error": "Could not reach Swish", "details": str(exc)}), 502
 
#     if response.status_code != 201:
#         return jsonify({
#             "error": "Swish rejected the payment request",
#             "swish_status": response.status_code,
#             "swish_body":   response.text,
#         }), 400
 
#     # Persist pending transaction
#     transaction = EventTransaction(
#         event_id=event.id,
#         attendee_user_id=user.id,
#         amount=event.base_price,
#         currency=event.currency or "SEK",
#         swish_reference=payment_ref,
#         status=TransactionStatus.pending.value,
#     )
#     db.session.add(transaction)
 
#     try:
#         db.session.commit()
#     except Exception:
#         db.session.rollback()
#         traceback.print_exc()
#         return jsonify({"error": "Payment initiated but failed to persist transaction"}), 500
 
#     return jsonify({"payment_reference": payment_ref}), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# POST /swish/callback  —  Swish server-to-server webhook
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route("/swish/callback", methods=["POST"])
def swish_callback():
    """
    Swish calls this endpoint after every status change.
    Must always return 200; Swish retries on non-200 responses.
 
    Swish payload shape:
        {
          "payeePaymentReference": "...",
          "status": "PAID" | "DECLINED" | "ERROR",
          ...
        }
    """
    data = request.get_json(silent=True)
    if not data:
        # Still return 200 — log and move on
        print("swish_callback: empty or non-JSON body")
        return "", 200
 
    ref    = data.get("payeePaymentReference")
    status = data.get("status", "").upper()
 
    transaction = EventTransaction.query.filter_by(swish_reference=ref).first()
    if not transaction:
        print(f"swish_callback: unknown reference {ref!r}")
        return "", 200  # unknown ref — still 200 so Swish stops retrying
 
    if status == "PAID" and transaction.status != TransactionStatus.paid.value:
        transaction.status = TransactionStatus.paid.value
 
        # Stamp the ticket as paid
        attendance = Attendance.query.filter_by(
            user_id=transaction.attendee_user_id,
            location_id=transaction.event_id,
        ).first()
        if attendance and attendance.ticket:
            attendance.ticket.amount_paid  = transaction.amount
            attendance.ticket.currency     = transaction.currency
            attendance.ticket.payment_ref  = ref
            attendance.ticket.paid_at      = datetime.now(timezone.utc)
 
    elif status == "DECLINED":
        transaction.status = TransactionStatus.declined.value
 
    elif status == "ERROR":
        # Treat ERROR the same as DECLINED for now; adjust as needed
        transaction.status = TransactionStatus.declined.value
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        # Still return 200 — a retry won't help a DB error, log it instead
 
    return "", 200
 
 
# ─────────────────────────────────────────────────────────────────────────────
# GET /ticket/pay/status/<payment_ref>  —  client polls payment outcome
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route("/ticket/pay/status/<string:payment_ref>", methods=["GET"])
def get_payment_status(payment_ref: str):
    """
    Lets the frontend poll for the outcome of a Swish payment without
    waiting for the callback to fire.
    """
    user = get_current_user_from_token()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
 
    transaction = EventTransaction.query.filter_by(
        swish_reference=payment_ref,
        attendee_user_id=user.id,          # users can only see their own
    ).first()
    if not transaction:
        return jsonify({"error": "Transaction not found"}), 404
 
    return jsonify({
        "payment_reference": transaction.swish_reference,
        "status":            transaction.status,
        "amount":            float(transaction.amount),
        "currency":          transaction.currency,
    }), 200
 
 
# ─────────────────────────────────────────────────────────────────────────────
# POST /event/<event_id>/payout  —  trigger host payout after event ends
# ─────────────────────────────────────────────────────────────────────────────
 
# @app.route("/event/<int:event_id>/payout", methods=["POST"])
# def trigger_payout(event_id: int):
#     """
#     Calculates the host's payout from all paid transactions for the event,
#     deducts the platform fee, and initiates a Swish payout.
 
#     Guard rails:
#         - Caller must be the event's host (or an admin — extend as needed).
#         - Event must have ended before a payout is allowed.
#         - Host must have a verified Swish number.
#         - A payout can only be triggered once per event.
#     """
#     user = get_current_user_from_token()
#     if not user:
#         return jsonify({"error": "Unauthorized"}), 401
 
#     event = EventLocation.query.get_or_404(event_id)
#     host  = event.event_host
 
#     # Only the owning host may trigger their own payout
#     if not host or host.user_id != user.id:
#         return jsonify({"error": "Forbidden — you are not the host of this event"}), 403
 
#     if not event.is_past:
#         return jsonify({"error": "Payout can only be triggered after the event has ended"}), 400
 
#     payment_details = host.payment_details
#     if not payment_details or not payment_details.swish_verified:
#         return jsonify({"error": "Host has no verified Swish number"}), 400
 
#     # Idempotency — one payout per event
#     existing_payout = EventPayout.query.filter_by(event_id=event_id).first()
#     if existing_payout:
#         return jsonify({
#             "error":           "Payout already exists for this event",
#             "status":          existing_payout.status,
#             "payout_reference": existing_payout.swish_reference,
#         }), 409
 
#     # Sum all confirmed paid transactions for this event
#     paid_transactions = EventTransaction.query.filter_by(
#         event_id=event_id,
#         status=TransactionStatus.paid.value,
#     ).all()
 
#     if not paid_transactions:
#         return jsonify({"error": "No paid transactions found for this event"}), 400
 
#     gross         = sum(t.amount for t in paid_transactions)
#     platform_fee  = round(float(gross) * PLATFORM_FEE_RATE, 2)
#     payout_amount = round(float(gross) - platform_fee, 2)
 
#     if payout_amount <= 0:
#         return jsonify({"error": "Payout amount is zero after fee deduction"}), 400
 
#     payout_ref = _make_payment_ref()
 
#     swish_payload = {
#         "payoutInstructionUUID":  payout_ref,
#         "payerPaymentReference":  payout_ref,
#         "payerAlias":             YOUR_SWISH_NUMBER,
#         "payeeAlias":             payment_details.swish_number,
#         "amount":                 str(payout_amount),
#         "currency":               "SEK",
#         "message":                f"Payout event {event_id}"[:50],
#     }
 
#     try:
#         response = _swish_post(SWISH_PAYOUT_URL, swish_payload)
#     except requests.RequestException as exc:
#         traceback.print_exc()
#         return jsonify({"error": "Could not reach Swish", "details": str(exc)}), 502
 
#     if response.status_code not in (200, 201):
#         return jsonify({
#             "error":        "Swish rejected the payout request",
#             "swish_status": response.status_code,
#             "swish_body":   response.text,
#         }), 400
 
#     payout = EventPayout(
#         event_id=event_id,
#         event_host_id=host.id,
#         gross_amount=gross,
#         platform_fee=platform_fee,
#         payout_amount=payout_amount,
#         currency="SEK",
#         swish_reference=payout_ref,
#         status=PayoutStatus.processing.value,
#     )
#     db.session.add(payout)
 
#     try:
#         db.session.commit()
#     except Exception:
#         db.session.rollback()
#         traceback.print_exc()
#         return jsonify({"error": "Payout initiated but failed to persist record"}), 500
 
#     return jsonify({
#         "payout_reference": payout_ref,
#         "gross_amount":     float(gross),
#         "platform_fee":     platform_fee,
#         "payout_amount":    payout_amount,
#     }), 200
 
 
# ─────────────────────────────────────────────────────────────────────────────
# GET /event/<event_id>/payout  —  check payout status
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route("/event/<int:event_id>/payout", methods=["GET"])
def get_payout_status(event_id: int):
    """Returns the current payout record for an event (host-only)."""
    user = get_current_user_from_token()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
 
    event = EventLocation.query.get_or_404(event_id)
    host  = event.event_host
 
    if not host or host.user_id != user.id:
        return jsonify({"error": "Forbidden"}), 403
 
    payout = EventPayout.query.filter_by(event_id=event_id).first()
    if not payout:
        return jsonify({"error": "No payout found for this event"}), 404
 
    return jsonify({
        "id":               payout.id,
        "status":           payout.status,
        "gross_amount":     float(payout.gross_amount),
        "platform_fee":     float(payout.platform_fee),
        "payout_amount":    float(payout.payout_amount),
        "currency":         payout.currency,
        "payout_reference": payout.swish_reference,
        "initiated_at":     payout.initiated_at.isoformat() if payout.initiated_at else None,
        "completed_at":     payout.completed_at.isoformat() if payout.completed_at else None,
    }), 200

