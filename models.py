from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

# 辅助表：用于用户和频道之间的多对多关系 (频道成员)
channel_members = db.Table('channel_members',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('channel_id', db.Integer, db.ForeignKey('channel.id'), primary_key=True)
)

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    avatar_url = db.Column(db.String(255), nullable=True)
    auto_join_voice = db.Column(db.Boolean, default=False)
    
    messages = db.relationship('Message', backref='user', lazy=True)
    voice_sessions = db.relationship('VoiceSession', backref='user', lazy=True)
    # 'joined_channels' backref 会由 Channel.members 自动创建

class Channel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    channel_type = db.Column(db.String(10), nullable=False)  # 'text' or 'voice'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_private = db.Column(db.Boolean, default=False, nullable=False) # 新增 is_private 字段
    
    messages = db.relationship('Message', backref='channel', lazy=True)
    voice_sessions = db.relationship('VoiceSession', backref='channel', lazy=True)
    members = db.relationship(
        'User', 
        secondary=channel_members, 
        lazy='subquery', # 或者 'dynamic'，subquery 通常在加载时获取所有成员
        backref=db.backref('joined_channels', lazy=True) # 用户加入的频道列表
    )

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    channel_id = db.Column(db.Integer, db.ForeignKey('channel.id'), nullable=False)

class VoiceSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    channel_id = db.Column(db.Integer, db.ForeignKey('channel.id'), nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_muted = db.Column(db.Boolean, default=False) 