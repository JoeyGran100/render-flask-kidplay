import re, os
from threading import Thread
from flask import Flask, jsonify, logging, request, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, leave_room, join_room
from werkzeug.utils import secure_filename
from flask import request, jsonify
import traceback
import jwt
from flask_migrate import Migrate
import enum
from sqlalchemy.orm import validates, joinedload
from PIL import Image                    # ✅ NEW: For image resizing
from functools import lru_cache
from xml.etree.ElementTree import Comment
import secrets       # built-in (used for qr_token generation)
from flask_bcrypt import Bcrypt
import uuid
import os
from typing import Optional
import logging
import hmac      # Built-in
import io        # Built-in
import base64    # Built-in
import hashlib   # Built-in
import qrcode
import time
from datetime import date, datetime, timedelta, timezone

# Add this to your Flask app setup
logging.basicConfig(
    level=logging.DEBUG,  # Change to INFO for production
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # Also print to console
    ]
)
logger = logging.getLogger(__name__)


# ============================================================================
# FLASK APP INITIALIZATION
# ============================================================================

app = Flask(__name__)

app.config['SQLALCHEMY_DATABASE_URI'] = "postgresql://kidplay_render_database_8_user:YozEdj5pE0zkZSKxajPhNV8I1M1mR7ov@dpg-dapbsvid0e5s73fae0mg-a.frankfurt-postgres.render.com/kidplay_render_database_8"

app.config['SECRET_KEY'] = 'a8f4c2e1b5d6f7a8c9e0d1f2b3a4c5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2'


# ============================================================================
# DATABASE & EXTENSIONS
# ============================================================================

db = SQLAlchemy(app)
migrate = Migrate(app, db)  # 2️⃣ migrate second, now db exists
bcrypt = Bcrypt()


# ============================================================================
# SOCKETIO INITIALIZATION
# ============================================================================

# ✅ UPDATE Socket.IO INITIALIZATION
socketio = SocketIO(
    app,
    cors_allowed_origins="*",  # Update to specific domain in production
    async_mode='threading'      # Use 'gevent' for production
)


# ============================================================================
# CONFIGURATION & CONSTANTS
# ============================================================================
SECRET_KEY = app.config['SECRET_KEY'].encode()

def _derive_key(purpose: str) -> bytes:
    """Derive a purpose-specific subkey so each use is cryptographically isolated."""
    return hmac.new(SECRET_KEY, purpose.encode(), hashlib.sha256).digest()

QR_KEY = _derive_key("qr_signing")   # isolated subkey, no separate env var needed
WINDOW_SECONDS = 15


# ============================================================================
# GLOBAL STATE
# ============================================================================
active_connections = {}  # Format: { user_id: sid, ... }
active_qr_subscriptions = {}  # ticket_uid -> [sids]
map_viewers = {}  # user_id -> sid
qr_refresh_thread = None  # Will be assigned after function is defined


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

    id = db.Column(db.Integer,primary_key=True,autoincrement=True)
    email = db.Column(db.String(200),unique=True,nullable=False,index=True)
    password_hash = db.Column(db.String(255),nullable=False)
    created_at = db.Column(db.DateTime,default=lambda: datetime.now(timezone.utc),nullable=False)
    
    # One authentication account -> one parent profile
    parent_profile = db.relationship('ParentsProfile',back_populates='user',uselist=False,cascade='all, delete-orphan')
    # User/account-level activity
    attendances = db.relationship('Attendance',back_populates='user')
    checkins = db.relationship('CheckIn',back_populates='user')


class ParentsProfile(db.Model):
    __tablename__ = 'parents_profile'

    id = db.Column(db.Integer,primary_key=True,autoincrement=True)

    # Only ParentsProfile points to User
    parents_id = db.Column(db.Integer,db.ForeignKey('user_credentials.id',ondelete='CASCADE'),nullable=False,unique=True,index=True)

    gender = db.Column(db.Enum(GenderEnum))
    first_name = db.Column(db.String(100))
    last_name = db.Column(db.String(100))
    date_of_birth = db.Column(db.Date)
    phone_number = db.Column(db.String(20))

    created_at = db.Column(db.DateTime,default=lambda: datetime.now(timezone.utc))

    updated_at = db.Column(db.DateTime,onupdate=lambda: datetime.now(timezone.utc))

    # Authentication account
    user = db.relationship('User',back_populates='parent_profile')

    # Parent -> children
    kids = db.relationship('KidsProfile',back_populates='parent',cascade='all, delete-orphan',lazy=True)

    # Parent -> parent images
    images = db.relationship('ParentsProfileImages',back_populates='parent_profile',cascade='all, delete-orphan',lazy=True)


class ParentsProfileImages(db.Model):
    __tablename__ = 'parents_profile_images'

    id = db.Column(db.Integer,primary_key=True,autoincrement=True)
    parent_profile_id = db.Column(db.Integer,db.ForeignKey('parents_profile.id',ondelete='CASCADE'),nullable=False,index=True)
    image_url = db.Column(db.String(500),nullable=False)
    created_at = db.Column(db.DateTime,default=lambda: datetime.now(timezone.utc))

    parent_profile = db.relationship('ParentsProfile',back_populates='images')


class KidsProfile(db.Model):
    __tablename__ = 'kids_profile'

    id = db.Column(db.Integer,primary_key=True,autoincrement=True)
    parent_profile_id = db.Column(db.Integer,db.ForeignKey('parents_profile.id',ondelete='CASCADE'),nullable=False,index=True)
    gender = db.Column(db.Enum(ChildEnum))
    name = db.Column(db.String(150))
    date_of_birth = db.Column(db.Date)
    bio = db.Column(db.Text)
    grade_level = db.Column(db.String(100),nullable=True)
    hobbies = db.Column(db.ARRAY(db.String),nullable=True)
    allergies = db.Column(db.ARRAY(db.String),nullable=True)
    individual_needs = db.Column(db.ARRAY(db.String),nullable=True)
    created_at = db.Column(db.DateTime,default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime,onupdate=lambda: datetime.now(timezone.utc))

    parent = db.relationship('ParentsProfile',back_populates='kids')
    image = db.relationship('KidsProfileImages',back_populates='kid',uselist=False,cascade='all, delete-orphan')


class KidsProfileImages(db.Model):
    __tablename__ = 'kids_profile_images'

    id = db.Column(db.Integer,primary_key=True,autoincrement=True)
    kids_profile_id = db.Column(db.Integer,db.ForeignKey('kids_profile.id',ondelete='CASCADE'),nullable=False,unique=True,index=True)
    image_url = db.Column(db.String(500),nullable=False)
    created_at = db.Column(db.DateTime,default=lambda: datetime.now(timezone.utc))

    kid = db.relationship('KidsProfile',back_populates='image')


class OrganizerVerificationStatus(enum.Enum):
    pending  = 'pending'   # applied, waiting for review
    approved = 'approved'  # verified, can create events
    rejected = 'rejected'  # denied, cannot create events    


# Observe!! total events and participants are calculated properties, not stored in DB. This is to avoid denormalization issues and ensure real-time accuracy.
class EventOrganizer(db.Model):
    """Created when a user chooses to become an organizer/Host. One-to-one with User."""
    __tablename__ = 'event_organizers'

    id      = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user_credentials.id', ondelete='SET NULL'), nullable=True, unique=True)
    verified_at = db.Column(db.DateTime, nullable=True)  # set only when status → approved
    name    = db.Column(db.String(100), unique=True, nullable=False)

    organizer_bio           = db.Column(db.Text, nullable=True)
    avatar_url              = db.Column(db.String(500), nullable=True)  # Profile picture
    top_event_hashtags = db.Column(db.ARRAY(db.String), nullable=True)

    verification_status = db.Column(db.Enum(OrganizerVerificationStatus),default=OrganizerVerificationStatus.pending,nullable=False)

    # Relationships
    owner  = db.relationship('User', backref=db.backref('event_organizer', uselist=False))
    images = db.relationship('EventOrganizerImage', back_populates='organizer', cascade='all, delete-orphan', order_by='EventOrganizerImage.display_order')
    events = db.relationship('EventLocation', back_populates='event_organizer')

    # ── Derived from ParentsProfile via owner ─────────────────────────────

    @property
    def _user_profile(self):
        return self.owner.parent_profile  # ✅ Matches User model's relationship

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
            .filter(EventLocation.event_organizer_id == self.id)
            .count()
        )

    @property
    def is_approved(self):
        return self.verification_status == OrganizerVerificationStatus.approved


class EventOrganizerImage(db.Model):
    """
    Portfolio images the organizer chooses to display on their profile (max 3).
    General portfolio images not tied to a specific event.
    """
    __tablename__ = 'event_organizer_images'
 
    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    organizer_id  = db.Column(db.Integer, db.ForeignKey('event_organizers.id', ondelete='CASCADE'), nullable=False)
    
    image_url     = db.Column(db.String(500), nullable=False)
    display_order = db.Column(db.Integer, default=0)
    uploaded_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
 
    organizer = db.relationship('EventOrganizer', back_populates='images')


class EventCategory(db.Model):
    __tablename__ = 'event_categories'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), unique=True, nullable=False)


class EventCoordinates(db.Model):
    """The physical place. Reusable across events."""
    __tablename__ = 'event_coordinates'

    id        = db.Column(db.Integer, primary_key=True, autoincrement=True)
    address   = db.Column(db.String(300), nullable=True)
    latitude  = db.Column(db.Float)
    longitude = db.Column(db.Float)

    # Relationships
    events = db.relationship('EventLocation', back_populates='event_coordinates')


# ✅ NEW: EventCoverImage (if you want event-specific cover images)
class EventCoverImage(db.Model):
    """
    Cover/hero image for a specific event.
    One image per event.
    """
    __tablename__ = 'event_cover_images'

    id = db.Column(db.Integer,primary_key=True,autoincrement=True)
    event_id = db.Column(db.Integer,db.ForeignKey('event_locations.id', ondelete='CASCADE'),nullable=False,unique=True)
    image_url = db.Column(db.String(500),nullable=False)
    uploaded_at = db.Column(db.DateTime(timezone=True),default=lambda: datetime.now(timezone.utc))
    event = db.relationship('EventLocation',back_populates='cover_image')


class EventLocationImage(db.Model):
    """
    Portfolio/gallery images for a specific event (max 10 per event).
    These are displayed in the event gallery when viewing event details.
    """
    __tablename__ = 'event_location_images'
 
    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    event_id      = db.Column(db.Integer, db.ForeignKey('event_locations.id', ondelete='CASCADE'), nullable=False)
    
    image_url     = db.Column(db.String(500), nullable=False)
    display_order = db.Column(db.Integer, default=0)  # For ordering images in gallery
    uploaded_at   = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
 
    # Relationship
    event = db.relationship('EventLocation', back_populates='images')
    
    @validates('image_url')
    def validate_image_count(self, key, value):
        """Check if event already has 10 images"""
        if self.event_id:
            count = EventLocationImage.query.filter_by(event_id=self.event_id).count()
            if count >= 10:
                raise ValueError("Maximum 10 images per event")


class EventLocation(db.Model):
    """One specific event instance at a event coordinates."""
    __tablename__ = 'event_locations'
 
    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    eventcoordinates_id          = db.Column(db.Integer, db.ForeignKey('event_coordinates.id'), nullable=False)
    event_category_id = db.Column(db.Integer, db.ForeignKey('event_categories.id'), nullable=False)
    event_organizer_id = db.Column(db.Integer, db.ForeignKey('event_organizers.id'), nullable=False)
 
    # Event config
    event_name      = db.Column(db.String(300), nullable=False)
    start_time    = db.Column(db.DateTime(timezone=True), nullable=False)
    duration_minutes = db.Column(db.Integer, nullable=True)  # Duration in minutes (e.g., 60 for 1h, 80 for 1h 20min). NULL = undecided/open-ended
    event_description   = db.Column(db.String(500))
    max_attendees = db.Column(db.Integer, nullable=False)
    girls_attendees = db.Column(db.Integer, nullable=True)
    boys_attendees  = db.Column(db.Integer, nullable=True)
    
    # Age range ─────────────────────────────────────────────────────────────
    min_age       = db.Column(db.Integer, nullable=False, default=1)  # Minimum age requirement
    max_age       = db.Column(db.Integer, nullable=True, default=18)  # Maximum age requirement
    
    base_price    = db.Column(db.Numeric(10, 2))
    currency      = db.Column(db.String(10), default='SEK', nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))
 
    # Operational state
    is_checkin_closed = db.Column(db.Boolean, default=False, nullable=False)
 
    # ── Relationships ──────────────────────────────────────────────────────────
    
    event_coordinates   = db.relationship('EventCoordinates', back_populates='events')
    event_category      = db.relationship('EventCategory', lazy='selectin')
    event_organizer     = db.relationship('EventOrganizer', back_populates='events', lazy='selectin')
    
    # Images
    cover_image         = db.relationship('EventCoverImage', uselist=False, back_populates='event', cascade='all, delete-orphan')
    images              = db.relationship('EventLocationImage', back_populates='event', lazy=True, cascade='all, delete-orphan', order_by='EventLocationImage.display_order')
    
    # Event data
    attendances         = db.relationship('Attendance', back_populates='location', lazy=True, cascade='all, delete-orphan')
    checkins            = db.relationship('CheckIn', back_populates='location', lazy=True, cascade='all, delete-orphan')
    transactions        = db.relationship('EventTransaction', back_populates='event', lazy=True, cascade='all, delete-orphan')
    conversations       = db.relationship('Conversation', foreign_keys='Conversation.event_id', lazy=True, overlaps="event")
    
    # ── Validators ─────────────────────────────────────────────────────────────
 
    @validates('duration_minutes')
    def validate_duration(self, key, value):
        if value is not None and value <= 0:
            raise ValueError("duration_minutes must be positive (or NULL for undecided)")
        return value
 
    @validates('girls_attendees', 'boys_attendees')
    def validate_gender_limits(self, key, value):
        if value is not None and value < 0:
            raise ValueError(f"{key} cannot be negative")
        return value
    
    
    @validates('min_age')
    def validate_min_age(self, key, value):
        if value is not None and value < 0:
            raise ValueError("min_age cannot be negative")
        return value
    
    @validates('max_age')
    def validate_max_age(self, key, value):
        if value is not None and value < 0:
            raise ValueError("max_age cannot be negative")
        if value is not None and self.min_age and value < self.min_age:
            raise ValueError("max_age cannot be less than min_age")
        return value
    
 
    def validate_attendee_totals(self):
        validate_attendee_totals(self.max_attendees, self.girls_attendees, self.boys_attendees)
 
    # ── State properties ───────────────────────────────────────────────────────
    
    @property
    def age_range(self):
        """Format age range as '1 - 18' or '1+' if no max age"""
        if self.max_age is None:
            return f"{self.min_age}+"
        return f"{self.min_age} - {self.max_age}"
    
 
    @property
    def end_time(self):
        """Calculate end time from start_time and duration_minutes. Returns None if duration is undecided."""
        if self.duration_minutes is None:
            return None
        from datetime import timedelta
        return self.start_time + timedelta(minutes=self.duration_minutes)
 
    @property
    def is_ongoing(self):
        now = datetime.now(timezone.utc)
        if self.end_time is None:
            # Undecided duration: event is ongoing if it has started
            return self.start_time <= now
        return self.start_time <= now <= self.end_time
 
    @property
    def is_past(self):
        if self.end_time is None:
            # Undecided duration: treat as never truly "past" (people can still be there)
            return False
        return datetime.now(timezone.utc) > self.end_time
 
    @property
    def is_upcoming(self):
        return datetime.now(timezone.utc) < self.start_time
    
    @property
    def total_participants(self):
        """Total number of attendees registered for this event"""
        return Attendance.query.filter_by(location_id=self.id).count()
 
    # ── Gender counting ────────────────────────────────────────────────────────
 
    def _count_by_gender(self, gender: GenderEnum) -> int:
        return (
            Attendance.query
            .join(User, User.id == Attendance.parent_id)
            .join(ParentsProfile, ParentsProfile.parents_id == User.id)
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
            user_id=self.attendance.parent_id, 
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
    parent_id   = db.Column(db.Integer, db.ForeignKey('user_credentials.id', ondelete='CASCADE'), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey('event_locations.id', ondelete='CASCADE'), nullable=False)
    timestamp   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user     = db.relationship('User', back_populates='attendances')
    location = db.relationship('EventLocation', back_populates='attendances')
    ticket   = db.relationship('Ticket', back_populates='attendance', uselist=False)

    __table_args__ = (
        db.UniqueConstraint('parent_id', 'location_id', name='unique_parent_location_attendance'),
    )


class Conversation(db.Model):
    """
    Groups messages between two users, optionally for a specific event.
    
    'user_id' is the primary perspective holder.
    'other_user_id' is the conversation partner.
    """
    __tablename__ = 'conversations'
    
    id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    parent_id       = db.Column(db.Integer, db.ForeignKey('user_credentials.id'), nullable=False)
    other_user_id   = db.Column(db.Integer, db.ForeignKey('user_credentials.id'), nullable=False)
    event_id        = db.Column(db.Integer, db.ForeignKey('event_locations.id'), nullable=True)
    created_at      = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at      = db.Column(db.DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    messages        = db.relationship('Message', back_populates='conversation', cascade='all, delete-orphan')
    event           = db.relationship('EventLocation', foreign_keys=[event_id])
    parent          = db.relationship('User', foreign_keys=[parent_id])
    other_user      = db.relationship('User', foreign_keys=[other_user_id])
    
    @property
    def latest_message(self) -> Optional['Message']:
        """Get the most recent message in this conversation."""
        return Message.query.filter_by(conversation_id=self.id).order_by(Message.timestamp.desc()).first()
    
    @property
    def unread_count(self) -> int:
        """
        Count unread messages from OTHER user's perspective.
        Returns count of messages where user_id is the receiver and is_read=False.
        """
        return Message.query.filter(
            Message.conversation_id == self.id,
            Message.receiver_id == self.parent_id,  # Messages received BY parent_id
            Message.is_read == False
        ).count()
    
    def get_unread_count_for_user(self, user_id: int) -> int:
        """
        Get unread message count for a specific user in this conversation.
        
        Args:
            user_id: The user to get unread count for
            
        Returns:
            Number of unread messages where user_id is the receiver
        """
        return Message.query.filter(
            Message.conversation_id == self.id,
            Message.receiver_id == user_id,
            Message.is_read == False
        ).count()


class Message(db.Model):
    """Individual messages within a conversation."""
    __tablename__ = 'chat_messages'

    id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversations.id'), nullable=False)
    sender_id       = db.Column(db.Integer, db.ForeignKey('user_credentials.id'), nullable=False)
    receiver_id     = db.Column(db.Integer, db.ForeignKey('user_credentials.id'), nullable=False)
    message         = db.Column(db.Text, nullable=False)
    reply_to_id     = db.Column(db.Integer, db.ForeignKey('chat_messages.id'), nullable=True)
    timestamp       = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    image_url       = db.Column(db.String(), nullable=True)
    is_read         = db.Column(db.Boolean, default=False, nullable=False)

    # Relationships
    conversation    = db.relationship('Conversation', back_populates='messages')
    sender          = db.relationship('User', foreign_keys=[sender_id], backref=db.backref('sent_messages', lazy=True))
    receiver        = db.relationship('User', foreign_keys=[receiver_id], backref=db.backref('received_messages', lazy=True))
    reply_to        = db.relationship('Message', remote_side=[id], backref=db.backref('replies', lazy=True))

    @property
    def time_ago(self) -> str:
        """Returns human-readable string like '32 min ago', '2 hrs ago', 'just now'."""
        now = datetime.now(timezone.utc)
        diff = now - self.timestamp.replace(tzinfo=timezone.utc)
        seconds = int(diff.total_seconds())

        if seconds < 60:
            return "just now"
        if seconds < 3600:
            mins = seconds // 60
            return f"{mins} min ago" if mins == 1 else f"{mins} mins ago"
        if seconds < 86400:
            hrs = seconds // 3600
            return f"{hrs} hr ago" if hrs == 1 else f"{hrs} hrs ago"
        days = seconds // 86400
        return f"{days} day ago" if days == 1 else f"{days} days ago"

    def to_dict(self, include_sender=False, include_receiver=False):
        """Serialize message for API responses"""
        data = {
            'id': self.id,
            'conversationId': self.conversation_id,
            'senderId': self.sender_id,
            'receiverId': self.receiver_id,
            'message': self.message,
            'replyToId': self.reply_to_id,
            'timestamp': self.timestamp.isoformat(),
            'imageUrl': self.image_url,
            'isRead': self.is_read,
            'timeAgo': self.time_ago,
        }
        
        if include_sender:
            data['sender'] = self.sender.to_dict()
        if include_receiver:
            data['receiver'] = self.receiver.to_dict()
        
        return data


class EventOrganizerPaymentDetails(db.Model):
    __tablename__ = 'event_organizer_payment_details'

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    event_organizer_id = db.Column(db.Integer, db.ForeignKey('event_organizers.id', ondelete='CASCADE'), nullable=False, unique=True)

    organisation_number = db.Column(db.String(11), nullable=True)   # XXXXXX-XXXX Swedish format
    swish_number        = db.Column(db.String(11), nullable=False)   # 10 digits for Swish för företag

    swish_verified = db.Column(db.Boolean, default=False, nullable=False)  # must be True before first payout

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    event_organizer = db.relationship('EventOrganizer', backref=db.backref('payment_details', uselist=False))


class TransactionStatus(enum.Enum):
    pending      = 'pending'      # awaiting payment processing
    completed    = 'completed'    # payment successful
    failed       = 'failed'       # payment declined/failed
    refunded     = 'refunded'     # refund processed
    cancelled    = 'cancelled'    # user or admin cancelled


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
    event_organizer_id = db.Column(db.Integer, db.ForeignKey('event_organizers.id', ondelete='RESTRICT'), nullable=False)

    gross_amount  = db.Column(db.Numeric(10, 2), nullable=False)
    platform_fee  = db.Column(db.Numeric(10, 2), nullable=False)
    payout_amount = db.Column(db.Numeric(10, 2), nullable=False)

    currency        = db.Column(db.String(10), default='SEK', nullable=False)
    swish_reference = db.Column(db.String(100), unique=True, nullable=False)
    status          = db.Column(db.Enum(PayoutStatus), default=PayoutStatus.processing, nullable=False)

    initiated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = db.Column(db.DateTime, nullable=True)

    event      = db.relationship('EventLocation')
    event_organizer = db.relationship('EventOrganizer')
    

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


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


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


def decode_token(token):
    """Extract JWT decoding logic from your existing function"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        return payload.get('user_id')
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def initiate_payout(event_id, organizer_id):
    payment_details = EventOrganizerPaymentDetails.query.filter_by(event_organizer_id=organizer_id).first()

    if not payment_details:
        raise ValueError("Organizer has no payment details on file.")

    if not payment_details.swish_verified:
        raise ValueError("Organizer Swish number is not verified. Cannot initiate payout.")

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


def _current_window(offset: int = 0) -> int:
    """Returns the current 15-second time window index."""
    return int(time.time() // WINDOW_SECONDS) + offset


def generate_rotating_token(ticket_uid: str) -> str:
    """HMAC-signed token valid for the current 15-second window."""
    window = _current_window()
    message = f"{ticket_uid}:{window}".encode()
    signature = hmac.new(QR_KEY, message, hashlib.sha256).hexdigest()
    payload = f"{ticket_uid}:{window}:{signature}"
    return base64.urlsafe_b64encode(payload.encode()).decode()


def verify_rotating_token(token: str) -> tuple[bool, str | None]:
    """
    Returns (is_valid, ticket_uid).
    Accepts current window and ±1 for clock drift.
    """
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        parts = decoded.split(":")          # UUID4 has hyphens, not colons — safe to split on ":"
        ticket_uid = parts[0]
        window     = int(parts[1])
        signature  = parts[2]
    except Exception:
        return False, None

    for offset in [0, -1, 1]:
        expected_window = _current_window(offset)
        if window == expected_window:
            message      = f"{ticket_uid}:{window}".encode()
            expected_sig = hmac.new(QR_KEY, message, hashlib.sha256).hexdigest()
            if hmac.compare_digest(signature, expected_sig):
                return True, ticket_uid

    return False, None


def send_qr_to_ticket(ticket_uid: str):
    """
    Generate new token and push to subscribers.
    ✅ Only the token is sent - QR code rendering is done by clients.
    """
    try:
        ticket = Ticket.query.filter_by(ticket_uid=ticket_uid).first()
        if not ticket or ticket.is_expired or ticket.status == 'cancelled':
            app.logger.warning(f"Cannot send QR: ticket {ticket_uid} is inactive")
            room = f"ticket:{ticket_uid}"
            socketio.emit('qr/ticket_inactive', {
                'ticket_uid': ticket_uid,
                'reason': 'expired' if ticket and ticket.is_expired else 'cancelled'
            }, room=room)
            return
        
        # Generate new token (not QR bitmap)
        token = generate_rotating_token(ticket.ticket_uid)
        
        # Calculate time until expiry
        now = time.time()
        current_window_start = int(now // WINDOW_SECONDS) * WINDOW_SECONDS
        expires_in_ms = int((current_window_start + WINDOW_SECONDS - now) * 1000)
        
        # ✅ Send only token - frontend generates QR bitmap
        room = f"ticket:{ticket_uid}"
        socketio.emit('qr/update', {
            'token': token,
            'expires_in_ms': expires_in_ms,
            'window_seconds': WINDOW_SECONDS,
            'generated_at': now
        }, room=room)
        
        app.logger.info(f"Sent QR token to {ticket_uid} (expires in {expires_in_ms}ms)")
        
    except Exception as e:
        app.logger.exception(f"Error sending QR to {ticket_uid}: {e}")


def refresh_qr_codes_background():
    """
    Background task: Refresh all active QR codes before they expire.
    
    Window is 15 seconds, so we refresh every ~10 seconds to be safe.
    This gives clients 5 seconds buffer before the current token expires.
    """
    print(f"\n{'='*60}")
    print(f"🔄 QR Refresh Background Task Started")
    print(f"{'='*60}\n")
    
    while True:
        try:
            time.sleep(10)  # Refresh every 10 seconds (15s window - 5s buffer)
            
            if not active_qr_subscriptions:
                # No active subscriptions, skip this cycle
                continue
            
            print(f"🔄 Refreshing {len(active_qr_subscriptions)} active QR codes...")
            
            # Refresh each ticket that has subscribers
            for ticket_uid in list(active_qr_subscriptions.keys()):
                send_qr_to_ticket(ticket_uid)
            
            print(f"✅ Refresh cycle complete\n")
            
        except Exception as e:
            app.logger.exception(f"Error in QR refresh thread: {e}")
            print(f"❌ Error in refresh cycle: {e}\n")



# ─────────────────────────────────────────────────────────────────────────────
# SOCKET.IO EVENT HANDLERS - CONNECTION
# ─────────────────────────────────────────────────────────────────────────────


@socketio.on('connect')
def handle_connect():
    """
    Handle user connection to Socket.IO server.
    Authenticates user via JWT token and tracks connection.
    """
    print(f"\n{'='*60}")
    print(f"🔗 NEW CONNECTION REQUEST")
    print(f"{'='*60}")
    
    try:
        # Get token from query params or headers
        token = request.args.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
        print(f"🔑 Token received: {token[:20]}..." if token else "❌ No token provided")
        
        if not token:
            print(f"❌ REJECTED: No authentication token")
            return False
        
        # Decode token to get user
        try:
            decoded = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            user_id = decoded.get('user_id')
            print(f"✅ Token decoded successfully, user_id: {user_id}")
        except jwt.InvalidTokenError as e:
            print(f"❌ REJECTED: Invalid token - {e}")
            return False
        
        # Get user from database
        user = User.query.get(user_id)
        if not user:
            print(f"❌ REJECTED: User not found (id: {user_id})")
            return False
        
        # Track this connection
        sid = request.sid
        active_connections[user_id] = sid
        print(f"✅ ACCEPTED: User {user_id} connected (sid: {sid})")
        print(f"📊 Active connections: {len(active_connections)}")
        print(f"{'='*60}\n")
        
        # Emit confirmation to client
        emit('connection_response', {
            'status': 'connected',
            'userId': user_id,
            'message': f'Successfully connected as user {user_id}'
        })
        
    except Exception as e:
        print(f"❌ ERROR during connect: {e}")
        import traceback
        traceback.print_exc()
        return False


@socketio.on('disconnect')
def handle_disconnect():
    """Handle user disconnection from Socket.IO server."""
    try:
        sid = request.sid
        print(f"\n{'='*60}")
        print(f"🔌 USER DISCONNECTED")
        print(f"{'='*60}")
        
        # Find which user this sid belongs to
        disconnected_user = None
        for user_id, user_sid in list(active_connections.items()):
            if user_sid == sid:
                disconnected_user = user_id
                break
        
        if disconnected_user:
            del active_connections[disconnected_user]
            
            # Remove from map viewers if they were viewing map
            if disconnected_user in map_viewers:
                del map_viewers[disconnected_user]
                leave_room('map')
            
            print(f"✅ User {disconnected_user} disconnected (sid: {sid})")
            print(f"📊 Active connections: {len(active_connections)}")
        else:
            print(f"⚠️ Unknown session {sid} disconnected")
        
        # ✅ NEW: Also remove from all QR subscriptions
        for ticket_uid in list(active_qr_subscriptions.keys()):
            if sid in active_qr_subscriptions[ticket_uid]:
                active_qr_subscriptions[ticket_uid].remove(sid)
                print(f"✅ Removed {sid} from QR subscriptions for {ticket_uid}")
                
                # Clean up empty subscriptions
                if not active_qr_subscriptions[ticket_uid]:
                    del active_qr_subscriptions[ticket_uid]
                    print(f"🧹 No more subscribers for {ticket_uid}, cleaned up")
        
        print(f"📊 Total active tickets: {len(active_qr_subscriptions)}")
        print(f"{'='*60}\n")
        
    except Exception as e:
        app.logger.exception(f"ERROR in handle_disconnect: {e}")
        import traceback
        traceback.print_exc()
        

# ─────────────────────────────────────────────────────────────────────────────
# SOCKET.IO EVENT HANDLERS - MESSAGING
# ─────────────────────────────────────────────────────────────────────────────


@socketio.on('send_message')
def handle_send_message(data):
    """
    Handle real-time message sending via Socket.IO.
    Saves to database and emits to recipient.
    """
    print(f"\n{'='*60}")
    print(f"💬 MESSAGE SEND REQUEST")
    print(f"{'='*60}")
    
    try:
        # Get current user from active connections
        sid = request.sid
        current_user_id = None
        for user_id, user_sid in active_connections.items():
            if user_sid == sid:
                current_user_id = user_id
                break
        
        if not current_user_id:
            print(f"❌ FAILED: Unknown sender (sid: {sid})")
            emit('error', {'message': 'Unauthorized - connection not authenticated'})
            return
        
        print(f"👤 Sender: {current_user_id}")
        
        # Parse message data
        conversation_id = data.get('conversationId')
        receiver_id = data.get('receiverId')
        message_text = data.get('message')
        reply_to_id = data.get('replyToId')
        image_url = data.get('imageUrl')
        
        print(f"📝 Data: convo={conversation_id}, receiver={receiver_id}, msg_len={len(message_text) if message_text else 0}")
        
        # Validate required fields
        if not conversation_id or not receiver_id or not message_text:
            print(f"❌ VALIDATION FAILED: Missing required fields")
            emit('error', {'message': 'conversationId, receiverId, and message are required'})
            return
        
        # Verify conversation exists and user is part of it
        conversation = Conversation.query.get(conversation_id)
        if not conversation:
            print(f"❌ FAILED: Conversation {conversation_id} not found")
            emit('error', {'message': 'Conversation not found'})
            return
        
        if (conversation.user_id != current_user_id and 
            conversation.other_user_id != current_user_id):
            print(f"❌ FAILED: User {current_user_id} not part of conversation")
            emit('error', {'message': 'Unauthorized - not part of this conversation'})
            return
        
        # Create message in database
        message = Message(
            conversation_id=conversation_id,
            sender_id=current_user_id,
            receiver_id=receiver_id,
            message=message_text,
            reply_to_id=reply_to_id,
            image_url=image_url,
        )
        
        db.session.add(message)
        db.session.commit()
        
        print(f"✅ Message {message.id} saved to database")
        
        # Build message response object
        message_response = {
            'id': message.id,
            'conversationId': conversation_id,
            'senderId': current_user_id,
            'receiverId': receiver_id,
            'message': message_text,
            'imageUrl': image_url,
            'replyToId': reply_to_id,
            'timestamp': message.timestamp.isoformat(),
            'isRead': False
        }
        
        # Emit to sender (confirmation)
        emit('message_sent', message_response)
        print(f"✅ Sent confirmation to sender")
        
        # Emit to receiver if they're online
        if receiver_id in active_connections:
            receiver_sid = active_connections[receiver_id]
            print(f"📤 Receiver {receiver_id} is online (sid: {receiver_sid})")
            
            socketio.emit('new_message', message_response, room=receiver_sid)
            print(f"✅ Emitted new_message to receiver")
        else:
            print(f"⚠️  Receiver {receiver_id} is offline (message saved)")
        
        print(f"{'='*60}\n")
        
    except Exception as e:
        print(f"❌ ERROR in handle_send_message: {e}")
        import traceback
        traceback.print_exc()
        emit('error', {'message': f'Error sending message: {str(e)}'})
        db.session.rollback()
 
 
@socketio.on('mark_as_read')
def handle_mark_as_read(data):
    """Mark a message as read."""
    print(f"\n{'='*60}")
    print(f"📖 MARK AS READ REQUEST")
    print(f"{'='*60}")
    
    try:
        # Get current user from active connections
        sid = request.sid
        current_user_id = None
        for user_id, user_sid in active_connections.items():
            if user_sid == sid:
                current_user_id = user_id
                break
        
        if not current_user_id:
            print(f"❌ FAILED: Unknown user")
            return
        
        message_id = data.get('messageId')
        conversation_id = data.get('conversationId')
        
        print(f"👤 User: {current_user_id}")
        print(f"📝 Message: {message_id}, Conversation: {conversation_id}")
        
        # Get message
        message = Message.query.get(message_id)
        if not message:
            print(f"❌ Message not found")
            return
        
        # Verify user is the receiver
        if message.receiver_id != current_user_id:
            print(f"❌ User is not the receiver of this message")
            return
        
        # Mark as read
        message.is_read = True
        db.session.commit()
        
        print(f"✅ Message marked as read")
        
        # Notify sender that message was read
        if message.sender_id in active_connections:
            sender_sid = active_connections[message.sender_id]
            socketio.emit('message_read', {
                'messageId': message_id,
                'conversationId': conversation_id,
                'readBy': current_user_id,
                'readAt': datetime.utcnow().isoformat()
            }, room=sender_sid)
            print(f"✅ Notified sender that message was read")
        
        print(f"{'='*60}\n")
        
    except Exception as e:
        print(f"❌ ERROR in handle_mark_as_read: {e}")
        import traceback
        traceback.print_exc()
 
 
@socketio.on('user_typing')
def handle_user_typing(data):
    """Broadcast that a user is typing."""
    try:
        sid = request.sid
        current_user_id = None
        for user_id, user_sid in active_connections.items():
            if user_sid == sid:
                current_user_id = user_id
                break
        
        if not current_user_id:
            return
        
        receiver_id = data.get('receiverId')
        conversation_id = data.get('conversationId')
        
        # Notify receiver if online
        if receiver_id in active_connections:
            receiver_sid = active_connections[receiver_id]
            socketio.emit('user_is_typing', {
                'conversationId': conversation_id,
                'typingUserId': current_user_id
            }, room=receiver_sid)
        
    except Exception as e:
        print(f"ERROR in handle_user_typing: {e}")
 
 
@socketio.on('user_stopped_typing')
def handle_user_stopped_typing(data):
    """Broadcast that a user stopped typing."""
    try:
        sid = request.sid
        current_user_id = None
        for user_id, user_sid in active_connections.items():
            if user_sid == sid:
                current_user_id = user_id
                break
        
        if not current_user_id:
            return
        
        receiver_id = data.get('receiverId')
        conversation_id = data.get('conversationId')
        
        # Notify receiver if online
        if receiver_id in active_connections:
            receiver_sid = active_connections[receiver_id]
            socketio.emit('user_stopped_typing', {
                'conversationId': conversation_id,
                'typingUserId': current_user_id
            }, room=receiver_sid)
        
    except Exception as e:
        print(f"ERROR in handle_user_stopped_typing: {e}")
 
     
# ─────────────────────────────────────────────────────────────────────────────
# SOCKET.IO EVENT HANDLERS - MAPS & EVENTS
# ─────────────────────────────────────────────────────────────────────────────


@socketio.on('join_map')
def handle_join_map(data):
    """
    User is viewing the map. Add them to the map room.
    They'll receive real-time event updates.
    """
    print(f"\n{'='*60}")
    print(f"🗺️  USER JOINED MAP")
    print(f"{'='*60}")
    
    try:
        sid = request.sid
        current_user_id = None
        
        # Find current user from active connections
        for user_id, user_sid in active_connections.items():
            if user_sid == sid:
                current_user_id = user_id
                break
        
        if not current_user_id:
            print(f"❌ FAILED: Unknown user")
            emit('error', {'message': 'Unauthorized'})
            return  # ← ADD THIS: Return early
        
        # Add user to map viewers
        map_viewers[current_user_id] = sid
        join_room('map')
        
        print(f"✅ User {current_user_id} joined map room")
        print(f"📊 Active map viewers: {len(map_viewers)}")
        print(f"{'='*60}\n")
        
        # ✅ EMIT SUCCESS RESPONSE
        emit('map_joined', {
            'status': 'connected_to_map',
            'userId': current_user_id
        })
        
    except Exception as e:
        print(f"❌ ERROR in handle_join_map: {e}")
        import traceback
        traceback.print_exc()
        emit('error', {'message': str(e)})  # ← Send error to client


@socketio.on('leave_map')
def handle_leave_map(data):  # ← Add this parameter
    """
    User is leaving the map. Remove them from the map room.
    """
    print(f"\n{'='*60}")
    print(f"🗺️  USER LEFT MAP")
    print(f"{'='*60}")
    
    try:
        sid = request.sid
        current_user_id = None
        
        # Find current user
        for user_id, user_sid in active_connections.items():
            if user_sid == sid:
                current_user_id = user_id
                break
        
        if current_user_id and current_user_id in map_viewers:
            del map_viewers[current_user_id]
            leave_room('map')
            print(f"✅ User {current_user_id} left map room")
            print(f"📊 Active map viewers: {len(map_viewers)}")
        
        print(f"{'='*60}\n")
        
    except Exception as e:
        print(f"❌ ERROR in handle_leave_map: {e}")
        import traceback
        traceback.print_exc()


def broadcast_event_to_map(event_coordinates):
    """
    Tier 1: ULTRA-LIGHTWEIGHT
    Only coordinates - no event details.
    Full details loaded on-demand via REST API.
    """
    try:
        event_payload = {
            'id': event_coordinates.id,
            'title': event_coordinates.name,
            'event_name': event_coordinates.name,
            'coordinate': {
                'latitude': float(event_coordinates.latitude),
                'longitude': float(event_coordinates.longitude),
            },
            'address': event_coordinates.address or "",
        }
        
        socketio.emit('new_event_on_map', event_payload, room='map')
        print(f"✅ Broadcasted marker {event_coordinates.id}")
        
    except Exception as e:
        print(f"❌ ERROR broadcasting event: {e}")
        traceback.print_exc()  # ← Log full traceback
        

# ─────────────────────────────────────────────────────────────────────────────
# SOCKET.IO QR CODE SUBSCRIPTION HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

@socketio.on('qr/subscribe')
def handle_qr_subscribe(data):
    """
    Ticket holder: "Start sending me rotating QR codes for this ticket"
    """
    print(f"\n{'='*60}")
    print(f"📲 QR SUBSCRIPTION REQUEST")
    print(f"{'='*60}")
    
    try:
        # Get auth from data or headers
        auth_token = data.get('auth_token') or request.headers.get('Authorization', '').replace('Bearer ', '')
        ticket_uid = data.get('ticket_uid')
        
        if not ticket_uid or not auth_token:
            print(f"❌ REJECTED: Missing ticket_uid or auth_token")
            emit('error', {'message': 'Missing ticket_uid or auth_token'})
            return
        
        print(f"🎟️  Ticket UID: {ticket_uid}")
        
        # Decode token to get user_id
        try:
            decoded = jwt.decode(auth_token, app.config['SECRET_KEY'], algorithms=['HS256'])
            user_id = decoded.get('user_id')
        except jwt.InvalidTokenError as e:
            print(f"❌ REJECTED: Invalid token - {e}")
            emit('error', {'message': 'Invalid auth token'})
            return
        
        print(f"👤 User ID: {user_id}")
        
        # Verify ticket exists and belongs to this user
        ticket = Ticket.query.filter_by(ticket_uid=ticket_uid).first()
        if not ticket:
            print(f"❌ REJECTED: Ticket not found")
            emit('error', {'message': 'Ticket not found'})
            return
        
        # ✅ FIXED: Use parent_id instead of user_id
        if ticket.attendance.parent_id != user_id:
            print(f"❌ REJECTED: User {user_id} does not own ticket {ticket_uid}")
            emit('error', {'message': 'Unauthorized'})
            return
        
        if ticket.is_expired or ticket.status == 'cancelled':
            print(f"❌ REJECTED: Ticket is inactive (expired={ticket.is_expired}, status={ticket.status})")
            emit('error', {'message': 'Ticket is not active'})
            return
        
        # Subscribe: join a room for this ticket
        room = f"ticket:{ticket_uid}"
        join_room(room)
        
        # Track this subscription
        if ticket_uid not in active_qr_subscriptions:
            active_qr_subscriptions[ticket_uid] = []
        active_qr_subscriptions[ticket_uid].append(request.sid)
        
        print(f"✅ ACCEPTED: User {user_id} subscribed to QR updates for {ticket_uid}")
        print(f"🔔 Active subscriptions on {ticket_uid}: {len(active_qr_subscriptions[ticket_uid])}")
        print(f"📊 Total active tickets: {len(active_qr_subscriptions)}")
        print(f"{'='*60}\n")
        
        # Immediately send current QR code (don't wait for refresh cycle)
        send_qr_to_ticket(ticket_uid)
        
        # Confirm subscription
        emit('qr/subscribed', {
            'status': 'subscribed',
            'ticket_uid': ticket_uid,
            'message': f'Subscribed to QR updates for {ticket_uid}'
        })
        
    except Exception as e:
        print(f"❌ ERROR in qr/subscribe: {e}")
        import traceback
        traceback.print_exc()
        emit('error', {'message': 'Subscription failed'})


@socketio.on('qr/unsubscribe')
def handle_qr_unsubscribe(data):
    """Ticket holder: Stop sending me QR codes"""
    ticket_uid = data.get('ticket_uid')
    
    if not ticket_uid:
        return
    
    room = f"ticket:{ticket_uid}"
    leave_room(room)
    
    # Remove from tracking
    if ticket_uid in active_qr_subscriptions:
        try:
            active_qr_subscriptions[ticket_uid].remove(request.sid)
            if not active_qr_subscriptions[ticket_uid]:
                del active_qr_subscriptions[ticket_uid]
        except ValueError:
            pass
    
    print(f"✅ User unsubscribed from {ticket_uid}")
    print(f"📊 Total active tickets: {len(active_qr_subscriptions)}")


# ============================================================================
# START BACKGROUND THREAD (after function is defined!)
# ============================================================================
qr_refresh_thread = Thread(target=refresh_qr_codes_background, daemon=True)
qr_refresh_thread.start()


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
# @app.route('/sign-in', methods=['POST'])
# def sign_in():
#     try:
#         data = request.get_json()
#         email = data.get('email')
#         password = data.get('password')  # ← Plaintext password from client

#         if not email or not password:
#             return jsonify({'error': 'Email and password are required'}), 400

#         user = User.query.filter_by(email=email).first()
#         if not user or not bcrypt.check_password_hash(user.password_hash, password):
#             return jsonify({'message': 'Invalid credentials'}), 401

#         payload = {
#             'user_id': user.id,
#             'exp': datetime.utcnow() + timedelta(days=7)
#         }
#         token = jwt.encode(payload, SECRET_KEY, algorithm='HS256')
#         if isinstance(token, bytes):
#             token = token.decode('utf-8')

#         return jsonify({'message': 'Sign in successful', 'token': token}), 200

#     except Exception as e:
#         print("Sign-in error:", e)
#         return jsonify({'error': str(e)}), 500
    
    
@app.route('/log-in', methods=['POST'])
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

        token = create_token(user)  # ✅ Use your existing function

        return jsonify({
            'message': 'Sign in successful',
            'token': token,
            'user_id': user.id  # ✅ Changed to snake_case
        }), 200

    except Exception as e:
        print("Sign-in error:", e)
        return jsonify({'error': str(e)}), 500


@app.route('/sign-up', methods=['POST'])
def sign_up():
    try:
        data = request.get_json()
        email = data.get('email')
        password = data.get('password')

        if not email or not password:
            return jsonify({'error': 'Email and password are required'}), 400

        if User.query.filter_by(email=email).first():
            return jsonify({'message': 'Email already exists'}), 400

        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        new_user = User(email=email, password_hash=hashed_password)
        db.session.add(new_user)
        db.session.commit()

        token = create_token(new_user)  # ✅ Use your existing function

        return jsonify({
            'message': 'Sign up successful',
            'token': token,
            'userId': new_user.id  # ✅ ADD THIS
        }), 200

    except Exception as e:
        print("Sign-up error:", e)
        return jsonify({'error': str(e)}), 500


@app.route('/logout', methods=['POST'])
def logout():
    """
    Logout endpoint.
    Verifies the user's token is valid.
    With stateless JWT, logout is complete once client deletes the token.
    """
    current_user = get_current_user_from_token()
    if not current_user:
        return jsonify({"error": "Unauthorized"}), 401
    
    print(f"User {current_user.id} ({current_user.email}) logged out")
    
    return jsonify({"message": "Logged out successfully"}), 200


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
            return jsonify({'message': 'Email already exists'}), 409

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

        return jsonify({
            'message': "New User added",
            'token': token,
            'userId': new_user.id  # ✅ ADD THIS
        }), 201

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

    profile = user.parent_profile
    if not profile:
        return jsonify({'error': 'Profile not found'}), 404

    return jsonify({
        'id':            profile.id,
        'email':         user.email,
        'first_name':    profile.first_name,
        'last_name':     profile.last_name,
        'date_of_birth': profile.date_of_birth.isoformat() if profile.date_of_birth else None,
        'gender':        profile.gender.value if profile.gender else None,
        'phone_number':  profile.phone_number,
        'created_at':    profile.created_at.isoformat() if profile.created_at else None,
        'updated_at':    profile.updated_at.isoformat() if profile.updated_at else None,
    }), 200
 
 
@app.route('/parents/profile', methods=['POST'])
def post_parents_profile():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    profile = user.parent_profile

    if not profile:
        profile = ParentsProfile(parents_id=user.id)
        db.session.add(profile)

    if 'first_name' in data:
        profile.first_name = data['first_name']

    if 'last_name' in data:
        profile.last_name = data['last_name']

    if 'date_of_birth' in data:
        try:
            profile.date_of_birth = date.fromisoformat(data['date_of_birth'])
        except (ValueError, TypeError):
            return jsonify({
                'error': 'Invalid date_of_birth, expected YYYY-MM-DD'
            }), 400

    if 'gender' in data:
        gender_map = {'Male': GenderEnum.Male, 'Female': GenderEnum.Female}
        val = data['gender']

        if val not in gender_map:
            return jsonify({
                'error': f'Invalid gender: {val}'
            }), 400

        profile.gender = gender_map[val]

    if 'phone_number' in data:
        profile.phone_number = data['phone_number']

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to save profile'}), 500

    return jsonify({
        'message': 'Parents profile saved',
        'id': profile.id
    }), 201
 
 
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
        parents_id=user.id,
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
 
# ─── Helper function for authorization ────────────────────────────────────
def get_current_parent():
    """Get current parent from token"""
    user = get_current_user_from_token()
    if not user:
        return None
    
    parent = ParentsProfile.query.filter_by(parents_id=user.id).first()
    return parent


# ─── Serialization helpers ────────────────────────────────────────────────
def serialize_kids_profile(profile):
    """Serialize a KidsProfile object to JSON"""
    return {
        'id': profile.id,
        'name': profile.name,
        'date_of_birth': profile.date_of_birth.isoformat() if profile.date_of_birth else None,
        'gender': profile.gender.value if profile.gender else None,
        'grade_level': profile.grade_level,
        'bio': profile.bio,
        'hobbies': profile.hobbies or [],
        'allergies': profile.allergies or [],
        'individual_needs': profile.individual_needs or [],
        'photo_url': profile.image.image_url if profile.image else None,  # ✅ Access via relationship
        'created_at': profile.created_at.isoformat() if profile.created_at else None,
        'updated_at': profile.updated_at.isoformat() if profile.updated_at else None,
    }


@app.route('/kids/profiles', methods=['GET'])
def get_kids_profiles():
    """Get all kids profiles for the current parent"""
    logger.info("Fetching all kid profiles")
    
    parent = get_current_parent()
    if not parent:
        logger.warning("Unauthorized access to get kid profiles")
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        logger.info(f"Fetching profiles for parent {parent.id}")
        
        profiles = (
            KidsProfile.query
            .filter_by(parent_profile_id=parent.id)
            .options(joinedload(KidsProfile.image))  # ✅ Eager load images
            .order_by(KidsProfile.created_at.asc())
            .all()
        )

        logger.info(f"Found {len(profiles)} profiles for parent {parent.id}")
        
        if not profiles:
            logger.info(f"No profiles found for parent {parent.id}")
            return jsonify([]), 200

        serialized = [serialize_kids_profile(p) for p in profiles]
        return jsonify(serialized), 200
        
    except Exception as e:
        logger.error(f"Error fetching profiles for parent {parent.id}: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ─── GET single kid profile ───────────────────────────────────────────────
@app.route('/kids/profiles/<int:kid_id>', methods=['GET'])
def get_kids_profile(kid_id):
    """Get a specific kid's profile"""
    parent = get_current_parent()
    if not parent:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        profile = (
            KidsProfile.query
            .filter_by(id=kid_id, parent_profile_id=parent.id)
            .options(joinedload(KidsProfile.image))
            .first()
        )

        if not profile:
            return jsonify({'error': 'Kid profile not found'}), 404

        return jsonify(serialize_kids_profile(profile)), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── POST create new kid profile ──────────────────────────────────────────
@app.route('/kids/profiles', methods=['POST'])
def create_kids_profile():
    """Create a new kid profile"""
    logger.info("Creating new kid profile")
    
    parent = get_current_parent()
    if not parent:
        logger.warning("Unauthorized access attempt to create kid profile")
        return jsonify({'error': 'Unauthorized'}), 401
    
    logger.info(f"Parent {parent.id} initiating kid profile creation")

    try:
        data = request.get_json()
        logger.debug(f"Received payload: {data}")

        # Validate required fields
        if not data.get('name'):
            logger.warning(f"Missing required field 'name' for parent {parent.id}")
            return jsonify({'error': 'name is required'}), 400

        # Parse date of birth - frontend sends YYYY/MM/DD format
        parsed_dob = None
        if data.get('date_of_birth'):
            try:
                dob_str = data['date_of_birth']
                # Convert YYYY/MM/DD to YYYY-MM-DD for datetime.fromisoformat()
                formatted_dob = dob_str.replace('/', '-')
                parsed_dob = datetime.fromisoformat(formatted_dob)
                logger.debug(f"Parsed date_of_birth: {parsed_dob} (input: {dob_str})")
            except ValueError as e:
                logger.warning(f"Invalid date format for parent {parent.id}: {data['date_of_birth']}")
                return jsonify({'error': 'Invalid date format. Expected YYYY/MM/DD'}), 400

        logger.info(f"Creating profile for {data.get('name')}")
        
        profile = KidsProfile(
            parent_profile_id=parent.id,
            name=data.get('name'),
            date_of_birth=parsed_dob,
            gender=ChildEnum(data['gender']) if data.get('gender') else None,
            grade_level=data.get('grade_level'),
            bio=data.get('bio'),
            hobbies=data.get('hobbies', []),
            allergies=data.get('allergies', []),
            individual_needs=data.get('individual_needs', [])
        )

        db.session.add(profile)
        db.session.commit()
        
        logger.info(f"Kid profile {profile.id} created successfully for parent {parent.id}")
        return jsonify(serialize_kids_profile(profile)), 201
        
    except ValueError as e:
        logger.error(f"Invalid data for parent {parent.id}: {str(e)}", exc_info=True)
        return jsonify({'error': f'Invalid data: {str(e)}'}), 400
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error creating kid profile for parent {parent.id}: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ─── PUT update kid profile ──────────────────────────────────────────────
@app.route('/kids/profiles/<int:kid_id>', methods=['PUT'])
def update_kids_profile(kid_id):
    """Update an existing kid's profile"""
    parent = get_current_parent()
    if not parent:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        profile = KidsProfile.query.filter_by(
            id=kid_id,
            parent_profile_id=parent.id
        ).first()

        if not profile:
            return jsonify({'error': 'Kid profile not found'}), 404

        data = request.get_json()

        # Update fields
        if 'name' in data:
            profile.name = data['name']
        if 'date_of_birth' in data:
            profile.date_of_birth = datetime.fromisoformat(data['date_of_birth']) if data['date_of_birth'] else None
        if 'gender' in data:
            profile.gender = ChildEnum(data['gender']) if data['gender'] else None
        if 'grade_level' in data:
            profile.grade_level = data['grade_level']
        if 'bio' in data:
            profile.bio = data['bio']
        if 'hobbies' in data:
            profile.hobbies = data['hobbies']
        if 'allergies' in data:
            profile.allergies = data['allergies']
        if 'individual_needs' in data:
            profile.individual_needs = data['individual_needs']

        profile.updated_at = datetime.now(timezone.utc)
        db.session.commit()

        return jsonify(serialize_kids_profile(profile)), 200
    except ValueError as e:
        return jsonify({'error': f'Invalid data: {str(e)}'}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


# ─── DELETE kid profile ──────────────────────────────────────────────────
@app.route('/kids/profiles/<int:kid_id>', methods=['DELETE'])
def delete_kids_profile(kid_id):
    """Delete a kid profile"""
    parent = get_current_parent()
    if not parent:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        profile = KidsProfile.query.filter_by(
            id=kid_id,
            parent_profile_id=parent.id
        ).first()

        if not profile:
            return jsonify({'error': 'Kid profile not found'}), 404

        db.session.delete(profile)
        db.session.commit()

        return jsonify({'message': 'Kid profile deleted successfully'}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
    
    
# ─────────────────────────────────────────────────────────────────────────────
# KIDS PROFILE IMAGES
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/kids/<int:kid_id>/image', methods=['GET'])
def get_kid_image(kid_id):
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    parent = user.parent_profile
    if not parent:
        return jsonify({'error': 'Parent profile not found'}), 404

    # Make sure this child belongs to the logged-in parent
    kid = KidsProfile.query.filter_by(
        id=kid_id,
        parent_profile_id=parent.id
    ).first()

    if not kid:
        return jsonify({'error': 'Kid profile not found'}), 404

    image = kid.image

    if not image:
        return jsonify({
            'id': None,
            'image_url': None,
            'created_at': None
        }), 200

    return jsonify({
        'id': image.id,
        'image_url': image.image_url,
        'created_at': image.created_at.isoformat() if image.created_at else None,
    }), 200


@app.route('/kids/<int:kid_id>/image', methods=['POST'])
def post_kid_image(kid_id):
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    parent = user.parent_profile
    if not parent:
        return jsonify({'error': 'Parent profile not found'}), 404

    # Make sure this child belongs to the logged-in parent
    kid = KidsProfile.query.filter_by(
        id=kid_id,
        parent_profile_id=parent.id
    ).first()

    if not kid:
        return jsonify({'error': 'Kid profile not found'}), 404

    data = request.get_json()

    if not data or 'image_url' not in data:
        return jsonify({'error': 'image_url is required'}), 400

    # KidsProfileImages is one-to-one with KidsProfile
    if kid.image:
        kid.image.image_url = data['image_url']
    else:
        image = KidsProfileImages(
            kids_profile_id=kid.id,
            image_url=data['image_url']
        )
        db.session.add(image)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to save image'}), 500

    return jsonify({
        'message': 'Kid image saved',
        'kid_id': kid.id,
        'image_url': kid.image.image_url if kid.image else data['image_url']
    }), 201
 
 
# ─────────────────────────────────────────────────────────────────────────────
# EVENT ORGANIZER ✅
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/organizer', methods=['GET'])
def get_event_organizer():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    organizer = user.event_organizer
    if not organizer:
        return jsonify({'error': 'Organizer profile not found'}), 404

    return jsonify({
        'id':                   organizer.id,
        'user_id':              organizer.user_id,  # ← ADD (useful for APIs)
        'name':                 organizer.name,
        'organizer_bio':        organizer.organizer_bio,
        'avatar_url':           organizer.avatar_url,
        'top_event_hashtags':   organizer.top_event_hashtags or [],
        'verification_status':  organizer.verification_status.value,
        'is_approved':          organizer.is_approved,
        'verified_at':          organizer.verified_at.isoformat() if organizer.verified_at else None,
        
        # ── Profile Details ──
        'first_name':           organizer.first_name,
        'last_name':            organizer.last_name,
        'phone_number':         organizer.phone_number,
        'gender':               organizer.gender.value if organizer.gender else None,
        'date_of_birth':        organizer.date_of_birth.isoformat() if organizer.date_of_birth else None,
        
        # ── Stats (important for host credibility) ──
        'follower_count':       organizer.follower_count,  # ← ADD THIS
        'total_events_created': organizer.total_events_created,
        'total_participants':   organizer.total_participants,
        
        # ── Portfolio Images (shows past events/work) ──
        'portfolio_images': [
            {
                'id': img.id,
                'image_url': img.image_url,
                'display_order': img.display_order,
                'uploaded_at': img.uploaded_at.isoformat(),
            }
            for img in organizer.images
        ] if organizer.images else [],  # ← ADD THIS
    }), 200
 
 
@app.route('/organizer', methods=['POST'])
def post_event_organizer():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
 
    organizer = user.event_organizer
 
    if not organizer:
        # Creating a new organizer — name is required
        if 'name' not in data:
            return jsonify({'error': 'name is required to register as an organizer'}), 400
        organizer = EventOrganizer(user_id=user.id, name=data['name'])
        db.session.add(organizer)
    else:
        if 'name' in data:
            organizer.name = data['name']
 
    if 'organizer_bio' in data:
        organizer.organizer_bio = data['organizer_bio']
    if 'top_event_hashtags' in data:
        if not isinstance(data['top_event_hashtags'], list):
            return jsonify({'error': 'top_event_hashtags must be a list'}), 400
        organizer.top_event_hashtags = data['top_event_hashtags']
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to save organizer profile'}), 500
 
    return jsonify({'message': 'Organizer profile saved', 'id': organizer.id}), 201
 
 
 # Only an admin can approve a host/Organizer. This is a separate endpoint to keep the workflow clear and auditable.


@app.route('/organizers/<int:organizer_id>', methods=['GET'])
def get_organizer_public(organizer_id):
    """Public organizer profile with follow status - FULL DETAILS"""
    current_user = get_current_user_from_token()
    
    organizer = db.session.query(EventOrganizer).get(organizer_id)
    
    if not organizer:
        return jsonify({'error': 'Organizer not found'}), 404
    
    if not organizer.is_approved:
        return jsonify({'error': 'Organizer profile not available'}), 403
    
    # Check if current user is following this organizer
    is_following = False
    if current_user:
        follow = Follow.query.filter_by(
            follower_id=current_user.id,
            following_id=organizer.user_id
        ).first()
        is_following = follow is not None
    
    return jsonify({
        'id':                   organizer.id,
        'user_id':              organizer.user_id,
        'name':                 organizer.name,
        'organizer_bio':        organizer.organizer_bio,
        'avatar_url':           organizer.avatar_url,
        'top_event_hashtags':   organizer.top_event_hashtags or [],
        'verification_status':  organizer.verification_status.value,
        'is_approved':          organizer.is_approved,
        'verified_at':          organizer.verified_at.isoformat() if organizer.verified_at else None,
        
        # ── Public Stats ──
        'follower_count':       organizer.follower_count,
        'total_events_created': organizer.total_events_created,
        'total_participants':   organizer.total_participants,
        
        # ── Portfolio ──
        'portfolio_images': [
            {
                'id': img.id,
                'image_url': img.image_url,
                'display_order': img.display_order,
                'uploaded_at': img.uploaded_at.isoformat(),
            }
            for img in organizer.images
        ] if organizer.images else [],
        
        # ── Follow Status ──
        'is_following': is_following,
    }), 200


@app.route('/organizer/<int:organizer_id>/approve', methods=['POST'])
def approve_event_organizer(organizer_id):
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    organizer = EventOrganizer.query.get(organizer_id)
    if not organizer:
        return jsonify({'error': 'Organizer not found'}), 404

    if organizer.is_approved:
        return jsonify({'error': 'Organizer is already approved'}), 409

    organizer.verification_status = OrganizerVerificationStatus.approved
    organizer.verified_at = datetime.utcnow()

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to approve organizer'}), 500

    return jsonify({'message': 'Organizer approved', 'id': organizer.id}), 200


# ─────────────────────────────────────────────────────────────────────────────
# EVENT ORGANIZER IMAGES ✅
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/organizer/images', methods=['GET'])
def get_organizer_images():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    organizer = user.event_organizer
    if not organizer:
        return jsonify({'error': 'Organizer profile not found'}), 404
 
    return jsonify([
        {
            'id':              img.id,
            'cover_image_url': img.cover_image_url,
            'display_order':   img.display_order,
            'uploaded_at':     img.uploaded_at.isoformat() if img.uploaded_at else None,
        }
        for img in organizer.images
    ]), 200
 
 
@app.route('/organizer/images', methods=['POST'])
def post_organizer_image():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    organizer = user.event_organizer
    if not organizer:
        return jsonify({'error': 'Organizer profile not found'}), 404
 
    # Enforce max-3 rule
    if len(organizer.images) >= 3:
        return jsonify({'error': 'Maximum of 3 images allowed'}), 400
 
    data = request.get_json()
    if not data or 'cover_image_url' not in data:
        return jsonify({'error': 'cover_image_url is required'}), 400
 
    image = EventOrganizerImage(
        organizer_id=organizer.id,
        cover_image_url=data['cover_image_url'],
        display_order=data.get('display_order', len(organizer.images)),
    )
    db.session.add(image)
 
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to save image'}), 500
 
    return jsonify({'message': 'Organizer image added', 'id': image.id}), 201
 
 
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
# EVENT LOCATIONS ✅ / GOOGLE MAPS ✅
# ─────────────────────────────────────────────────────────────────────────────


@app.route('/events/map/bounds', methods=['POST'])
def get_events_in_bounds():
    try:
        data = request.get_json()

        ne = data.get('northeast', {})
        sw = data.get('southwest', {})
        
        print("MAP BOUNDS REQUEST:", data)
        print("NE:", ne)
        print("SW:", sw)

        if (ne.get('latitude') is None or ne.get('longitude') is None or
            sw.get('latitude') is None or sw.get('longitude') is None):
            return jsonify({'success': False, 'error': 'Invalid bounds'}), 400

        lat_min = min(float(ne['latitude']), float(sw['latitude']))
        lat_max = max(float(ne['latitude']), float(sw['latitude']))
        lng_min = min(float(ne['longitude']), float(sw['longitude']))
        lng_max = max(float(ne['longitude']), float(sw['longitude']))
        
        print(f"LAT RANGE: {lat_min} to {lat_max}")
        print(f"LNG RANGE: {lng_min} to {lng_max}")
        
                # ✅ ADD THIS: Check total events before filters
        total_events = EventLocation.query.join(EventCoordinates).count()
        print(f"Total events in DB: {total_events}")

        query = EventLocation.query.join(EventCoordinates).options(
            joinedload(EventLocation.event_coordinates),
            joinedload(EventLocation.event_category),
        ).filter(
            EventCoordinates.latitude.between(lat_min, lat_max),
            EventCoordinates.longitude.between(lng_min, lng_max),
        )
                
        # Check after coordinates filter
        coords_filtered = query.filter(
            EventCoordinates.latitude.between(lat_min, lat_max),
            EventCoordinates.longitude.between(lng_min, lng_max),
        ).all()
        print(f"Events after coords filter: {len(coords_filtered)}")

        for e in coords_filtered:
            print(f"  - Event {e.id}: lat={e.event_coordinates.latitude}, lng={e.event_coordinates.longitude}, is_upcoming={e.is_upcoming}")

        # Category filter
        if data.get('category_ids'):
            query = query.filter(
                EventLocation.event_category_id.in_(data.get('category_ids'))
            )

        # Status filter (in query, not Python)
        status = data.get('status_filter', 'upcoming')
        print(f"STATUS FILTER: {status}")
        
        now = datetime.now(timezone.utc)

        if status == 'upcoming':
            query = query.filter(EventLocation.start_time > now)  # ← Filter by column
        elif status == 'ongoing':
            query = query.filter(
                EventLocation.start_time <= now,
                (EventLocation.end_time >= now) | (EventLocation.end_time == None)
    )

        events = query.all()
        print(f"Events after status filter: {len(events)}")

        # Build response (Tier 1 - lightweight)
        map_events = [
            {
                'id': event.id,
                'title': event.event_category.name if event.event_category else 'Event',
                'event_name': event.event_name,
                'coordinate': {
                    'latitude': float(event.event_coordinates.latitude),
                    'longitude': float(event.event_coordinates.longitude),
                },
                'address': event.event_coordinates.address,
                'start_time': event.start_time.isoformat(),
                'remaining_spots': max(0, event.max_attendees - event.total_participants),
                'max_attendees': event.max_attendees,
                'duration_minutes': event.duration_minutes,
                'end_time': event.end_time.isoformat(),
                'age_range': event.age_range,
                'base_price': float(event.base_price) if event.base_price else None,
                'currency': event.currency,
                'status': 'ongoing' if event.is_ongoing else 'upcoming',
            }
            for event in events
        ]

        return jsonify({
            'success': True,
            'count': len(map_events),
            'events': map_events
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


# Shows a lightweight summary of an event for use in marker info windows on the map. 
# This endpoint returns only the essential fields needed for displaying event information without loading full details, making it efficient for map interactions.
@app.route('/events/<int:event_id>/summary', methods=['GET'])
def get_event_summary(event_id):
    """
    Lightweight event summary for marker info window.
    Returns ONLY fields needed for the marker card/info window.
    Tier 2 data - between coordinates and full details.
    """
    try:
        event = (
            EventLocation.query
            .filter_by(id=event_id)
            .options(
                joinedload(EventLocation.event_coordinates),
                joinedload(EventLocation.event_category),
                joinedload(EventLocation.cover_image),  # Only cover image, not gallery
            )
            .first()
        )
        
        if not event:
            return jsonify({'error': 'Event not found'}), 404
        
        response = jsonify({
            'id': event.id,
            'title': event.event_category.name if event.event_category else 'Event',
            'event_name': event.event_name,
            'coordinate': {
                'latitude': float(event.event_coordinates.latitude),
                'longitude': float(event.event_coordinates.longitude),
            },
            'address': event.event_coordinates.address,
            'start_time': event.start_time.isoformat(),
            'end_time': event.end_time.isoformat() if event.end_time else None,
            'duration_minutes': event.duration_minutes,
            'remaining_spots': max(0, event.max_attendees - event.total_participants),
            'max_attendees': event.max_attendees,
            'age_range': event.age_range,
            'base_price': float(event.base_price) if event.base_price else None,
            'currency': event.currency,
            'status': (
                'ongoing' if event.is_ongoing
                else 'upcoming' if event.is_upcoming
                else 'past'
            ),
            'cover_image': event.cover_image.image_url if event.cover_image else None,
            'total_attendees': event.total_participants,
            'is_upcoming': event.is_upcoming,
            'is_ongoing': event.is_ongoing,
        })
        
        # Cache for 2 minutes - lightweight so can be cached
        response.cache_control.max_age = 120
        response.add_etag()
        
        return response, 200
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Internal server error'}), 500
   

# Shows full event details for a specific event, including organizer info, images, and attendance stats. 
# This endpoint is used when a user taps on a marker on the map to view event details.
@app.route('/events/<int:event_id>', methods=['GET'])
def get_event_details(event_id):
    """
    Get full event details (existing endpoint - UNCHANGED).
    Add caching headers since maps will fetch these on marker tap.
    """
    try:
        event = (
            EventLocation.query
            .filter_by(id=event_id)
            .options(
                joinedload(EventLocation.event_coordinates),
                joinedload(EventLocation.event_organizer),
                joinedload(EventLocation.event_category),
                joinedload(EventLocation.cover_image),
                joinedload(EventLocation.images),
                joinedload(EventLocation.attendances).joinedload(Attendance.user).joinedload(User.parent_profile)
            )
            .first()
        )
        
        if not event:
            return jsonify({'error': 'Event not found'}), 404
        
        response = jsonify({
            'id': event.id,
            'event_coordinates': {
                'id': event.event_coordinates.id,
                'address': event.event_coordinates.address,
                'latitude': float(event.event_coordinates.latitude) if event.event_coordinates.latitude else None,
                'longitude': float(event.event_coordinates.longitude) if event.event_coordinates.longitude else None,
            },
            'event_category': {
                'id': event.event_category.id,
                'name': event.event_category.name,
            } if event.event_category else None,
            'start_time': event.start_time.isoformat(),
            'duration_minutes': event.duration_minutes,
            'end_time': event.end_time.isoformat() if event.end_time else None,
            'event_description': event.event_description,
            'max_attendees': event.max_attendees,
            'girls_attendees': event.girls_attendees,
            'boys_attendees': event.boys_attendees,
            'age_range': event.age_range,
            'base_price': float(event.base_price) if event.base_price else None,
            'currency': event.currency,
            'is_checkin_closed': event.is_checkin_closed,
            'is_upcoming': event.is_upcoming,
            'is_ongoing': event.is_ongoing,
            'is_past': event.is_past,
            'total_attendees': event.total_participants,
            'remaining_spots': event.max_attendees - event.total_participants,
            'total_male_attendees': sum(1 for a in event.attendances if a.user.parent_profile.gender == GenderEnum.Male),
            'total_female_attendees': sum(1 for a in event.attendances if a.user.parent_profile.gender == GenderEnum.Female),
            'cover_image': {
                'id': event.cover_image.id,
                'image_url': event.cover_image.image_url,
                'uploaded_at': event.cover_image.uploaded_at.isoformat()
            } if event.cover_image else None,
            'gallery_images': [
                {
                    'id': img.id,
                    'image_url': img.image_url,
                    'display_order': img.display_order
                }
                for img in event.images
            ],
            'organizer_preview': {
                'id': event.event_organizer.id,
                'user_id': event.event_organizer.user_id,
                'first_name': event.event_organizer.first_name,
                'avatar_url': event.event_organizer.avatar_url,
                'is_approved': event.event_organizer.is_approved,
            }
        })
        
        # Cache for 5 minutes (events don't change frequently mid-session)
        response.cache_control.max_age = 300
        response.add_etag()
        
        return response, 200
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Internal server error'}), 500
    
    
# Load organizer details for a specific event, but only the organizer info, not the full event details. 
# This is useful for lightweight requests where you just need to show who is organizing an event without fetching all event data.  

@app.route('/events/<int:event_id>/organizer/details', methods=['GET'])
def get_event_organizer_details(event_id):
    """
    Get organizer information for a specific event.
    Includes EventOrganizer and EventOrganizerImage data.
    """
    try:
        event = (
            EventLocation.query
            .filter_by(id=event_id)
            .options(
                joinedload(EventLocation.event_organizer).options(
                    joinedload(EventOrganizer.images),
                    joinedload(EventOrganizer.owner).joinedload(User.parent_profile)
                )
            )
            .first()
        )
        
        if not event or not event.event_organizer:
            return jsonify({'error': 'Event or organizer not found'}), 404
        
        organizer = event.event_organizer
        
        organizer_data = {
            'id': organizer.id,
            'name': organizer.name,
            'avatar_url': organizer.avatar_url,
            'bio': organizer.organizer_bio,
            'verification_status': organizer.verification_status.value,
            'is_approved': organizer.is_approved,
            'portfolio_images': [
                {
                    'id': img.id,
                    'image_url': img.image_url,
                    'display_order': img.display_order,
                    'uploaded_at': img.uploaded_at.isoformat()
                }
                for img in organizer.images
            ],
            'contact_email': organizer.owner.email if organizer.owner else None,
        }
        
        return jsonify(organizer_data), 200
        
    except Exception:
        traceback.print_exc()
        return jsonify({'error': 'Internal server error'}), 500


# When creating an event, the organizer can either select an existing event coordinates by providing an event_coordinates_id or create new event coordinates by providing event_coordinates_data.
# The endpoint will handle both cases and ensure that the event coordinates are valid before creating the event.
@app.route('/events', methods=['POST'])
def post_event():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    organizer = user.event_organizer
    if not organizer or not organizer.is_approved:
        return jsonify({'error': 'Only approved organizers can create events'}), 403
 
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
 
    # Handle event coordinates - either existing event_coordinates_id OR new event coordinates data
    event_coordinates_id = None
    
    if 'event_coordinates_id' in data:
        event_coordinates_id = data['event_coordinates_id']
        event_coordinates = EventCoordinates.query.get(event_coordinates_id)
        if not event_coordinates:
            return jsonify({'error': f'Event coordinates with id {event_coordinates_id} not found'}), 404
    
    elif 'event_coordinates_data' in data:
        event_coordinates_data = data['event_coordinates_data']
        required_event_coordinates = ['name', 'latitude', 'longitude']
        missing = [f for f in required_event_coordinates if f not in event_coordinates_data]
        if missing:
            return jsonify({'error': f'Missing event coordinates fields: {", ".join(missing)}'}), 400
        
        try:
            latitude = float(event_coordinates_data['latitude'])
            longitude = float(event_coordinates_data['longitude'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Latitude and longitude must be valid numbers'}), 400
        
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return jsonify({'error': 'Invalid latitude/longitude coordinates'}), 400
        
        tolerance = 0.0001
        existing_event_coordinates = EventCoordinates.query.filter(
            EventCoordinates.name.ilike(event_coordinates_data['name'].strip()),
            EventCoordinates.latitude.between(latitude - tolerance, latitude + tolerance),
            EventCoordinates.longitude.between(longitude - tolerance, longitude + tolerance)
        ).first()
        
        if existing_event_coordinates:
            print(f"✅ Event coordinates already exist: {existing_event_coordinates.id}")
            event_coordinates_id = existing_event_coordinates.id
            event_coordinates = existing_event_coordinates
        else:
            try:
                new_event_coordinates = EventCoordinates(
                    address=event_coordinates_data.get('address'),
                    latitude=latitude,
                    longitude=longitude,
                    name=event_coordinates_data['name']
                )
                db.session.add(new_event_coordinates)
                db.session.flush()
                event_coordinates_id = new_event_coordinates.id
                event_coordinates = new_event_coordinates
                print(f"✅ New event coordinates created with ID: {event_coordinates_id}")
            except Exception as e:
                db.session.rollback()
                print(f"❌ Error creating event coordinates: {str(e)}")
                return jsonify({'error': f'Failed to create event coordinates: {str(e)}'}), 400
    else:
        return jsonify({'error': 'Must provide either event_coordinates_id or event_coordinates_data'}), 400
 
    # Rest of event creation
    required = ['event_category_id', 'start_time', 'end_time', 'max_attendees']
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400
 
    try:
        start_time = datetime.fromisoformat(data['start_time'])
        end_time = datetime.fromisoformat(data['end_time'])
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid datetime format. Use ISO 8601.'}), 400
    
    # Calculate duration_minutes from start and end time
    duration = end_time - start_time
    duration_minutes = int(duration.total_seconds() / 60)
    
    if duration_minutes <= 0:
        return jsonify({'error': 'End time must be after start time'}), 400
    
    print(f"⏱️ Event duration: {duration_minutes} minutes")
 
    event = EventLocation(
        event_coordinates_id=event_coordinates_id,
        event_category_id=data['event_category_id'],
        event_organizer_id=organizer.id,
        event_name=data.get('event_name', 'Untitled Event'),
        start_time=start_time,
        duration_minutes=duration_minutes,
        event_description=data.get('event_description'),
        max_attendees=data['max_attendees'],
        girls_attendees=data.get('girls_attendees'),
        boys_attendees=data.get('boys_attendees'),
        min_age=data.get('min_age', 1),
        max_age=data.get('max_age', 18),
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
        print(f"✅ Event created with ID: {event.id}")
        
        # 🔴 NEW: Broadcast the event coordinates to all map viewers in real-time
        if event_coordinates:
            broadcast_event_to_map(event_coordinates)
        
    except Exception as e:
        db.session.rollback()
        traceback.print_exc()
        print(f"❌ Error creating event: {str(e)}")
        return jsonify({'error': 'Failed to create event'}), 500
 
    return jsonify({'message': 'Event created', 'id': event.id}), 201


# ─────────────────────────────────────────────────────────────────────────────
# ATTENDANCE ✅
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
    existing = Attendance.query.filter_by(parent_id=user.id, location_id=event.id).first()
    if existing:
        return jsonify({'error': 'Already registered for this event'}), 409
 
    # Check gender-based capacity
    profile = user.profile
    if profile and profile.gender:
        can_register, reason = event.can_register(profile.gender)
        if not can_register:
            return jsonify({'error': reason}), 400
 
    attendance = Attendance(parent_id=user.id, location_id=event.id)
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
# CHECK-IN ✅
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
    attendance = Attendance.query.filter_by(parent_id=user.id, location_id=event.id).first()
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
# TICKETS ✅
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/tickets', methods=['GET'])
def get_tickets():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Re-fetch user with all relationships eager-loaded
    user = (
        db.session.query(User)
        .options(
            db.joinedload(User.attendances)
            .joinedload(Attendance.ticket),
            db.joinedload(User.attendances)
            .joinedload(Attendance.location),
            db.joinedload(User.attendances)
            .joinedload(Attendance.location)
            .joinedload(EventLocation.event_coordinates),
            db.joinedload(User.attendances)
            .joinedload(Attendance.location)
            .joinedload(EventLocation.event_category),
            db.joinedload(User.attendances)
            .joinedload(Attendance.location)
            .joinedload(EventLocation.event_organizer),
            db.joinedload(User.parent_profile)
        )
        .filter(User.id == user.id)
        .first()
    )
    
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # Collect tickets through attendances
    tickets = [a.ticket for a in user.attendances if a.ticket]
    
    # Helper function to format ticket
    def format_ticket(t):
        return {
            # Ticket info
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
            
            # Event Location Details
            'event': {
                'id':                t.attendance.location.id,
                'event_name':        t.attendance.location.event_name,
                'start_time':        t.attendance.location.start_time.isoformat(),
                'end_time':          t.attendance.location.end_time.isoformat() if t.attendance.location.end_time else None,
                'duration_minutes':  t.attendance.location.duration_minutes,
                'description':       t.attendance.location.event_description,
                'age_range':         t.attendance.location.age_range,
                'max_attendees':     t.attendance.location.max_attendees,
                'category':          t.attendance.location.event_category.name,
                'organizer': {
                    'id':   t.attendance.location.event_organizer.id,
                    'name': t.attendance.location.event_organizer.name,
                },
                'base_price':        float(t.attendance.location.base_price) if t.attendance.location.base_price else None,
            },
            
            # Venue/Coordinates (renamed from event_coordinates) ✅
            'event_coordinates': {
                'id':        t.attendance.location.event_coordinates.id,
                'address':   t.attendance.location.event_coordinates.address,
                'latitude':  t.attendance.location.event_coordinates.latitude,
                'longitude': t.attendance.location.event_coordinates.longitude,
            },
            
            # Parent/User Profile Info
            'user_profile': {
                'id':        user.id,
                'email':     user.email,
                'name':      f"{user.parent_profile.first_name} {user.parent_profile.last_name}".strip() if user.parent_profile else None,
                'phone':     user.parent_profile.phone_number if user.parent_profile else None,
                'gender':    user.parent_profile.gender.value if user.parent_profile and user.parent_profile.gender else None,
            }
        }
    
    # Separate active and expired tickets ✅
    active_tickets = [format_ticket(t) for t in tickets if t.status == "active"]
    expired_tickets = [format_ticket(t) for t in tickets if t.status != "active"]
    
    # Return wrapped in MyTicketsResponseDto structure ✅
    return jsonify({
        'active_tickets': active_tickets,
        'expired_tickets': expired_tickets,
        'created_events': []  # TODO: Implement if needed
    }), 200
 
 
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
 
 
#Show all events created by the logged-in organizer in TICKET screen, including event coordinates and category details, cover image, and other relevant information.
@app.route('/organizer/events/tickets', methods=['GET'])
def get_created_events():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'message': 'Unauthorized'}), 401

    # Get the organizer profile for this user
    organizer = EventOrganizer.query.filter_by(user_id=user.id).first()
    if not organizer:
        return jsonify({'message': 'User is not an event organizer'}), 403

    # Get all events created by this organizer
    created_locations = (
        EventLocation.query
        .filter_by(event_organizer_id=organizer.id)
        .options(
            db.joinedload(EventLocation.event_coordinates),  # ✅ Fixed: event_coordinates
            db.joinedload(EventLocation.event_category),
            db.joinedload(EventLocation.cover_image),
            db.joinedload(EventLocation.images)  # ✅ Added: event images
        )
        .all()
    )

    created_events = []
    for loc in created_locations:
        # ✅ Fixed: Use event_coordinates correctly
        event_coords = loc.event_coordinates
        
        # ✅ Fixed: Cover image from EventCoverImage
        cover_image_url = loc.cover_image.image_url if loc.cover_image else None
        
        # ✅ Fixed: Gallery images from EventLocationImage
        gallery_images = [img.image_url for img in loc.images] if loc.images else []
        
        created_events.append({
            'id':                        loc.id,
            'event_name':                loc.event_name,
            'event_coordinates_id':      loc.eventcoordinates_id,  # ✅ Fixed: correct field name
            'event_coordinates_address': event_coords.address if event_coords else None,
            'event_coordinates_latitude': float(event_coords.latitude) if event_coords and event_coords.latitude else None,
            'event_coordinates_longitude': float(event_coords.longitude) if event_coords and event_coords.longitude else None,
            'cover_image_url':           cover_image_url,
            'gallery_images':            gallery_images,  # ✅ New: multiple gallery images
            'category':                  loc.event_category.name,
            'start_time':                loc.start_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'end_time':                  loc.end_time.strftime('%Y-%m-%dT%H:%M:%SZ') if loc.end_time else None,
            'duration_minutes':          loc.duration_minutes,
            'description':               loc.event_description,
            'base_price':                float(loc.base_price) if loc.base_price else None,
            'currency':                  loc.currency,
            'max_attendees':             loc.max_attendees,
            'girls_attendees':           loc.girls_attendees,
            'boys_attendees':            loc.boys_attendees,
            'min_age':                   loc.min_age,
            'max_age':                   loc.max_age,
            'age_range':                 loc.age_range,
            'total_participants':        loc.total_participants,  # ✅ New: actual participant count
            'is_checkin_closed':         loc.is_checkin_closed,
            'is_ongoing':                loc.is_ongoing,
            'is_upcoming':               loc.is_upcoming,
            'is_past':                   loc.is_past,
            'created_at':                loc.created_at.strftime('%Y-%m-%dT%H:%M:%SZ'),
        })

    return jsonify({'created_events': created_events}), 200

# ─────────────────────────────────────────────────────────────────────────────
# Qr CODE SCANNER/GENERATOR ✅
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/ticket/<string:ticket_uid>/rotating_qr', methods=['GET'])
def get_rotating_qr(ticket_uid: str):
    app.logger.debug(
        f"GET /ticket/{ticket_uid}/rotating_qr - Request received"
    )

    # Get authenticated user
    user = get_current_user_from_token()

    if not user:
        app.logger.warning(
            f"Unauthorized access attempt to rotating_qr for ticket={ticket_uid}"
        )
        return jsonify({'message': 'Unauthorized'}), 401

    app.logger.debug(
        f"User {user.id} requesting rotating QR for ticket={ticket_uid}"
    )

    # Find ticket
    ticket = Ticket.query.filter_by(ticket_uid=ticket_uid).first_or_404()

    # Get attendance associated with the ticket
    attendance = ticket.attendance

    if not attendance:
        app.logger.error(
            f"Ticket {ticket_uid} has no associated attendance record"
        )
        return jsonify({
            'message': 'Ticket has no attendance record'
        }), 500

    # Attendance uses parent_id, not user_id
    attendance_parent_id = attendance.parent_id

    app.logger.debug(
        f"Ticket found: {ticket_uid}, "
        f"attendance_parent_id={attendance_parent_id}"
    )

    # Make sure the ticket belongs to the authenticated user
    if attendance_parent_id != user.id:
        app.logger.warning(
            f"Forbidden: User {user.id} attempted to access "
            f"ticket {ticket_uid} owned by parent {attendance_parent_id}"
        )
        return jsonify({'message': 'Forbidden'}), 403

    # Check whether ticket is still active
    if ticket.is_expired or ticket.status == 'cancelled':
        app.logger.info(
            f"Inactive ticket access: ticket={ticket_uid}, "
            f"is_expired={ticket.is_expired}, "
            f"status={ticket.status}"
        )
        return jsonify({
            'message': 'Ticket is not active'
        }), 400

    # Generate rotating QR token
    try:
        app.logger.debug(
            f"Generating rotating token for ticket={ticket_uid}"
        )

        token = generate_rotating_token(ticket.ticket_uid)

        app.logger.info(
            f"Successfully generated rotating token for ticket={ticket_uid}"
        )

    except Exception as e:
        app.logger.exception(
            f"Failed to generate rotating token "
            f"for ticket={ticket_uid}: {e}"
        )

        return jsonify({
            'message': 'Failed to generate QR code'
        }), 500

    # Calculate remaining lifetime of the current QR window
    now = time.time()

    current_window_start = (
        int(now // WINDOW_SECONDS) * WINDOW_SECONDS
    )

    expires_in_ms = int(
        (current_window_start + WINDOW_SECONDS - now) * 1000
    )

    app.logger.debug(
        f"Rotating QR response: ticket={ticket_uid}, "
        f"expires_in_ms={expires_in_ms}, "
        f"window_seconds={WINDOW_SECONDS}"
    )

    return jsonify({
        'token': token,
        'expires_in_ms': expires_in_ms,
        'window_seconds': WINDOW_SECONDS,
    }), 200


@app.route('/ticket/verify_rotating_qr', methods=['POST'])
def verify_rotating_qr():
    """
    Scanner app verifies QR code (REST endpoint).
    If valid, also notify the ticket holder via Socket.IO.
    """
    user = get_current_user_from_token()
    if not user:
        return jsonify({'message': 'Unauthorized'}), 401

    token    = request.json.get('token', '')
    event_id = request.json.get('event_id')

    is_valid, ticket_uid = verify_rotating_token(token)

    if not is_valid:
        return jsonify({'valid': False, 'message': 'QR code expired or invalid'}), 200

    ticket = Ticket.query.filter_by(ticket_uid=ticket_uid).first()
    if not ticket or ticket.is_expired or ticket.status == 'cancelled':
        return jsonify({'valid': False, 'message': 'Ticket is not active'}), 200

    if event_id and ticket.attendance.location_id != int(event_id):
        return jsonify({'valid': False, 'message': 'Ticket is for a different event'}), 200

    attendance_user = ticket.attendance.user
    profile         = attendance_user.parent_profile

    # ✅ NEW: Notify ticket holder that their ticket was scanned
    room = f"ticket:{ticket_uid}"
    socketio.emit('qr/ticket_scanned', {
        'ticket_uid': ticket_uid,
        'scanned_at': time.time(),
        'event_name': ticket.attendance.location.name if ticket.attendance.location else 'Unknown Event'
    }, room=room)
    
    app.logger.info(f"Ticket {ticket_uid} scanned, notifying holder via Socket.IO")

    return jsonify({
        'valid':       True,
        'ticket_uid':  ticket.ticket_uid,
        'ticket_code': ticket.ticket_code,
        'user':        attendance_user.email,
        'first_name':  profile.first_name if profile else None,
        'last_name':   profile.last_name  if profile else None,
        'status':      ticket.status,
    }), 200


# @app.route('/event/<int:event_id>/lookup_attendee', methods=['GET'])
# def lookup_attendee(event_id: int):
#     user = get_current_user_from_token()
#     if not user:
#         return jsonify({'message': 'Unauthorized'}), 401

#     logger.info(
#         f"lookup_attendee: event_id={event_id} requested by user_id={user.id} "
#         f"(type={type(user.id).__name__})"
#     )

#     location = EventLocation.query.filter_by(id=event_id).first()  # ✅ Changed from EventLocation
#     if not location:
#         logger.warning(f"lookup_attendee: no EventLocation found with id={event_id}")
#         return jsonify({'message': 'Event not found'}), 404

#     if location.event_organizer_id != user.id:  # ✅ Changed: organizer check
#         logger.warning(
#             f"lookup_attendee: FORBIDDEN — event_id={event_id} "
#             f"event_organizer_id={location.event_organizer_id} (type={type(location.event_organizer_id).__name__}) "
#             f"requesting user_id={user.id} (type={type(user.id).__name__})"
#         )
#         return jsonify({'message': 'Forbidden'}), 403

#     ticket_code = request.args.get('ticket_code', '').strip()
#     name_email  = request.args.get('query', '').strip()

#     logger.info(
#         f"lookup_attendee: ownership OK, location_id={location.id}, "
#         f"ticket_code='{ticket_code}', query='{name_email}'"
#     )

#     if not ticket_code and not name_email:
#         return jsonify({'message': 'Provide ticket_code or query'}), 400

#     def escape_like(s: str) -> str:
#         """Escape LIKE wildcards so literal % and _ in user input don't act as wildcards."""
#         return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

#     q = (
#         Attendance.query
#         .filter_by(location_id=location.id)
#         .join(Ticket,       Ticket.attendance_id == Attendance.id)
#         .join(User,         User.id == Attendance.user_id)
#         .outerjoin(ParentsProfile, ParentsProfile.parents_id == User.id)  # ✅ Changed from UserProfile
#     )

#     if ticket_code:
#         safe_code = escape_like(ticket_code)
#         q = q.filter(Ticket.ticket_code.ilike(f'%{safe_code}%'))
#     elif name_email:
#         safe_query = escape_like(name_email)
#         q = q.filter(
#             db.or_(
#                 User.email.ilike(f'%{safe_query}%'),
#                 ParentsProfile.first_name.ilike(f'%{safe_query}%'),  # ✅ Changed from UserProfile
#                 ParentsProfile.last_name.ilike(f'%{safe_query}%'),   # ✅ Changed from UserProfile
#             )
#         )

#     attendances = q.limit(10).all()

#     logger.info(
#         f"lookup_attendee: location_id={location.id} matched {len(attendances)} attendance row(s)"
#     )

#     # ── Batch-fetch most-recent image per user (avoids N+1 query loop) ─────────
#     user_ids = [attendance.user.id for attendance in attendances]
#     images_by_user = {}
#     if user_ids:
#         all_images = (
#             ParentsProfileImages.query  # ✅ Changed from UserImages
#             .filter(ParentsProfileImages.parent_profile_id.in_(user_ids))  # ✅ Adjusted filter
#             .order_by(ParentsProfileImages.parent_profile_id, ParentsProfileImages.created_at.desc())  # ✅ Changed order_by
#             .all()
#         )
#         for img in all_images:
#             # First occurrence per parent_profile_id is the most recent, due to ORDER BY above
#             if img.parent_profile_id not in images_by_user:  # ✅ Changed key
#                 images_by_user[img.parent_profile_id] = img

#     results = []
#     for attendance in attendances:
#         ticket  = attendance.ticket
#         profile = attendance.user.parent_profile  # ✅ Changed from .profile to .parent_profile
#         image   = images_by_user.get(profile.id) if profile else None  # ✅ Updated lookup

#         results.append({
#             'attendance_id':       attendance.id,
#             'ticket_uid':          ticket.ticket_uid,
#             'ticket_code':         ticket.ticket_code,
#             'ticket_type':         ticket.ticket_type,
#             'status':              ticket.status,
#             'is_checked_in':       ticket.is_checked_in,
#             'issued_at':           ticket.issued_at.strftime('%Y-%m-%dT%H:%M:%SZ'),
#             'amount_paid':         float(ticket.amount_paid) if ticket.amount_paid else None,
#             'currency':            ticket.currency,
#             'cancellation_reason': ticket.cancellation_reason,
#             'user': {
#                 'email':        attendance.user.email,
#                 'first_name':   profile.first_name   if profile else None,
#                 'last_name':    profile.last_name     if profile else None,
#                 'gender':       profile.gender.value  if profile and profile.gender else None,
#                 'phone_number': profile.phone_number  if profile else None,
#                 'image_url':    image.image_url       if image   else None,
#             },
#             'location': {
#                 'start_time':        location.start_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
#                 'is_checkin_closed': location.is_checkin_closed,
#             },
#         })

#     return jsonify({'results': results}), 200
 

@app.route('/tickets/<ticket_uid>', methods=['GET'])
def get_ticket(ticket_uid: str):
    """Get a specific ticket by UID."""
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    
    ticket = (
        Ticket.query
        .filter_by(ticket_uid=ticket_uid)
        .join(Attendance)
        .filter(Attendance.parent_id == user.id)
        .first_or_404()
    )
    
    return jsonify(_ticket_to_json(ticket)), 200


# ═══════════════════════════════════════════════════════════════════════════
# QR Token Management - Separate Endpoint
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/tickets/<ticket_uid>/qr-token', methods=['GET'])
def get_qr_token(ticket_uid: str):
    """Generate rotating QR token for ticket."""
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    
    ticket = Ticket.query.filter_by(ticket_uid=ticket_uid).first()
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404
    
    if ticket.attendance.parent_id != user.id:
        return jsonify({'error': 'Forbidden'}), 403
    
    if ticket.is_expired or ticket.status == 'cancelled':
        return jsonify({'error': 'Ticket is not active'}), 410  # 410 Gone
    
    try:
        token = generate_rotating_token(ticket.ticket_uid)
        now = time.time()
        current_window_start = int(now // WINDOW_SECONDS) * WINDOW_SECONDS
        expires_in_ms = int((current_window_start + WINDOW_SECONDS - now) * 1000)
        
        return jsonify({
            'token': token,
            'expiresInMs': expires_in_ms,  # camelCase per Google API design
            'windowSeconds': WINDOW_SECONDS,
        }), 200
    except Exception as e:
        app.logger.exception(f"Failed to generate QR for {ticket_uid}")
        return jsonify({'error': 'Failed to generate QR token'}), 500


# ═══════════════════════════════════════════════════════════════════════════
# QR Verification - POST with validation
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/tickets/verify-qr', methods=['POST'])
def verify_qr_token():
    """Verify a QR token. Returns 200 for valid, 400 for invalid."""
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json()
    if not data or 'token' not in data:
        return jsonify({'error': 'token is required'}), 400
    
    token = data['token']
    event_id = data.get('eventId')  # camelCase
    
    is_valid, ticket_uid = verify_rotating_token(token)
    if not is_valid:
        return jsonify({
            'valid': False,
            'message': 'QR code expired or invalid'
        }), 400  # ✅ 400 instead of 200
    
    ticket = Ticket.query.filter_by(ticket_uid=ticket_uid).first()
    if not ticket or ticket.is_expired or ticket.status == 'cancelled':
        return jsonify({
            'valid': False,
            'message': 'Ticket is no longer valid'
        }), 400
    
    if event_id and ticket.attendance.location_id != int(event_id):
        return jsonify({
            'valid': False,
            'message': 'Ticket is for a different event'
        }), 400
    
    return jsonify({
        'valid': True,
        'ticketUid': ticket.ticket_uid,
        'ticketCode': ticket.ticket_code,
        'user': ticket.attendance.user.email,
        'firstName': ticket.attendance.user.parent_profile.first_name,
        'lastName': ticket.attendance.user.parent_profile.last_name,
        'status': ticket.status,
    }), 200


# ═══════════════════════════════════════════════════════════════════════════
# Helper - DRY JSON serialization
# ═══════════════════════════════════════════════════════════════════════════

def _ticket_to_json(ticket: Ticket) -> dict:
    """Serialize ticket to JSON following Google API guidelines."""
    attendance = ticket.attendance
    location = attendance.location
    
    return {
        'ticketUid': ticket.ticket_uid,
        'ticketCode': ticket.ticket_code,
        'ticketType': ticket.ticket_type,
        'status': ticket.status,
        'issuedAt': ticket.issued_at.isoformat() if ticket.issued_at else None,
        'paidAt': ticket.paid_at.isoformat() if ticket.paid_at else None,
        'amountPaid': float(ticket.amount_paid) if ticket.amount_paid else None,
        'currency': ticket.currency,
        'event': {
            'id': location.id,
            'title': location.title,  # ✅ Add if missing
            'startTime': location.start_time.isoformat(),
            'endTime': location.end_time.isoformat() if location.end_time else None,
            'category': location.event_category.name,
            'organizer': {
                'id': location.event_organizer.id,
                'name': location.event_organizer.name,
            },
        },
        'event_coordinates': {
            'id': location.event_coordinates.id,
            'name': location.event_coordinates.name,
            'address': location.event_coordinates.address,
            'latitude': location.event_coordinates.latitude,
            'longitude': location.event_coordinates.longitude,
        },
        'links': {
            'self': f'/api/v1/tickets/{ticket.ticket_uid}',
            'qrToken': f'/api/v1/tickets/{ticket.ticket_uid}/qr-token',
        }
    } 
 
# ─────────────────────────────────────────────────────────────────────────────
# EVENT HOST PAYMENT DETAILS ✅
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/organizer/payment-details', methods=['GET'])
def get_organizer_payment_details():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    organizer = user.event_organizer
    if not organizer:
        return jsonify({'error': 'Organizer profile not found'}), 404
 
    details = organizer.payment_details
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
 
 
@app.route('/organizer/payment-details', methods=['POST'])
def post_organizer_payment_details():
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    organizer = user.event_organizer
    if not organizer:
        return jsonify({'error': 'Organizer profile not found'}), 404
 
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
 
    details = organizer.payment_details
    if not details:
        if 'swish_number' not in data:
            return jsonify({'error': 'swish_number is required'}), 400
        details = EventOrganizerPaymentDetails(event_organizer_id=organizer.id, swish_number=data['swish_number'])
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
# FOLLOWS ✅
# ─────────────────────────────────────────────────────────────────────────────
 
@app.route('/follows', methods=['GET'])
def get_follows():
    """Get current user's followers and following"""
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
 
    return jsonify({
        'following': [{'user_id': f.following_id, 'since': f.created_at.isoformat()} for f in user.following],
        'followers': [{'user_id': f.follower_id, 'since': f.created_at.isoformat()} for f in user.followers],
    }), 200


@app.route('/follows/<int:following_id>', methods=['POST'])
def toggle_follow(following_id):
    """Follow or unfollow based on current state"""
    current_user = get_current_user_from_token()
    if not current_user:
        return jsonify({'error': 'Unauthorized'}), 401
    
    if current_user.id == following_id:
        return jsonify({'error': 'Cannot follow yourself'}), 400
    
    if not User.query.get(following_id):
        return jsonify({'error': 'User not found'}), 404
    
    follow = Follow.query.filter_by(
        follower_id=current_user.id,
        following_id=following_id
    ).first()
    
    try:
        if follow:
            db.session.delete(follow)
            db.session.commit()
            is_following = False  # ✅ Explicitly set
        else:
            follow = Follow(follower_id=current_user.id, following_id=following_id)
            db.session.add(follow)
            db.session.commit()
            is_following = True  # ✅ Explicitly set
        
        return jsonify({
            'following': is_following,  # ✅ Return correct state
            'message': 'Followed' if is_following else 'Unfollowed'
        }), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Toggle failed'}), 500
 
 
# ─────────────────────────────────────────────────────────────────────────────
# EVENT LIKES ✅
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
# FAVOURITE EVENTS (WITH FULL DETAILS) ✅
# ─────────────────────────────────────────────────────────────────────────────


@app.route('/favourites', methods=['GET'])
def get_favourite_events():
    """Get all events liked by the current user with minimal organizer data"""
    logger.info("=== GET /favourites request started ===")
    
    try:
        user = get_current_user_from_token()
        if not user:
            logger.warning("Unauthorized request - no user token found")
            return jsonify({'error': 'Unauthorized'}), 401
        
        logger.info(f"User authenticated: {user.id}")
        
        # Query liked events
        liked_events = (
            db.session.query(EventLocation)
            .join(EventLike, EventLike.event_id == EventLocation.id)
            .filter(EventLike.user_id == user.id)
            .order_by(EventLike.liked_at.desc())
            .all()
        )
        
        logger.info(f"Found {len(liked_events)} liked events for user {user.id}")
        
        response_data = []
        for idx, e in enumerate(liked_events):
            try:
                logger.debug(f"Processing event {idx + 1}/{len(liked_events)}: Event ID {e.id}")
                
                like_record = (
                    db.session.query(EventLike)
                    .filter_by(user_id=user.id, event_id=e.id)
                    .first()
                )
                
                if not like_record:
                    logger.warning(f"Like record not found for event {e.id}, skipping")
                    continue
                
                event_data = {
                    'id': e.id,
                    'event_coordinates_id': e.event_coordinates_id,
                    'event_category_id': e.event_category_id,
                    'event_organizer_id': e.event_organizer_id,
                    'start_time': e.start_time.isoformat(),
                    'end_time': e.end_time.isoformat() if e.end_time else None,
                    'event_description': e.event_description,
                    'max_attendees': e.max_attendees,
                    'girls_attendees': e.girls_attendees,
                    'boys_attendees': e.boys_attendees,
                    'min_age': e.min_age,
                    'max_age': e.max_age,
                    'age_range': e.age_range,
                    'base_price': float(e.base_price) if e.base_price else None,
                    'currency': e.currency,
                    'is_checkin_closed': e.is_checkin_closed,
                    'is_upcoming': e.is_upcoming,
                    'is_ongoing': e.is_ongoing,
                    'is_past': e.is_past,
                }
                
                # ── Event Coordinates ──
                if e.event_coordinates:
                    event_data['event_coordinates'] = {
                        'id': e.event_coordinates.id,
                        'name': e.event_coordinates.name,
                        'address': e.event_coordinates.address,
                        'latitude': e.event_coordinates.latitude,
                        'longitude': e.event_coordinates.longitude,
                    }
                else:
                    event_data['event_coordinates'] = None
                
                # ── Category ──
                if e.event_category:
                    event_data['category'] = {
                        'id': e.event_category.id,
                        'name': e.event_category.name,
                    }
                else:
                    event_data['category'] = None
                
                # ── Organizer (PREVIEW ONLY) ──
                if e.event_organizer:
                    event_data['organizer'] = {
                        'id': e.event_organizer.id,
                        'user_id': e.event_organizer.user_id,
                        'name': e.event_organizer.name,
                        'avatar_url': e.event_organizer.avatar_url,
                        'is_approved': e.event_organizer.is_approved,
                        'follower_count': e.event_organizer.follower_count,
                    }
                    logger.debug(f"  Organizer preview loaded: {e.event_organizer.name}")
                else:
                    logger.warning(f"  Event {e.id} has no organizer")
                    event_data['organizer'] = None
                
                # ── Event Images ──
                if e.cover_image:
                    event_data['cover_image'] = {
                        'id': e.cover_image.id,
                        'image_url': e.cover_image.image_url,
                        'uploaded_at': e.cover_image.uploaded_at.isoformat(),
                    }
                else:
                    event_data['cover_image'] = None
                
                event_data['gallery_images'] = [
                    {
                        'id': img.id,
                        'image_url': img.image_url,
                        'display_order': img.display_order,
                        'uploaded_at': img.uploaded_at.isoformat(),
                    }
                    for img in e.images
                ] if e.images else []
                
                event_data['liked_at'] = like_record.liked_at.isoformat()
                
                response_data.append(event_data)
                logger.debug(f"Event {e.id} processed successfully")
                
            except Exception as event_error:
                logger.error(f"Error processing event {e.id}: {str(event_error)}", exc_info=True)
                continue
        
        logger.info(f"Successfully built response with {len(response_data)} events")
        return jsonify(response_data), 200
        
    except Exception as e:
        logger.error(f"Internal server error in /favourites: {str(e)}", exc_info=True)
        traceback.print_exc()
        return jsonify({'error': 'Internal server error', 'details': str(e)}), 500


@app.route('/favourites/<int:event_id>', methods=['DELETE'])
def remove_favourite_event(event_id):
    """Remove an event from user's favourites"""
    user = get_current_user_from_token()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    
    like = EventLike.query.filter_by(user_id=user.id, event_id=event_id).first()
    if not like:
        return jsonify({'error': 'Event not in favourites'}), 404
    
    try:
        db.session.delete(like)
        db.session.commit()
        return jsonify({'message': 'Event removed from favourites'}), 200
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'error': 'Failed to remove event'}), 500
 
 
# ─────────────────────────────────────────────────────────────────────────────
# REPORTS ✅
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
#     organizer = event.event_organizer
#     payment_details = organizer.payment_details

#     if not payment_details or not payment_details.swish_verified:
#         return jsonify({"error": "Organizer has no verified Swish number"}), 400

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
#         "payeeAlias":  payment_details.swish_number,  # organizer's number
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
#             event_organizer_id=organizer.id,
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
# POST /event/<event_id>/payout  —  trigger organizer payout after event ends
# ─────────────────────────────────────────────────────────────────────────────
 
# @app.route("/event/<int:event_id>/payout", methods=["POST"])
# def trigger_payout(event_id: int):
#     """
#     Calculates the organizer's payout from all paid transactions for the event,
#     deducts the platform fee, and initiates a Swish payout.
 
#     Guard rails:
#         - Caller must be the event's organizer (or an admin — extend as needed).
#         - Event must have ended before a payout is allowed.
#         - Organizer must have a verified Swish number.
#         - A payout can only be triggered once per event.
#     """
#     user = get_current_user_from_token()
#     if not user:
#         return jsonify({"error": "Unauthorized"}), 401
 
#     event = EventLocation.query.get_or_404(event_id)
#     organizer = event.event_organizer
 
#     # Only the owning organizer may trigger their own payout
#     if not organizer or organizer.user_id != user.id:
#         return jsonify({"error": "Forbidden — you are not the organizer of this event"}), 403
 
#     if not event.is_past:
#         return jsonify({"error": "Payout can only be triggered after the event has ended"}), 400
 
#     payment_details = organizer.payment_details
#     if not payment_details or not payment_details.swish_verified:
#         return jsonify({"error": "Organizer has no verified Swish number"}), 400
 
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
#         event_organizer_id=organizer.id,
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
    """Returns the current payout record for an event (organizer-only)."""
    user = get_current_user_from_token()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
 
    event = EventLocation.query.get_or_404(event_id)
    organizer = event.event_organizer
 
    if not organizer or organizer.user_id != user.id:
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



# ─────────────────────────────────────────────────────────────────────────────
# MESSAGES
# ─────────────────────────────────────────────────────────────────────────────
 
 
@app.route('/conversations', methods=['GET'])
def get_conversations():
    """
    Get all active conversations for current user.
    Shows both event-specific and general chats.
    Deduplicates bidirectional conversations.
    """
    current_user = get_current_user_from_token()

    if not current_user:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        # ---------------------------------------------------------
        # Find conversations where current user is either:
        #   - parent
        #   - other user
        # ---------------------------------------------------------

        conversations = db.session.query(Conversation).filter(
            db.or_(
                Conversation.parent_id == current_user.id,
                Conversation.other_user_id == current_user.id
            )
        ).order_by(
            Conversation.updated_at.desc()
        ).all()

        print(f"\n{'=' * 60}")
        print(f"GET /conversations for user {current_user.id}")
        print(
            f"Found {len(conversations)} conversations "
            f"(before dedup)"
        )
        print(f"{'=' * 60}\n")

        # ---------------------------------------------------------
        # Deduplicate bidirectional conversations
        #
        # (user 1 -> user 2) and (user 2 -> user 1)
        # represent the same conversation for the same event.
        # ---------------------------------------------------------

        seen = {}
        unique_conversations = []

        for conv in conversations:

            user_pair = tuple(sorted([
                conv.parent_id,
                conv.other_user_id
            ]))

            key = (
                user_pair[0],
                user_pair[1],
                conv.event_id
            )

            if key not in seen:
                seen[key] = conv
                unique_conversations.append(conv)

            else:
                print(
                    f"DEBUG: Skipping duplicate conversation "
                    f"{conv.id} "
                    f"(already have {seen[key].id})"
                )

        print(
            f"Found {len(unique_conversations)} unique "
            f"conversations (after dedup)\n"
        )

        # ---------------------------------------------------------
        # Build response
        # ---------------------------------------------------------

        threads = []

        for conv in unique_conversations:

            # Determine which user is the other participant
            if conv.parent_id == current_user.id:
                other_user = conv.other_user
            else:
                other_user = conv.parent

            if not other_user:
                print(
                    f"WARNING: Could not resolve other user "
                    f"for conversation {conv.id}"
                )
                continue

            latest_msg = conv.latest_message

            # -----------------------------------------------------
            # DEBUG
            # -----------------------------------------------------

            all_msgs = Message.query.filter_by(
                conversation_id=conv.id
            ).all()

            print(f"\n=== CONV {conv.id} ===")
            print(
                f"  Structure: "
                f"parent_id={conv.parent_id}, "
                f"other_user_id={conv.other_user_id}"
            )
            print(f"  Current user: {current_user.id}")
            print(f"  Other user: {other_user.id}")
            print(
                f"  Total messages in conv: "
                f"{len(all_msgs)}"
            )

            for msg in all_msgs:
                print(
                    f"    - Msg {msg.id}: "
                    f"sender={msg.sender_id} → "
                    f"receiver={msg.receiver_id}, "
                    f"is_read={msg.is_read}, "
                    f"text='{msg.message[:30]}...'"
                )

            # -----------------------------------------------------
            # Unread count
            # -----------------------------------------------------

            unread = db.session.query(Message).filter(
                Message.conversation_id == conv.id,
                Message.receiver_id == current_user.id,
                Message.is_read == False
            ).count()

            print(
                f"  Unread count for user "
                f"{current_user.id}: {unread}"
            )

            # -----------------------------------------------------
            # Only return conversations containing messages
            # -----------------------------------------------------

            if latest_msg:

                # -------------------------------------------------
                # Other user's display name
                # -------------------------------------------------

                other_name = ""

                if other_user.parent_profile:
                    first_name = (
                        other_user.parent_profile.first_name
                        or ""
                    )

                    last_name = (
                        other_user.parent_profile.last_name
                        or ""
                    )

                    other_name = (
                        f"{first_name} {last_name}"
                    ).strip()

                # -------------------------------------------------
                # Other user's image
                # -------------------------------------------------

                other_image = ""

                if (
                    other_user.parent_profile
                    and other_user.parent_profile.images
                ):
                    if len(other_user.parent_profile.images) > 0:
                        other_image = (
                            other_user
                            .parent_profile
                            .images[0]
                            .image_url
                            or ""
                        )

                # -------------------------------------------------
                # Thread response
                # -------------------------------------------------

                thread = {
                    'conversationId': conv.id,
                    'otherUserId': other_user.id,
                    'otherUserName': (
                        other_name
                        or other_user.email
                    ),
                    'otherUserImage': other_image,
                    'eventId': conv.event_id,
                    'preview': (
                        latest_msg.message[:100]
                        + (
                            '...'
                            if len(latest_msg.message) > 100
                            else ''
                        )
                    ),
                    'time': latest_msg.time_ago,
                    'unreadCount': unread,
                    'lastMessageTime': (
                        latest_msg.timestamp.isoformat()
                    )
                }

                print(
                    f"  ✓ Added thread with "
                    f"unreadCount={thread['unreadCount']}"
                )

                threads.append(thread)

            else:
                print(
                    f"  ✗ Skipped - "
                    f"conversation {conv.id} has no messages"
                )

        # ---------------------------------------------------------
        # Return
        # ---------------------------------------------------------

        print(f"\n{'=' * 60}")
        print(f"Returning {len(threads)} threads")
        print(f"Final response = {threads}")
        print(f"{'=' * 60}\n")

        return jsonify(threads), 200

    except Exception as e:

        print(f"ERROR in get_conversations: {e}")

        import traceback
        traceback.print_exc()

        db.session.rollback()

        return jsonify({
            'error': str(e)
        }), 500


@app.route('/conversations', methods=['POST'])
def start_conversation():
    """
    Start or get an existing conversation.

    A conversation is uniquely identified by:
        - the two users, regardless of direction
        - optionally the event

    Returns full conversation data.
    """
    print(f"\n{'=' * 60}")
    print("POST /conversations - REQUEST RECEIVED")
    print(f"{'=' * 60}")

    current_user = get_current_user_from_token()

    print(
        f"🔐 Current user: "
        f"{current_user.id if current_user else 'None'}"
    )

    if not current_user:
        print("❌ Unauthorized - No current user")
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        data = request.get_json() or {}

        print(f"📥 Raw request data: {data}")

        other_user_id = data.get('otherUserId')
        event_id = data.get('eventId')

        print("📝 Parsed values:")
        print(f"   - otherUserId: {other_user_id}")
        print(f"   - eventId: {event_id}")

        # ---------------------------------------------------------
        # Validate other user
        # ---------------------------------------------------------

        if other_user_id is None:
            print("❌ VALIDATION FAILED: otherUserId is required")
            return jsonify({
                'error': 'otherUserId is required'
            }), 400

        try:
            other_user_id = int(other_user_id)
        except (TypeError, ValueError):
            return jsonify({
                'error': 'otherUserId must be an integer'
            }), 400

        print("✅ otherUserId validation passed")

        # ---------------------------------------------------------
        # Prevent self-conversation
        # ---------------------------------------------------------

        if other_user_id == current_user.id:
            print(
                "❌ VALIDATION FAILED: "
                "User trying to start conversation with themselves"
            )
            return jsonify({
                'error': 'Cannot start conversation with yourself'
            }), 400

        print("✅ Self-conversation validation passed")

        # ---------------------------------------------------------
        # Check other user
        # ---------------------------------------------------------

        other_user = db.session.get(User, other_user_id)

        print(
            f"🔍 Looking up other_user with ID "
            f"{other_user_id}: {other_user}"
        )

        if not other_user:
            print(f"❌ User not found: {other_user_id}")
            return jsonify({
                'error': 'User not found'
            }), 404

        print(f"✅ Other user found: {other_user.email}")

        # ---------------------------------------------------------
        # Validate event if supplied
        # ---------------------------------------------------------

        if event_id is not None:
            try:
                event_id = int(event_id)
            except (TypeError, ValueError):
                return jsonify({
                    'error': 'eventId must be an integer'
                }), 400

            event = db.session.get(EventLocation, event_id)

            print(
                f"🔍 Looking up event with ID "
                f"{event_id}: {event}"
            )

            if not event:
                print(f"❌ Event not found: {event_id}")
                return jsonify({
                    'error': 'Event not found'
                }), 404

            print(f"✅ Event found: {event.id}")

        else:
            print("ℹ️ No event_id provided (general conversation)")

        # ---------------------------------------------------------
        # Event filter
        # ---------------------------------------------------------

        if event_id is None:
            event_filter = Conversation.event_id.is_(None)
            print("🔍 Event filter: event_id IS NULL")
        else:
            event_filter = Conversation.event_id == event_id
            print(f"🔍 Event filter: event_id == {event_id}")

        # ---------------------------------------------------------
        # Find existing conversation
        #
        # Scenario 1:
        # parent_id = current user
        # other_user_id = other user
        #
        # Scenario 2:
        # parent_id = other user
        # other_user_id = current user
        # ---------------------------------------------------------

        print("\n🔎 Checking for existing conversation...")
        print(
            f"   Scenario 1: "
            f"parent_id={current_user.id}, "
            f"other_user_id={other_user_id}"
        )
        print(
            f"   Scenario 2: "
            f"parent_id={other_user_id}, "
            f"other_user_id={current_user.id}"
        )

        existing = db.session.query(Conversation).filter(
            db.or_(
                db.and_(
                    Conversation.parent_id == current_user.id,
                    Conversation.other_user_id == other_user_id,
                    event_filter
                ),
                db.and_(
                    Conversation.parent_id == other_user_id,
                    Conversation.other_user_id == current_user.id,
                    event_filter
                )
            )
        ).first()

        # ---------------------------------------------------------
        # Build response
        # ---------------------------------------------------------

        def build_conversation_response(conv, target_user_id):
            """
            Build conversation response from target user's perspective.
            """

            print(
                f"\n📦 Building response "
                f"for conversation {conv.id}"
            )

            print(f"   - Conversation parent_id: {conv.parent_id}")
            print(
                f"   - Conversation other_user_id: "
                f"{conv.other_user_id}"
            )
            print(f"   - Target user: {target_user_id}")
            print(f"   - Event ID: {conv.event_id}")

            # If target is the parent, other_user is the partner.
            # Otherwise parent is the partner.
            if conv.parent_id == target_user_id:
                other_user = conv.other_user
            else:
                other_user = conv.parent

            if not other_user:
                raise ValueError(
                    f"Could not resolve other user "
                    f"for conversation {conv.id}"
                )

            print(
                f"   - Other user in response: "
                f"{other_user.id}"
            )

            # -----------------------------------------------------
            # Name
            # -----------------------------------------------------

            other_name = ""

            if other_user.parent_profile:
                first_name = (
                    other_user.parent_profile.first_name or ""
                )
                last_name = (
                    other_user.parent_profile.last_name or ""
                )

                other_name = (
                    f"{first_name} {last_name}"
                ).strip()

            # -----------------------------------------------------
            # Profile image
            # -----------------------------------------------------

            other_image = ""

            if (
                other_user.parent_profile
                and other_user.parent_profile.images
            ):
                if len(other_user.parent_profile.images) > 0:
                    other_image = (
                        other_user
                        .parent_profile
                        .images[0]
                        .image_url
                        or ""
                    )

            # -----------------------------------------------------
            # Response
            # -----------------------------------------------------

            response = {
                'conversationId': conv.id,
                'otherUserId': other_user.id,
                'otherUserName': (
                    other_name
                    or other_user.email
                ),
                'otherUserImage': other_image,
                'eventId': conv.event_id
            }

            print(f"   - Response: {response}")

            return response

        # ---------------------------------------------------------
        # Existing conversation
        # ---------------------------------------------------------

        if existing:
            print(
                f"\n✅ FOUND EXISTING CONVERSATION "
                f"{existing.id}"
            )

            response = build_conversation_response(
                existing,
                current_user.id
            )

            print(f"{'=' * 60}")
            print("Returning 200 OK")
            print(f"{'=' * 60}\n")

            return jsonify(response), 200

        # ---------------------------------------------------------
        # Create new conversation
        # ---------------------------------------------------------

        print(
            f"\n➕ Creating NEW conversation "
            f"for user {current_user.id} "
            f"↔ {other_user_id}"
        )

        conversation = Conversation(
            parent_id=current_user.id,
            other_user_id=other_user_id,
            event_id=event_id
        )

        print(
            "   - New conversation object created "
            "(not yet in DB)"
        )
        print(
            f"   - parent_id: "
            f"{conversation.parent_id}"
        )
        print(
            f"   - other_user_id: "
            f"{conversation.other_user_id}"
        )
        print(
            f"   - event_id: "
            f"{conversation.event_id}"
        )

        db.session.add(conversation)
        db.session.commit()

        print(
            f"✅ Conversation saved to DB "
            f"with ID: {conversation.id}"
        )

        response = build_conversation_response(
            conversation,
            current_user.id
        )

        print(f"{'=' * 60}")
        print("Returning 201 CREATED")
        print(f"{'=' * 60}\n")

        return jsonify(response), 201

    except Exception as e:
        db.session.rollback()

        print(f"\n{'=' * 60}")
        print(f"❌ ERROR in start_conversation: {e}")
        print(f"{'=' * 60}")

        import traceback
        traceback.print_exc()

        return jsonify({
            'error': str(e)
        }), 500


  
@app.route('/conversations/<int:conversation_id>/messages', methods=['GET'])
def get_messages(conversation_id):
    """
    Get message history for a conversation.
    """
    current_user = get_current_user_from_token()

    if not current_user:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        # SQLAlchemy 2.x style
        conversation = db.session.get(
            Conversation,
            conversation_id
        )

        if not conversation:
            return jsonify({
                'error': 'Conversation not found'
            }), 404

        # Verify user is part of this conversation
        if (
            conversation.parent_id != current_user.id
            and conversation.other_user_id != current_user.id
        ):
            return jsonify({
                'error': 'Unauthorized'
            }), 403

        page = request.args.get(
            'page',
            1,
            type=int
        )

        per_page = min(
            request.args.get(
                'per_page',
                50,
                type=int
            ),
            100
        )

        if page < 1:
            page = 1

        query = Message.query.filter_by(
            conversation_id=conversation_id
        )

        total_count = query.count()

        messages_page = query.order_by(
            Message.timestamp.desc()
        ).paginate(
            page=page,
            per_page=per_page,
            error_out=False
        )

        # Mark received messages as read
        for msg in messages_page.items:
            if (
                msg.receiver_id == current_user.id
                and not msg.is_read
            ):
                msg.is_read = True

        db.session.commit()

        # Database query is newest -> oldest.
        # Return oldest -> newest to the client.
        messages_data = [
            m.to_dict()
            for m in reversed(messages_page.items)
        ]

        return jsonify({
            'messages': messages_data,
            'total': total_count,
            'pages': messages_page.pages,
            'currentPage': page,
            'perPage': per_page
        }), 200

    except Exception as e:
        db.session.rollback()

        print(f"ERROR in get_messages: {e}")

        import traceback
        traceback.print_exc()

        return jsonify({
            'error': str(e)
        }), 500


@socketio.on('send_message')
def handle_send_message(data):
    """
    Handle real-time message sending via Socket.IO.
    Saves to database and emits to recipient.
    """
    print(f"\n{'='*60}")
    print(f"💬 MESSAGE SEND REQUEST")
    print(f"{'='*60}")

    try:
        # ---------------------------------------------------------
        # Find current user from active Socket.IO connection
        # ---------------------------------------------------------
        sid = request.sid
        current_user_id = None

        for user_id, user_sid in active_connections.items():
            if user_sid == sid:
                current_user_id = user_id
                break

        if not current_user_id:
            print(f"❌ FAILED: Unknown sender (sid: {sid})")
            emit('error', {
                'message': 'Unauthorized - connection not authenticated'
            })
            return

        print(f"👤 Sender: {current_user_id}")

        # ---------------------------------------------------------
        # Parse message data
        # ---------------------------------------------------------
        conversation_id = data.get('conversationId')
        receiver_id = data.get('receiverId')
        message_text = data.get('message')
        reply_to_id = data.get('replyToId')
        image_url = data.get('imageUrl')

        print(
            f"📝 Data: convo={conversation_id}, "
            f"receiver={receiver_id}, "
            f"msg_len={len(message_text) if message_text else 0}"
        )

        # ---------------------------------------------------------
        # Validate required fields
        # ---------------------------------------------------------
        if not conversation_id or not receiver_id or not message_text:
            print(f"❌ VALIDATION FAILED: Missing required fields")

            emit('error', {
                'message': 'conversationId, receiverId, and message are required'
            })
            return

        # ---------------------------------------------------------
        # Get conversation
        # Use db.session.get() instead of Query.get()
        # ---------------------------------------------------------
        conversation = db.session.get(Conversation, conversation_id)

        if not conversation:
            print(f"❌ FAILED: Conversation {conversation_id} not found")

            emit('error', {
                'message': 'Conversation not found'
            })
            return

        # ---------------------------------------------------------
        # Verify current user belongs to conversation
        #
        # Conversation uses:
        #   parent_id
        #   other_user_id
        #
        # NOT:
        #   user_id
        # ---------------------------------------------------------
        if (
            conversation.parent_id != current_user_id
            and conversation.other_user_id != current_user_id
        ):
            print(
                f"❌ FAILED: User {current_user_id} "
                f"not part of conversation"
            )

            emit('error', {
                'message': 'Unauthorized - not part of this conversation'
            })
            return

        # ---------------------------------------------------------
        # Verify receiver is the OTHER participant
        # ---------------------------------------------------------
        if current_user_id == conversation.parent_id:
            expected_receiver_id = conversation.other_user_id
        else:
            expected_receiver_id = conversation.parent_id

        if receiver_id != expected_receiver_id:
            print(
                f"❌ FAILED: Invalid receiver. "
                f"Expected {expected_receiver_id}, got {receiver_id}"
            )

            emit('error', {
                'message': 'Receiver is not the other participant in this conversation'
            })
            return

        # ---------------------------------------------------------
        # Create message
        # ---------------------------------------------------------
        message = Message(
            conversation_id=conversation_id,
            sender_id=current_user_id,
            receiver_id=receiver_id,
            message=message_text,
            reply_to_id=reply_to_id,
            image_url=image_url,
        )

        db.session.add(message)

        # Update conversation timestamp
        conversation.updated_at = datetime.now(timezone.utc)

        db.session.commit()

        print(f"✅ Message {message.id} saved to database")

        # ---------------------------------------------------------
        # Build response
        # ---------------------------------------------------------
        message_response = {
            'id': message.id,
            'conversationId': conversation_id,
            'senderId': current_user_id,
            'receiverId': receiver_id,
            'message': message_text,
            'imageUrl': image_url,
            'replyToId': reply_to_id,
            'timestamp': message.timestamp.isoformat(),
            'isRead': False
        }

        # ---------------------------------------------------------
        # Confirm to sender
        # ---------------------------------------------------------
        emit('message_sent', message_response)

        print(f"✅ Sent confirmation to sender")

        # ---------------------------------------------------------
        # Send to receiver if online
        # ---------------------------------------------------------
        if receiver_id in active_connections:

            receiver_sid = active_connections[receiver_id]

            print(
                f"📤 Receiver {receiver_id} is online "
                f"(sid: {receiver_sid})"
            )

            socketio.emit(
                'new_message',
                message_response,
                room=receiver_sid
            )

            print(f"✅ Emitted new_message to receiver")

        else:
            print(
                f"⚠️ Receiver {receiver_id} is offline "
                f"(message saved)"
            )

        print(f"{'='*60}\n")

    except Exception as e:
        print(f"❌ ERROR in handle_send_message: {e}")

        import traceback
        traceback.print_exc()

        db.session.rollback()

        emit('error', {
            'message': f'Error sending message: {str(e)}'
        })



def build_conversation_response(conv, target_user_id):
    """
    Build the response DTO for a conversation.
    """

    # Determine the other participant
    if conv.parent_id == target_user_id:
        other_user = conv.other_user
    else:
        other_user = conv.parent

    if not other_user:
        raise ValueError(
            f"Could not resolve other user "
            f"for conversation {conv.id}"
        )

    other_name = ""

    if other_user.parent_profile:
        first_name = (
            other_user.parent_profile.first_name
            or ""
        )

        last_name = (
            other_user.parent_profile.last_name
            or ""
        )

        other_name = (
            f"{first_name} {last_name}"
        ).strip()

    other_image = ""

    if (
        other_user.parent_profile
        and other_user.parent_profile.images
    ):
        if len(other_user.parent_profile.images) > 0:
            other_image = (
                other_user
                .parent_profile
                .images[0]
                .image_url
                or ""
            )

    return {
        'conversationId': conv.id,
        'otherUserId': other_user.id,
        'otherUserName': (
            other_name
            or other_user.email
        ),
        'otherUserImage': other_image,
        'eventId': conv.event_id
    }
    


# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────
 
def get_online_status(user_id):
    """Check if a user is currently online."""
    return user_id in active_connections
 
 
def get_active_users_count():
    """Get total count of active connections."""
    return len(active_connections)

    
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)