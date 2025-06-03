from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Channel, Message, VoiceSession
import os
import numpy as np
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///voicechat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Constants for message loading
INITIAL_MESSAGE_LOAD_COUNT = 20
OLDER_MESSAGE_LOAD_COUNT = 20

# --- 音频处理配置常量 ---
STANDARD_SAMPLERATE = 48000  # 统一使用48kHz采样率
STANDARD_CHANNELS = 1        # 单声道
STANDARD_DTYPE = 'float32'   # 标准数据类型
MAX_AUDIO_CHUNK_SIZE = 4800  # 最大音频块大小 (100ms @ 48kHz)
MIN_AUDIO_CHUNK_SIZE = 480   # 最小音频块大小 (10ms @ 48kHz)

# --- 音频重采样支持 ---
try:
    import scipy.signal
    SCIPY_AVAILABLE = True
    print("Server: scipy available for high-quality audio resampling")
except ImportError:
    SCIPY_AVAILABLE = False
    print("Server: scipy not available, using simple resampling")

def _resample_audio_server(audio_data, original_rate, target_rate):
    """服务端音频重采样函数"""
    if original_rate == target_rate:
        return audio_data
    
    if not isinstance(audio_data, np.ndarray):
        audio_data = np.array(audio_data, dtype=np.float32)
    
    if SCIPY_AVAILABLE:
        # 使用scipy进行高质量重采样
        num_samples = int(len(audio_data) * target_rate / original_rate)
        return scipy.signal.resample(audio_data, num_samples).astype(np.float32)
    else:
        # 简单的线性插值重采样
        ratio = target_rate / original_rate
        new_length = int(len(audio_data) * ratio)
        indices = np.linspace(0, len(audio_data) - 1, new_length)
        return np.interp(indices, np.arange(len(audio_data)), audio_data).astype(np.float32)

def _validate_and_process_audio(audio_data, metadata):
    """验证和处理音频数据"""
    was_resampled = False
    was_normalized = False
    was_enhanced = False
    had_error = False
    
    try:
        # 验证音频数据
        if not isinstance(audio_data, list) or len(audio_data) == 0:
            had_error = True
            _update_audio_stats(0, had_error=had_error)
            return None, "Invalid audio data format"
        
        original_length = len(audio_data)
        
        # 获取元数据
        chunk_samplerate = metadata.get('samplerate', STANDARD_SAMPLERATE)
        chunk_channels = metadata.get('channels', STANDARD_CHANNELS)
        chunk_dtype = metadata.get('dtype', STANDARD_DTYPE)
        
        # 验证音频块大小
        if len(audio_data) > MAX_AUDIO_CHUNK_SIZE:
            print(f"[AUDIO_VALIDATION] Audio chunk too large: {len(audio_data)} samples, truncating to {MAX_AUDIO_CHUNK_SIZE}")
            audio_data = audio_data[:MAX_AUDIO_CHUNK_SIZE]
        elif len(audio_data) < MIN_AUDIO_CHUNK_SIZE:
            print(f"[AUDIO_VALIDATION] Audio chunk too small: {len(audio_data)} samples, padding to {MIN_AUDIO_CHUNK_SIZE}")
            # 填充零到最小大小
            audio_data.extend([0.0] * (MIN_AUDIO_CHUNK_SIZE - len(audio_data)))
        
        # 转换为numpy数组
        audio_np = np.array(audio_data, dtype=np.float32)
        
        # 检查并修复音频数据范围
        max_val = np.max(np.abs(audio_np))
        if max_val > 1.0:
            print(f"[AUDIO_VALIDATION] Audio clipping detected (max: {max_val:.3f}), normalizing")
            audio_np = audio_np / max_val
            was_normalized = True
        
        # 重采样到标准采样率（如果需要）
        if chunk_samplerate != STANDARD_SAMPLERATE:
            print(f"[AUDIO_PROCESSING] Resampling from {chunk_samplerate}Hz to {STANDARD_SAMPLERATE}Hz")
            audio_np = _resample_audio_server(audio_np, chunk_samplerate, STANDARD_SAMPLERATE)
            was_resampled = True
        
        # 应用音频质量增强
        enhanced_audio = _enhance_audio_quality(audio_np)
        if not np.array_equal(enhanced_audio, audio_np):
            was_enhanced = True
        
        # 更新统计
        _update_audio_stats(
            original_length, 
            was_resampled=was_resampled,
            was_normalized=was_normalized, 
            was_enhanced=was_enhanced,
            had_error=had_error
        )
        
        return enhanced_audio.tolist(), None
        
    except Exception as e:
        had_error = True
        print(f"[AUDIO_PROCESSING] Error processing audio: {e}")
        _update_audio_stats(
            len(audio_data) if isinstance(audio_data, list) else 0,
            was_resampled=was_resampled,
            was_normalized=was_normalized,
            was_enhanced=was_enhanced,
            had_error=had_error
        )
        return None, f"Audio processing error: {str(e)}"

def _enhance_audio_quality(audio_data):
    """音频质量增强"""
    try:
        # 简单的降噪：移除极小的信号（可能是噪音）
        noise_threshold = 0.001
        audio_data[np.abs(audio_data) < noise_threshold] = 0
        
        # 轻微的平滑处理以减少噪音（简单的3点移动平均）
        if len(audio_data) > 2:
            smoothed = np.copy(audio_data)
            for i in range(1, len(audio_data) - 1):
                smoothed[i] = (audio_data[i-1] + audio_data[i] + audio_data[i+1]) / 3.0
            audio_data = smoothed
        
        return audio_data
    except Exception as e:
        print(f"[AUDIO_ENHANCEMENT] Error enhancing audio: {e}")
        return audio_data  # 返回原始数据如果增强失败

# 初始化扩展
db.init_app(app)
socketio = SocketIO(app)
login_manager = LoginManager(app)

# 全局存储连接的用户状态 (user_id: {username, sid, online, avatar_url, is_admin})
connected_users = {}

# --- 音频质量统计 ---
audio_stats = {
    'total_chunks_processed': 0,
    'chunks_resampled': 0,
    'chunks_normalized': 0,
    'chunks_enhanced': 0,
    'processing_errors': 0,
    'average_chunk_size': 0,
    'last_reset': datetime.utcnow()
}

def _update_audio_stats(chunk_size, was_resampled=False, was_normalized=False, was_enhanced=False, had_error=False):
    """更新音频处理统计"""
    global audio_stats
    
    audio_stats['total_chunks_processed'] += 1
    if was_resampled:
        audio_stats['chunks_resampled'] += 1
    if was_normalized:
        audio_stats['chunks_normalized'] += 1
    if was_enhanced:
        audio_stats['chunks_enhanced'] += 1
    if had_error:
        audio_stats['processing_errors'] += 1
    
    # 更新平均块大小（简单移动平均）
    if audio_stats['total_chunks_processed'] == 1:
        audio_stats['average_chunk_size'] = chunk_size
    else:
        audio_stats['average_chunk_size'] = (
            audio_stats['average_chunk_size'] * 0.9 + chunk_size * 0.1
        )

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
    print(f"[VOICE_DATA_STREAM] Event received. SID: {request.sid}, User: {current_user.username if current_user.is_authenticated else 'N/A'}. Data keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}") # Initial event reception log
    if not current_user.is_authenticated:
        print("[VOICE_DATA_STREAM] Received voice data from unauthenticated user.")
        return

    channel_id = data.get('channel_id')
    audio_data = data.get('audio_data') # This is a list of floats (samples)
    user_id = current_user.id
    username = current_user.username

    if channel_id is None or audio_data is None:
        print(f"[VOICE_DATA_STREAM] Missing channel_id or audio_data for user {user_id}. Discarding.")
        return
    
    # 构建音频元数据
    audio_metadata = {
        'samplerate': data.get('samplerate', STANDARD_SAMPLERATE),
        'channels': data.get('channels', STANDARD_CHANNELS),
        'dtype': data.get('dtype', STANDARD_DTYPE)
    }
    
    # 验证和处理音频数据
    processed_audio, error_msg = _validate_and_process_audio(audio_data, audio_metadata)
    
    if processed_audio is None:
        print(f"[VOICE_DATA_STREAM] Audio validation failed for user {user_id}: {error_msg}")
        # 可选：向客户端发送错误消息
        emit('audio_processing_error', {'message': error_msg}, room=request.sid)
        return
    
    # 如果音频数据长度发生变化，记录日志
    if len(processed_audio) != len(audio_data):
        print(f"[VOICE_DATA_STREAM] Audio length changed from {len(audio_data)} to {len(processed_audio)} samples for user {user_id}")
    
    room_name = f"voice_channel_{channel_id}"
    # print(f"[VOICE_DATA_STREAM] User {user_id} ({username}) sending processed audio to channel {channel_id} (room: {room_name}). Chunk size: {len(processed_audio)} samples.")

    # 1. Broadcast that this user is actively sending voice data (for card color change)
    emit('user_voice_activity', 
         {'channel_id': channel_id, 'user_id': user_id, 'username': username, 'active': True}, 
         room=room_name) # REMOVED: skip_sid=request.sid

    # 2. Forward the processed audio data chunk to others in the room with standardized metadata
    emit('voice_data_stream_chunk', 
         {
             'channel_id': channel_id, 
             'user_id': user_id, 
             'username': username, 
             'audio_data': processed_audio,
             'samplerate': STANDARD_SAMPLERATE,  # 总是发送标准采样率
             'channels': STANDARD_CHANNELS,      # 总是发送标准声道数
             'dtype': STANDARD_DTYPE             # 总是发送标准数据类型
         }, 
         room=room_name, 
         skip_sid=request.sid) # Still skip SID for the audio data itself to avoid self-playback of raw audio

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

# API: Get audio format configuration
@app.route('/api/audio/config', methods=['GET'])
@login_required
def get_audio_config_api():
    """返回服务端音频配置信息，供客户端协商格式使用"""
    return jsonify({
        'success': True,
        'audio_config': {
            'samplerate': STANDARD_SAMPLERATE,
            'channels': STANDARD_CHANNELS,
            'dtype': STANDARD_DTYPE,
            'max_chunk_size': MAX_AUDIO_CHUNK_SIZE,
            'min_chunk_size': MIN_AUDIO_CHUNK_SIZE,
            'supported_samplerates': [22050, 44100, 48000],  # 服务端支持的采样率
            'resampling_available': SCIPY_AVAILABLE,
            'audio_enhancement': True
        }
    })

# API: Test audio processing
@app.route('/api/audio/test', methods=['POST'])
@login_required
def test_audio_processing_api():
    """测试音频处理功能的API端点"""
    data = request.get_json()
    if not data:
        return jsonify(success=False, message="Request body cannot be empty"), 400
    
    test_audio = data.get('audio_data')
    test_metadata = data.get('metadata', {})
    
    if not test_audio:
        return jsonify(success=False, message="audio_data is required"), 400
    
    processed_audio, error_msg = _validate_and_process_audio(test_audio, test_metadata)
    
    if processed_audio is None:
        return jsonify(success=False, message=error_msg), 400
    
    return jsonify({
        'success': True,
        'original_length': len(test_audio),
        'processed_length': len(processed_audio),
        'message': 'Audio processing test successful'
    })

# API: Get audio statistics
@app.route('/api/audio/stats', methods=['GET'])
@login_required
def get_audio_stats_api():
    """获取音频处理统计信息（管理员专用）"""
    if not current_user.is_admin:
        return jsonify(success=False, message='仅限管理员访问'), 403
    
    return jsonify({
        'success': True,
        'audio_stats': {
            **audio_stats,
            'last_reset': audio_stats['last_reset'].isoformat(),
            'processing_success_rate': (
                (audio_stats['total_chunks_processed'] - audio_stats['processing_errors']) / 
                max(audio_stats['total_chunks_processed'], 1) * 100
            ),
            'resampling_rate': (
                audio_stats['chunks_resampled'] / 
                max(audio_stats['total_chunks_processed'], 1) * 100
            )
        }
    })

# API: Reset audio statistics
@app.route('/api/audio/stats/reset', methods=['POST'])
@login_required
def reset_audio_stats_api():
    """重置音频处理统计（管理员专用）"""
    if not current_user.is_admin:
        return jsonify(success=False, message='仅限管理员访问'), 403
    
    global audio_stats
    audio_stats = {
        'total_chunks_processed': 0,
        'chunks_resampled': 0,
        'chunks_normalized': 0,
        'chunks_enhanced': 0,
        'processing_errors': 0,
        'average_chunk_size': 0,
        'last_reset': datetime.utcnow()
    }
    
    return jsonify({
        'success': True,
        'message': 'Audio statistics reset successfully'
    })

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