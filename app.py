from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Channel, Message, VoiceSession
import os
import numpy as np
import time
from datetime import datetime
from collections import defaultdict, deque

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///voicechat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Constants for message loading
INITIAL_MESSAGE_LOAD_COUNT = 20
OLDER_MESSAGE_LOAD_COUNT = 20

# --- 音频优化配置 ---
STANDARD_SAMPLERATE = 48000  # 统一采样率
STANDARD_CHANNELS = 1        # 单声道
AUDIO_CHUNK_SIZE = 960       # 20ms @ 48kHz
MAX_AUDIO_BUFFER_SIZE = 5    # 每用户最大缓冲音频块数
VOICE_ACTIVITY_TIMEOUT = 3.0 # 语音活动超时时间

# 音频缓冲和带宽优化
audio_buffers = defaultdict(lambda: deque(maxlen=MAX_AUDIO_BUFFER_SIZE))  # user_id: deque of audio chunks
user_last_activity = defaultdict(float)  # user_id: last_activity_time
active_speakers = defaultdict(set)  # channel_id: set of active user_ids

def optimize_audio_chunk(audio_data, original_samplerate=None):
    """
    优化音频数据块，减少带宽并确保质量
    """
    try:
        if not audio_data or not isinstance(audio_data, (list, np.ndarray)):
            return None
            
        # 转换为numpy数组
        audio_np = np.array(audio_data, dtype=np.float32)
        
        # 基本音频质量检查
        if len(audio_np) == 0:
            return None
            
        # 移除过小的音频信号（噪声抑制）
        rms = np.sqrt(np.mean(audio_np**2))
        if rms < 0.001:  # 静音阈值
            return None
            
        # 规范化音频
        max_val = np.max(np.abs(audio_np))
        if max_val > 1.0:
            audio_np = audio_np / max_val
            
        # 确保块大小合理（带宽优化）
        if len(audio_np) > AUDIO_CHUNK_SIZE * 2:  # 如果块太大，截断
            audio_np = audio_np[:AUDIO_CHUNK_SIZE]
        elif len(audio_np) < AUDIO_CHUNK_SIZE // 4:  # 如果块太小，填充或丢弃
            return None
            
        # 简单的降噪（移除小幅度信号）
        noise_floor = 0.01
        audio_np = np.where(np.abs(audio_np) < noise_floor, 0, audio_np)
        
        return audio_np.tolist()
        
    except Exception as e:
        print(f"Error optimizing audio chunk: {e}")
        return None

def should_forward_audio(user_id, channel_id):
    """
    判断是否应该转发音频（基于活动状态和带宽优化）
    """
    current_time = time.time()
    
    # 更新用户活动时间
    user_last_activity[user_id] = current_time
    
    # 将用户添加到活跃说话者列表
    active_speakers[channel_id].add(user_id)
    
    # 清理过期的活跃说话者
    expired_users = []
    for speaker_id in active_speakers[channel_id]:
        if current_time - user_last_activity[speaker_id] > VOICE_ACTIVITY_TIMEOUT:
            expired_users.append(speaker_id)
    
    for expired_user in expired_users:
        active_speakers[channel_id].discard(expired_user)
        if expired_user in audio_buffers:
            audio_buffers[expired_user].clear()
    
    # 限制同时活跃说话者数量（带宽控制）
    max_concurrent_speakers = 4
    if len(active_speakers[channel_id]) > max_concurrent_speakers:
        # 保留最近活动的说话者
        sorted_speakers = sorted(
            active_speakers[channel_id], 
            key=lambda uid: user_last_activity[uid], 
            reverse=True
        )
        active_speakers[channel_id] = set(sorted_speakers[:max_concurrent_speakers])
        
        # 如果当前用户不在活跃列表中，不转发
        if user_id not in active_speakers[channel_id]:
            return False
    
    return True

def compress_audio_for_transmission(audio_data):
    """
    为传输压缩音频数据（简单的量化和舍入）
    """
    try:
        if not audio_data:
            return audio_data
            
        # 减少精度以节省带宽（从float32到16位精度）
        audio_np = np.array(audio_data, dtype=np.float32)
        
        # 量化到16位精度
        quantized = np.round(audio_np * 32767) / 32767
        
        # 移除非常小的值
        threshold = 1.0 / 32767
        quantized = np.where(np.abs(quantized) < threshold, 0, quantized)
        
        return quantized.tolist()
        
    except Exception as e:
        print(f"Error compressing audio: {e}")
        return audio_data

# 初始化扩展
db.init_app(app)
socketio = SocketIO(app)
login_manager = LoginManager(app)

# 全局存储连接的用户状态 (user_id: {username, sid, online, avatar_url, is_admin})
connected_users = {}

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@login_manager.unauthorized_handler
def unauthorized():
    # Return a 401 Unauthorized response for API clients
    return jsonify(success=False, message="Authentication required"), 401

# 创建初始数据 (移除 @app.before_first_request)
def create_initial_data():
    # db.create_all() # 这行通常在 app context 外部执行，或者通过 flask db init/migrate
    # 确保在 app_context 内执行
    if not Channel.query.first():
        general_text = Channel(name="general", channel_type="text", is_private=False)
        general_voice = Channel(name="voice-lobby", channel_type="voice", is_private=False)
        db.session.add(general_text)
        db.session.add(general_voice)
        db.session.commit()
        print("Default channels created.")
    else:
        print("Default channels already exist.")

# 路由: 登录 API
@app.route('/api/login', methods=['POST'])
def login_api():
    data = request.get_json()
    if not data or not data.get('username') or not data.get('password'):
        return jsonify(success=False, message="Username and password required"), 400
    
    user = User.query.filter_by(username=data.get('username')).first()
    if user and check_password_hash(user.password, data.get('password')):
        login_user(user)
        # TODO: Consider session management/token for desktop app if needed beyond SocketIO auth
        return jsonify(
            success=True, 
            user={'id': user.id, 'username': user.username, 'avatar_url': user.avatar_url, 'is_admin': user.is_admin}
        )
    return jsonify(success=False, message='用户名或密码错误'), 401

# 路由: 注册 API
@app.route('/api/register', methods=['POST'])
def register_api():
    data = request.get_json()
    if not data:
        return jsonify(success=False, message="Request body cannot be empty"), 400

    username = data.get('username')
    password = data.get('password')
    invite_code = data.get('invite_code')

    if not username or not password:
        return jsonify(success=False, message="Username and password required"), 400
    
    # For simplicity, invite code check can be basic for now
    if invite_code != 'ARC2015': 
        return jsonify(success=False, message='邀请码错误'), 400
            
    if User.query.filter_by(username=username).first():
        return jsonify(success=False, message='用户名已存在'), 409 # 409 Conflict
            
    hashed_password = generate_password_hash(password)
    new_user = User(username=username, password=hashed_password)
    db.session.add(new_user)
    db.session.commit()
    return jsonify(success=True, message='注册成功，请登录'), 201

# 路由: 登出 API
@app.route('/api/logout', methods=['POST'])
@login_required
def logout_api():
    logout_user()
    return jsonify(success=True, message='登出成功')

# Endpoint to get current user info
@app.route('/api/users/me', methods=['GET'])
@login_required
def get_current_user_api():
    return jsonify(
        id=current_user.id,
        username=current_user.username,
        avatar_url=current_user.avatar_url,
        is_admin=current_user.is_admin,
        auto_join_voice=current_user.auto_join_voice # Assuming this field exists on User model
    )

# Endpoint to get channels
@app.route('/api/channels', methods=['GET'])
@login_required
def get_channels_api():
    text_channels_query = Channel.query.filter_by(channel_type='text')
    voice_channels_query = Channel.query.filter_by(channel_type='voice')

    if not current_user.is_admin:
        text_channels_list = [ch for ch in text_channels_query.all() if not ch.is_private or current_user in ch.members]
        voice_channels_list = [ch for ch in voice_channels_query.all() if not ch.is_private or current_user in ch.members]
    else:
        text_channels_list = text_channels_query.all()
        voice_channels_list = voice_channels_query.all()
    
    return jsonify(
        text_channels=[{'id': ch.id, 'name': ch.name, 'is_private': ch.is_private} for ch in text_channels_list],
        voice_channels=[{'id': ch.id, 'name': ch.name, 'is_private': ch.is_private} for ch in voice_channels_list]
    )

# API: Update user settings
@app.route('/api/settings', methods=['POST'])
@login_required
def update_settings_api():
    data = request.get_json()
    if not data:
        return jsonify(success=False, message="Request body cannot be empty"), 400

    # Basic validation
    if 'avatar_url' in data:
        current_user.avatar_url = data.get('avatar_url')
    if 'auto_join_voice' in data and isinstance(data.get('auto_join_voice'), bool):
        current_user.auto_join_voice = data.get('auto_join_voice')
    
    # Add more specific validation as needed
    # Example: check if avatar_url is a valid URL format

    try:
        db.session.commit()
        return jsonify(
            success=True, 
            message='设置已成功保存', 
            user={
                'id': current_user.id,
                'username': current_user.username,
                'avatar_url': current_user.avatar_url, 
                'is_admin': current_user.is_admin,
                'auto_join_voice': current_user.auto_join_voice
            }
        )
    except Exception as e:
        db.session.rollback()
        return jsonify(success=False, message=f"更新设置失败: {str(e)}"), 500

# API: Create a new channel (Admin only)
@app.route('/api/channels', methods=['POST']) # Changed from /api/admin/channels for consistency with GET /api/channels
@login_required
def create_channel_api():
    if not current_user.is_admin:
        return jsonify(success=False, message='只有管理员可以创建频道'), 403

    data = request.get_json()
    if not data:
        return jsonify(success=False, message="Request body cannot be empty"), 400

    name = data.get('name')
    channel_type = data.get('channel_type')
    is_private = data.get('is_private', False) # Default to public if not provided

    if not name or not channel_type:
        return jsonify(success=False, message="频道名称和类型是必需的"), 400
    
    if channel_type not in ['text', 'voice']:
        return jsonify(success=False, message="无效的频道类型"), 400
    
    if not isinstance(is_private, bool):
        return jsonify(success=False, message="is_private 必须是布尔值"), 400

    new_channel = Channel(
        name=name,
        channel_type=channel_type,
        is_private=is_private
    )
    db.session.add(new_channel)
    
    # If private, admin creator is automatically a member
    if new_channel.is_private:
        if current_user not in new_channel.members: # Should always be true for a new channel
             new_channel.members.append(current_user)
            
    try:
        db.session.commit()
        return jsonify(
            success=True, 
            message='频道创建成功', 
            channel={
                'id': new_channel.id, 
                'name': new_channel.name, 
                'channel_type': new_channel.channel_type,
                'is_private': new_channel.is_private
            }
        ), 201
    except Exception as e:
        db.session.rollback()
        return jsonify(success=False, message=f"创建频道失败: {str(e)}"), 500

# WebSocket: 连接事件
@socketio.on('connect')
def handle_connect():
    if current_user.is_authenticated:
        join_room(f"user_{current_user.id}") # User joins their own room for direct messages/signals
        print(f"User {current_user.username} (ID: {current_user.id}, SID: {request.sid}) connected and joined room user_{current_user.id}")
        
        # 更新或添加用户到 connected_users
        connected_users[current_user.id] = {
            'username': current_user.username,
            'sid': request.sid,
            'online': True,
            'avatar_url': current_user.avatar_url, # Store avatar for rich presence
            'is_admin': current_user.is_admin # Store admin status if needed for display
        }
        
        # 向所有客户端广播更新的用户列表
        emit('server_user_list_update', list(connected_users.values()), broadcast=True)
        
        # 也单独给当前连接的用户发送一次完整的列表 (以防万一广播稍早于其准备好接收)
        # emit('server_user_list_update', list(connected_users.values()), room=request.sid)
        # The broadcast=True should cover the new user as well, if they are ready to receive.
        # A more robust way is to send it specifically to the new user after they signal readiness or here.
        # For now, relying on the broadcast.

        # emit('user_connected', {'user_id': current_user.id, 'username': current_user.username}, broadcast=True) # This is now covered by server_user_list_update
    else:
        print(f"Unauthenticated connection attempt from SID: {request.sid}")
        return False # Disconnect unauthenticated users

# WebSocket: 断开连接事件
@socketio.on('disconnect')
def handle_disconnect():
    if current_user.is_authenticated and current_user.id in connected_users:
        print(f"User {connected_users[current_user.id]['username']} (ID: {current_user.id}, SID: {request.sid}) disconnected.")
        
        # 清理用户的语音会话
        session = VoiceSession.query.filter_by(user_id=current_user.id).first()
        if session:
            channel_id_being_left = session.channel_id
            # Inform others in the voice channel about leaving
            emit('user_left_voice', {
                'channel_id': channel_id_being_left, 
                'user_id': current_user.id,
                'username': connected_users[current_user.id]['username'] # Include username for consistency
            }, room=f"voice_channel_{channel_id_being_left}")
            
            db.session.delete(session)
            db.session.commit()
            print(f"Cleaned up voice session for user {current_user.id} from channel {channel_id_being_left}")

        # 清理音频相关状态
        if current_user.id in audio_buffers:
            audio_buffers[current_user.id].clear()
            del audio_buffers[current_user.id]
        
        if current_user.id in user_last_activity:
            del user_last_activity[current_user.id]
            
        # 从所有频道的活跃说话者列表中移除
        for channel_speakers in active_speakers.values():
            channel_speakers.discard(current_user.id)

        if current_user.id in connected_users:
             del connected_users[current_user.id]

        emit('server_user_list_update', list(connected_users.values()), broadcast=True)
    else:
        print(f"Disconnect event for an unauthenticated or unknown user. SID: {request.sid}")

# WebSocket: 加入文字频道
@socketio.on('join_text_channel')
def handle_join_text_channel(data):
    channel_id = data['channel_id']
    join_room(f"text_channel_{channel_id}")

    # Fetch initial batch of messages (most recent ones)
    historical_messages_query = Message.query.filter_by(channel_id=channel_id)\
                                            .order_by(Message.timestamp.desc())\
                                            .limit(INITIAL_MESSAGE_LOAD_COUNT)\
                                            .all()
    
    # Messages are fetched in descending order (newest first), reverse them for chronological display
    historical_messages_query.reverse() 

    formatted_messages = []
    for msg in historical_messages_query:
        sender = User.query.get(msg.user_id)
        formatted_messages.append({
            'channel_id': msg.channel_id,
            'message_id': msg.id, # Important for fetching older messages
            'content': msg.content,
            'username': sender.username if sender else 'Unknown User',
            'user_id': msg.user_id,
            'avatar_url': sender.avatar_url if sender else None,
            'timestamp': msg.timestamp.strftime('%H:%M:%S'),
            'timestamp_iso': msg.timestamp.isoformat() # Full ISO timestamp for precise comparison
        })
    
    # Check if there might be more older messages
    total_messages_in_channel = Message.query.filter_by(channel_id=channel_id).count()
    has_more_older = total_messages_in_channel > len(formatted_messages)

    emit('load_historical_messages', {
        'channel_id': channel_id,
        'messages': formatted_messages,
        'has_more_older': has_more_older
    }, room=request.sid)

    print(f"User {current_user.username} joined text channel {channel_id}, sent {len(formatted_messages)} initial messages. Has more: {has_more_older}")

@socketio.on('request_older_messages')
def handle_request_older_messages(data):
    if not current_user.is_authenticated:
        return

    channel_id = data.get('channel_id')
    before_message_id = data.get('before_message_id') # Client should send the ID of the oldest message it has
    # Alternatively, client could send `before_timestamp_iso`
    limit_count = data.get('limit', OLDER_MESSAGE_LOAD_COUNT)

    if not channel_id or not before_message_id:
        emit('error', {'message': 'Channel ID and before_message_id are required to load older messages.'}, room=request.sid)
        return

    oldest_message_on_client = Message.query.get(before_message_id)
    if not oldest_message_on_client:
        emit('older_messages_loaded', {
            'channel_id': channel_id,
            'messages': [],
            'has_more_older': False # Cannot find the reference message
        }, room=request.sid)
        return

    older_messages_query = Message.query.filter(
                                        Message.channel_id == channel_id,
                                        Message.timestamp < oldest_message_on_client.timestamp
                                    )\
                                    .order_by(Message.timestamp.desc())\
                                    .limit(limit_count)\
                                    .all()
    
    older_messages_query.reverse() # Reverse for chronological order

    formatted_older_messages = []
    for msg in older_messages_query:
        sender = User.query.get(msg.user_id)
        formatted_older_messages.append({
            'channel_id': msg.channel_id,
            'message_id': msg.id,
            'content': msg.content,
            'username': sender.username if sender else 'Unknown User',
            'user_id': msg.user_id,
            'avatar_url': sender.avatar_url if sender else None,
            'timestamp': msg.timestamp.strftime('%H:%M:%S'),
            'timestamp_iso': msg.timestamp.isoformat()
        })

    # Check if there are even more messages older than this batch
    # This check can be more precise by looking for a message older than the oldest one in the current batch sent
    has_even_more_older = False
    if older_messages_query: # If we found any older messages in this batch
        oldest_in_batch_timestamp = older_messages_query[0].timestamp # Since it's chronological now, first is oldest
        more_exist_check = Message.query.filter(
            Message.channel_id == channel_id,
            Message.timestamp < oldest_in_batch_timestamp
        ).first()
        if more_exist_check:
            has_even_more_older = True
            
    emit('older_messages_loaded', {
        'channel_id': channel_id,
        'messages': formatted_older_messages,
        'has_more_older': has_even_more_older 
    }, room=request.sid)
    print(f"Sent {len(formatted_older_messages)} older messages to {current_user.username} for channel {channel_id}. Has more: {has_even_more_older}")

# WebSocket: 发送消息
@socketio.on('send_message')
def handle_message(data):
    channel_id = data['channel_id']
    content = data['message']
    
    target_channel = Channel.query.get(channel_id)
    if not target_channel:
        emit('error', {'message': '频道不存在'})
        return

    # 权限检查: 发送消息
    if target_channel.is_private and not current_user.is_admin and current_user not in target_channel.members:
        emit('error', {'message': '您没有权限在此私有频道发送消息'})
        return

    # 保存消息到数据库
    new_message = Message(
        content=content,
        timestamp=datetime.utcnow(),
        user_id=current_user.id,
        channel_id=channel_id
    )
    db.session.add(new_message)
    db.session.commit()
    
    # 广播消息
    emit('new_message', {
        'channel_id': channel_id,
        'message_id': new_message.id,
        'content': content,
        'username': current_user.username,
        'user_id': current_user.id,
        'avatar_url': current_user.avatar_url,
        'timestamp': new_message.timestamp.strftime('%H:%M:%S')
    }, room=f"text_channel_{channel_id}")

# WebSocket: 加入语音频道
@socketio.on('join_voice_channel')
def handle_join_voice_channel(data):
    channel_id = data.get('channel_id')
    if not channel_id:
        emit('error', {'message': 'Channel ID missing in join_voice_channel request'})
        return

    target_channel = Channel.query.get(channel_id)
    if not target_channel:
        emit('error', {'message': '语音频道不存在'})
        return
    if target_channel.channel_type != 'voice':
        emit('error', {'message': '目标频道不是语音频道'})
        return
    if target_channel.is_private and not current_user.is_admin and current_user not in target_channel.members:
        emit('error', {'message': '您没有权限加入此私有语音频道'})
        return

    existing_session = VoiceSession.query.filter_by(user_id=current_user.id).first()
    user_was_already_in_target_channel = False

    if existing_session:
        if existing_session.channel_id != channel_id:
            old_channel_id = existing_session.channel_id
            leave_room(f"voice_channel_{old_channel_id}")
            db.session.delete(existing_session)
            emit('user_left_voice', {
                'channel_id': old_channel_id,
                'user_id': current_user.id,
                'username': current_user.username 
            }, room=f"voice_channel_{old_channel_id}")
            print(f"User {current_user.username} left old voice channel {old_channel_id} before joining {channel_id}")
        else: # User is already in the target channel's session
            user_was_already_in_target_channel = True
            print(f"User {current_user.username} is re-joining/refreshing voice channel {channel_id}")

    if not user_was_already_in_target_channel:
        new_session = VoiceSession(user_id=current_user.id, channel_id=channel_id)
        db.session.add(new_session)
    
    db.session.commit()
    
    join_room(f"voice_channel_{channel_id}")
    
    users_in_channel_q = VoiceSession.query.filter_by(channel_id=channel_id).all()
    user_list = [{'user_id': s.user.id, 'username': s.user.username, 'avatar_url': s.user.avatar_url} for s in users_in_channel_q if s.user]
    
    emit('voice_channel_users', {
        'channel_id': channel_id,
        'users': user_list
    }, room=request.sid)

    if not user_was_already_in_target_channel:
        emit('user_joined_voice', {
            'channel_id': channel_id,
            'user_id': current_user.id,
            'username': current_user.username,
            'avatar_url': current_user.avatar_url
        }, room=f"voice_channel_{channel_id}", skip_sid=request.sid)
        print(f"User {current_user.username} newly joined voice channel {channel_id}. SID: {request.sid}")
    else:
        # Optionally, if user was already in channel, we might want to inform them their "rejoin" was processed
        # For now, sending voice_channel_users is the primary feedback.
        print(f"User {current_user.username} re-confirmed in voice channel {channel_id}. SID: {request.sid}")

# WebSocket: 离开语音频道
@socketio.on('leave_voice_channel')
def handle_leave_voice_channel(data): # Expecting data to contain channel_id from client
    # It's safer for client to tell which channel it *thinks* it's leaving
    channel_id_from_client = data.get('channel_id') 

    session = VoiceSession.query.filter_by(user_id=current_user.id).first()
    if session:
        # If client specified a channel_id, ensure it matches the one in DB for this user
        # This adds a layer of safety but can be simplified if we trust the client or VoiceSession is the sole truth
        if channel_id_from_client is not None and session.channel_id != channel_id_from_client:
            print(f"Warning: User {current_user.username} attempting to leave voice channel {channel_id_from_client} but DB session says {session.channel_id}")
            # Potentially emit error back to client or just use DB session.channel_id
            # For now, we'll trust the DB session as the source of truth for which channel they were in.
        
        channel_id_to_leave = session.channel_id # Use channel_id from DB session

        leave_room(f"voice_channel_{channel_id_to_leave}")
        db.session.delete(session)
        db.session.commit()
        
        emit('user_left_voice', {
            'channel_id': channel_id_to_leave,
            'user_id': current_user.id
        }, room=f"voice_channel_{channel_id_to_leave}")
        print(f"User {current_user.username} left voice channel {channel_id_to_leave}. SID: {request.sid}")
    else:
        # User was not in any voice session according to DB, maybe client state was out of sync.
        # If client sent a channel_id, we could still try to emit to that room if we want, but it's less clean.
        if channel_id_from_client:
            print(f"User {current_user.username} requested leave for voice channel {channel_id_from_client}, but no active session found in DB.")
        else:
            print(f"User {current_user.username} requested leave_voice_channel but no active session found and no channel_id provided.")

# WebSocket: 接收客户端的麦克风状态更新 (是否静音)
@socketio.on('user_microphone_status')
def handle_user_microphone_status(data):
    channel_id = data.get('channel_id')
    is_unmuted = data.get('is_unmuted')
    user_id = current_user.id

    if channel_id is not None and is_unmuted is not None and user_id is not None:
        room_name = f"voice_channel_{channel_id}"
        print(f"[USER_MICROPHONE_STATUS] User {user_id} is_unmuted: {is_unmuted} in channel {channel_id} (room: {room_name})") 
        
        # Broadcast the updated mic status to all clients in the room (including sender)
        emit('user_mic_status_updated',
             {'channel_id': channel_id, 'user_id': user_id, 'is_unmuted': is_unmuted},
             room=room_name)

# WebSocket: 接收并转发语音数据流
@socketio.on('voice_data_stream')
def handle_voice_data_stream(data):
    if not current_user.is_authenticated:
        print("[VOICE_DATA_STREAM] Received voice data from unauthenticated user.")
        return

    channel_id = data.get('channel_id')
    audio_data = data.get('audio_data')  # List of floats (samples)
    client_samplerate = data.get('samplerate', STANDARD_SAMPLERATE)
    client_channels = data.get('channels', STANDARD_CHANNELS)
    client_dtype = data.get('dtype', 'float32')
    
    user_id = current_user.id
    username = current_user.username

    if channel_id is None or audio_data is None:
        print(f"[VOICE_DATA_STREAM] Missing channel_id or audio_data for user {user_id}. Discarding.")
        return
    
    # 检查是否应该转发音频（带宽和活动控制）
    if not should_forward_audio(user_id, channel_id):
        # print(f"[VOICE_DATA_STREAM] Skipping audio forward for user {user_id} due to activity limits.")
        return
    
    # 音频格式验证和标准化
    if client_samplerate != STANDARD_SAMPLERATE:
        print(f"[VOICE_DATA_STREAM] Warning: Client {user_id} using non-standard samplerate {client_samplerate}Hz")
    
    if client_channels != STANDARD_CHANNELS:
        print(f"[VOICE_DATA_STREAM] Warning: Client {user_id} using {client_channels} channels, expected {STANDARD_CHANNELS}")
    
    # 优化音频数据
    optimized_audio = optimize_audio_chunk(audio_data, client_samplerate)
    if optimized_audio is None:
        # 静音或无效数据，不转发
        return
    
    # 压缩音频以节省带宽
    compressed_audio = compress_audio_for_transmission(optimized_audio)
    
    # 添加到用户音频缓冲区（防止音频丢失）
    audio_buffers[user_id].append({
        'audio_data': compressed_audio,
        'timestamp': time.time(),
        'channel_id': channel_id
    })
    
    room_name = f"voice_channel_{channel_id}"
    
    # 记录优化效果
    original_size = len(audio_data) if audio_data else 0
    compressed_size = len(compressed_audio) if compressed_audio else 0
    # print(f"[VOICE_DATA_STREAM] User {user_id} audio: {original_size} -> {compressed_size} samples (channel {channel_id})")

    # 1. 广播用户语音活动状态
    emit('user_voice_activity', 
         {'channel_id': channel_id, 'user_id': user_id, 'username': username, 'active': True}, 
         room=room_name)

    # 2. 转发优化后的音频数据，包含标准化的元数据
    emit('voice_data_stream_chunk', 
         {
             'channel_id': channel_id, 
             'user_id': user_id, 
             'username': username, 
             'audio_data': compressed_audio,
             'samplerate': STANDARD_SAMPLERATE,  # 强制标准采样率
             'channels': STANDARD_CHANNELS,      # 强制单声道
             'dtype': 'float32',                 # 标准数据类型
             'chunk_size': len(compressed_audio),
             'server_optimized': True            # 标记为服务端优化过的数据
         }, 
         room=room_name, 
         skip_sid=request.sid)

# WebSocket: WebRTC信令
@socketio.on('voice_signal')
def handle_voice_signal(data):
    recipient_id = data['recipient_id']
    
    # 检查接收者是否在线
    recipient_session = VoiceSession.query.filter_by(user_id=recipient_id).first()
    if recipient_session:
        # 添加发送者信息
        data['sender_id'] = current_user.id
        data['sender_name'] = current_user.username
        # Emit to a user-specific room for WebRTC signaling
        emit('voice_signal', data, room=f"user_{recipient_id}")

# API: Get all users (Admin only)
@app.route('/api/admin/users', methods=['GET'])
@login_required
def get_all_users_api():
    if not current_user.is_admin:
        return jsonify(success=False, message='仅限管理员访问'), 403
    
    users = User.query.all()
    return jsonify(success=True, users=[{'id': u.id, 'username': u.username, 'is_admin': u.is_admin, 'avatar_url': u.avatar_url} for u in users])

# API: Toggle admin status for a user (Admin only)
@app.route('/api/admin/users/<int:user_id>/toggle_admin', methods=['POST'])
@login_required
def toggle_admin_status_api(user_id):
    if not current_user.is_admin:
        return jsonify(success=False, message='仅限管理员访问'), 403

    user_to_modify = User.query.get(user_id)
    if not user_to_modify:
        return jsonify(success=False, message='用户未找到'), 404

    if user_to_modify.id == current_user.id:
        return jsonify(success=False, message='不能修改自己的管理员状态'), 400

    user_to_modify.is_admin = not user_to_modify.is_admin
    try:
        db.session.commit()
        action = "授予" if user_to_modify.is_admin else "移除"
        return jsonify(success=True, message=f'用户 {user_to_modify.username} 的管理员权限已{action}', user={'id': user_to_modify.id, 'is_admin': user_to_modify.is_admin})
    except Exception as e:
        db.session.rollback()
        return jsonify(success=False, message=f"操作失败: {str(e)}"), 500

# API: Delete a user (Admin only)
@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@login_required
def delete_user_api(user_id):
    if not current_user.is_admin:
        return jsonify(success=False, message='仅限管理员访问'), 403

    user_to_delete = User.query.get(user_id)
    if not user_to_delete:
        return jsonify(success=False, message='用户未找到'), 404

    if user_to_delete.id == current_user.id:
        return jsonify(success=False, message='不能删除自己'), 400

    try:
        # Consider cascading deletes in DB or more robust cleanup
        Message.query.filter_by(user_id=user_to_delete.id).delete()
        VoiceSession.query.filter_by(user_id=user_to_delete.id).delete()
        # Remove user from channel memberships
        for channel in Channel.query.filter(Channel.members.contains(user_to_delete)).all():
            channel.members.remove(user_to_delete)

        db.session.delete(user_to_delete)
        db.session.commit()
        return jsonify(success=True, message=f'用户 {user_to_delete.username} 已被成功删除')
    except Exception as e:
        db.session.rollback()
        return jsonify(success=False, message=f"删除用户失败: {str(e)}"), 500

# API: Edit a channel (Admin only)
@app.route('/api/admin/channels/<int:channel_id>', methods=['PUT']) # Using PUT for update
@login_required
def edit_channel_api(channel_id):
    if not current_user.is_admin:
        return jsonify(success=False, message='仅限管理员访问'), 403

    channel_to_edit = Channel.query.get(channel_id)
    if not channel_to_edit:
        return jsonify(success=False, message='频道未找到'), 404

    data = request.get_json()
    if not data:
        return jsonify(success=False, message="Request body cannot be empty"), 400

    # Basic validation
    if 'name' in data and data.get('name').strip():
        channel_to_edit.name = data.get('name').strip()
    if 'channel_type' in data and data.get('channel_type') in ['text', 'voice']:
        channel_to_edit.channel_type = data.get('channel_type')
    if 'is_private' in data and isinstance(data.get('is_private'), bool):
        is_now_private = data.get('is_private')
        # Logic if channel privacy changes
        if is_now_private and not channel_to_edit.is_private: # Public to Private
            if current_user not in channel_to_edit.members:
                 channel_to_edit.members.append(current_user)
        elif not is_now_private and channel_to_edit.is_private: # Private to Public
            pass # Optional: channel_to_edit.members = [] # Clear members if desired
        channel_to_edit.is_private = is_now_private
        
    try:
        db.session.commit()
        return jsonify(
            success=True, 
            message=f'频道 {channel_to_edit.name} 已成功更新',
            channel={
                'id': channel_to_edit.id, 
                'name': channel_to_edit.name, 
                'channel_type': channel_to_edit.channel_type,
                'is_private': channel_to_edit.is_private
            }
        )
    except Exception as e:
        db.session.rollback()
        return jsonify(success=False, message=f"更新频道失败: {str(e)}"), 500

# API: Delete a channel (Admin only)
@app.route('/api/admin/channels/<int:channel_id>', methods=['DELETE'])
@login_required
def delete_channel_api(channel_id):
    if not current_user.is_admin:
        return jsonify(success=False, message='仅限管理员访问'), 403

    channel_to_delete = Channel.query.get(channel_id)
    if not channel_to_delete:
        return jsonify(success=False, message='频道未找到'), 404

    try:
        # Consider cascading deletes in DB or more robust cleanup
        Message.query.filter_by(channel_id=channel_to_delete.id).delete()
        VoiceSession.query.filter_by(channel_id=channel_to_delete.id).delete()
        # Clear members from the channel
        channel_to_delete.members = []

        db.session.delete(channel_to_delete)
        db.session.commit()
        return jsonify(success=True, message=f'频道 {channel_to_delete.name} 已被成功删除')
    except Exception as e:
        db.session.rollback()
        return jsonify(success=False, message=f"删除频道失败: {str(e)}"), 500

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        create_initial_data()
    
    # 启动 Flask-SocketIO 应用，并启用 SSL
    # 重要: 将 'path/to/your/cert.pem' 和 'path/to/your/key.pem' 替换为您的实际文件路径
    # 例如，如果它们在项目根目录，就是 'cert.pem' 和 'key.pem'
    ssl_context = ('cert.pem', 'key.pem') # 或者 ('ssl/cert.pem', 'ssl/key.pem')
    
    print("Starting server with SSL context...")
    socketio.run(app, 
                 host='0.0.0.0', # 监听所有网络接口
                 port=5005,      # 您希望使用的端口
                 debug=True,     # 开发时可以开启debug
                 ssl_context=ssl_context)
    
    # 如果不使用 Flask-SocketIO 的 run，而是 Flask 自带的 app.run() (不推荐用于 SocketIO)
    # app.run(host='0.0.0.0', port=5000, debug=True, ssl_context=ssl_context) 