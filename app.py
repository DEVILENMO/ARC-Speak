from flask import Flask, render_template, request, redirect, url_for, flash
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Channel, Message, VoiceSession
from forms import LoginForm, RegisterForm, ChannelForm, SettingsForm
import os
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///voicechat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 初始化扩展
db.init_app(app)
socketio = SocketIO(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

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

# 路由: 主页
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('main'))
    return redirect(url_for('login'))

# 路由: 登录
@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and check_password_hash(user.password, form.password.data):
            login_user(user)
            return redirect(url_for('main'))
        flash('用户名或密码错误')
    return render_template('login.html', form=form)

# 路由: 注册
@app.route('/register', methods=['GET', 'POST'])
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        if form.invite_code.data != 'ARC2015':
            flash('邀请码错误')
            return render_template('register.html', form=form)
            
        # 检查用户名是否存在
        if User.query.filter_by(username=form.username.data).first():
            flash('用户名已存在')
            return render_template('register.html', form=form)
            
        # 创建新用户
        hashed_password = generate_password_hash(form.password.data)
        new_user = User(username=form.username.data, password=hashed_password)
        db.session.add(new_user)
        db.session.commit()
        flash('注册成功，请登录')
        return redirect(url_for('login'))
    return render_template('register.html', form=form)

# 路由: 主界面
@app.route('/main')
@login_required
def main():
    # 获取用户有权访问的频道
    text_channels_query = Channel.query.filter_by(channel_type='text')
    voice_channels_query = Channel.query.filter_by(channel_type='voice')

    if not current_user.is_admin:
        # 普通用户只能看到公共频道或他们是成员的私有频道
        text_channels = [ch for ch in text_channels_query.all() if not ch.is_private or current_user in ch.members]
        voice_channels = [ch for ch in voice_channels_query.all() if not ch.is_private or current_user in ch.members]
    else:
        # 管理员可以看到所有频道
        text_channels = text_channels_query.all()
        voice_channels = voice_channels_query.all()

    return render_template('main.html', 
                          text_channels=text_channels, 
                          voice_channels=voice_channels)

# 路由: 频道
@app.route('/channel/<int:channel_id>')
@login_required
def channel(channel_id):
    channel_obj = Channel.query.get_or_404(channel_id) # Renamed to avoid conflict

    # 权限检查
    if channel_obj.is_private and not current_user.is_admin and current_user not in channel_obj.members:
        flash('您没有权限访问此私有频道。', 'error')
        return redirect(url_for('main'))

    # 获取用户有权访问的频道列表 (用于侧边栏)
    text_channels_query = Channel.query.filter_by(channel_type='text')
    voice_channels_query = Channel.query.filter_by(channel_type='voice')
    if not current_user.is_admin:
        text_channels = [ch for ch in text_channels_query.all() if not ch.is_private or current_user in ch.members]
        voice_channels = [ch for ch in voice_channels_query.all() if not ch.is_private or current_user in ch.members]
    else:
        text_channels = text_channels_query.all()
        voice_channels = voice_channels_query.all()
    
    if channel_obj.channel_type == 'text':
        messages = Message.query.filter_by(channel_id=channel_id).order_by(Message.timestamp).all()
        return render_template('main.html', 
                              current_channel=channel_obj,
                              messages=messages,
                              text_channels=text_channels, 
                              voice_channels=voice_channels)
    else:  # 语音频道
        active_users = VoiceSession.query.filter_by(channel_id=channel_id).all()
        return render_template('main.html', 
                              current_channel=channel_obj,
                              active_users=active_users,
                              text_channels=text_channels, 
                              voice_channels=voice_channels)

# 路由: 设置
@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    form = SettingsForm(obj=current_user)

    if form.validate_on_submit():
        current_user.avatar_url = form.avatar_url.data
        current_user.auto_join_voice = form.auto_join_voice.data
        db.session.commit()
        flash('设置已成功保存', 'success')
        return redirect(url_for('settings'))

    return render_template('settings.html', form=form)

# 路由: 管理员创建频道
@app.route('/create_channel', methods=['GET', 'POST'])
@login_required
def create_channel():
    if not current_user.is_admin:
        flash('只有管理员可以创建频道')
        return redirect(url_for('main'))
        
    form = ChannelForm()
    if form.validate_on_submit():
        new_channel = Channel(
            name=form.name.data,
            channel_type=form.channel_type.data,
            is_private=form.is_private.data
        )
        db.session.add(new_channel)
        
        if new_channel.is_private:
            new_channel.members.append(current_user)
            
        db.session.commit()
        flash('频道创建成功', 'success')
        return redirect(url_for('main'))
    return render_template('create_channel.html', form=form)

# 路由: 登出
@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# WebSocket: 连接事件
@socketio.on('connect')
def handle_connect():
    if current_user.is_authenticated:
        emit('user_connected', {'user_id': current_user.id, 'username': current_user.username}, broadcast=True)
    else:
        return False

# WebSocket: 断开连接事件
@socketio.on('disconnect')
def handle_disconnect():
    if current_user.is_authenticated:
        # 清理用户的语音会话
        session = VoiceSession.query.filter_by(user_id=current_user.id).first()
        if session:
            db.session.delete(session)
            db.session.commit()
        emit('user_disconnected', {'user_id': current_user.id}, broadcast=True)

# WebSocket: 加入文字频道
@socketio.on('join_text_channel')
def handle_join_text_channel(data):
    channel_id = data['channel_id']
    join_room(f"text_channel_{channel_id}")
    emit('user_joined_channel', {'user_id': current_user.id, 'username': current_user.username}, room=f"text_channel_{channel_id}")

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
    channel_id = data['channel_id']
    target_channel = Channel.query.get(channel_id)

    if not target_channel:
        emit('error', {'message': '语音频道不存在'})
        return
    
    if target_channel.channel_type != 'voice':
        emit('error', {'message': '目标频道不是语音频道'})
        return

    # 权限检查: 加入语音频道
    if target_channel.is_private and not current_user.is_admin and current_user not in target_channel.members:
        emit('error', {'message': '您没有权限加入此私有语音频道'})
        return

    # 检查用户是否已在其他语音频道
    existing_session = VoiceSession.query.filter_by(user_id=current_user.id).first()
    if existing_session:
        leave_room(f"voice_channel_{existing_session.channel_id}")
        db.session.delete(existing_session)
    
    # 创建新的语音会话
    new_session = VoiceSession(
        user_id=current_user.id,
        channel_id=channel_id
    )
    db.session.add(new_session)
    db.session.commit()
    
    # 加入房间
    join_room(f"voice_channel_{channel_id}")
    
    # 获取频道中的所有用户
    users_in_channel = VoiceSession.query.filter_by(channel_id=channel_id).all()
    user_list = [{'user_id': session.user.id, 'username': session.user.username} for session in users_in_channel]
    
    # 通知所有用户
    emit('voice_channel_users', {'users': user_list}, room=f"voice_channel_{channel_id}")
    emit('user_joined_voice', {'user_id': current_user.id, 'username': current_user.username}, room=f"voice_channel_{channel_id}")

# WebSocket: 离开语音频道
@socketio.on('leave_voice_channel')
def handle_leave_voice_channel():
    session = VoiceSession.query.filter_by(user_id=current_user.id).first()
    if session:
        channel_id = session.channel_id
        leave_room(f"voice_channel_{channel_id}")
        db.session.delete(session)
        db.session.commit()
        emit('user_left_voice', {'user_id': current_user.id}, room=f"voice_channel_{channel_id}")

# WebSocket: 用户说话状态更新
@socketio.on('user_speaking_status')
def handle_user_speaking_status(data):
    channel_id = data.get('channel_id')
    speaking = data.get('speaking')
    user_id = current_user.id

    if channel_id is not None and speaking is not None and user_id is not None:
        room_name = f"voice_channel_{channel_id}"
        # print(f"User {user_id} speaking status {speaking} in room {room_name}") # For debugging
        emit('user_speaking',
             {'user_id': user_id, 'speaking': speaking}, 
             room=room_name, 
             skip_sid=request.sid) # skip_sid 确保事件不会发回给原始发送者

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

# 错误处理
@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    # 也可以在这里添加数据库回滚操作，以防错误是由数据库会话问题引起的
    # db.session.rollback()
    return render_template('500.html'), 500

# 路由: 管理员面板
@app.route('/admin')
@login_required
def admin_panel():
    if not current_user.is_admin:
        flash('只有管理员可以访问此页面', 'error')
        return redirect(url_for('main'))
    
    users = User.query.all()
    channels = Channel.query.all()
    return render_template('admin_panel.html', users=users, channels=channels)

# 路由: 切换用户管理员状态
@app.route('/admin/user/<int:user_id>/toggle_admin')
@login_required
def toggle_admin_status(user_id):
    if not current_user.is_admin:
        flash('只有管理员可以执行此操作', 'error')
        return redirect(url_for('main'))

    user_to_modify = User.query.get_or_404(user_id)

    if user_to_modify.id == current_user.id:
        flash('不能修改自己的管理员状态', 'error')
        return redirect(url_for('admin_panel'))

    user_to_modify.is_admin = not user_to_modify.is_admin
    db.session.commit()
    
    action = "授予" if user_to_modify.is_admin else "移除"
    flash(f'用户 {user_to_modify.username} 的管理员权限已{action}', 'success')
    return redirect(url_for('admin_panel'))

# 路由: 删除用户
@app.route('/admin/user/<int:user_id>/delete')
@login_required
def delete_user(user_id):
    if not current_user.is_admin:
        flash('只有管理员可以执行此操作', 'error')
        return redirect(url_for('main'))

    user_to_delete = User.query.get_or_404(user_id)

    if user_to_delete.id == current_user.id:
        flash('不能删除自己', 'error')
        return redirect(url_for('admin_panel'))

    # 删除与用户相关的消息和语音会话
    Message.query.filter_by(user_id=user_to_delete.id).delete()
    VoiceSession.query.filter_by(user_id=user_to_delete.id).delete()
    
    db.session.delete(user_to_delete)
    db.session.commit()
    
    flash(f'用户 {user_to_delete.username} 已被成功删除', 'success')
    return redirect(url_for('admin_panel'))

# 路由: 编辑频道
@app.route('/admin/channel/<int:channel_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_channel(channel_id):
    if not current_user.is_admin:
        flash('只有管理员可以执行此操作', 'error')
        return redirect(url_for('main'))

    channel_to_edit = Channel.query.get_or_404(channel_id)
    form = ChannelForm(obj=channel_to_edit) # 使用obj参数预填充表单

    if form.validate_on_submit():
        channel_to_edit.name = form.name.data
        channel_to_edit.channel_type = form.channel_type.data
        channel_to_edit.is_private = form.is_private.data

        # 权限逻辑：如果频道从公开变为私有，确保创建者是成员
        # 如果频道从私有变公开，可以考虑清除所有成员，或保留他们（当前选择保留）
        if channel_to_edit.is_private:
            if current_user not in channel_to_edit.members:
                channel_to_edit.members.append(current_user)
        # else:
            # 如果从私有变为公开，是否清除成员？
            # channel_to_edit.members = [] # 取消注释以在变为公开时清除成员

        db.session.commit()
        flash(f'频道 {channel_to_edit.name} 已成功更新', 'success')
        return redirect(url_for('admin_panel'))

    return render_template('edit_channel.html', form=form, channel=channel_to_edit)

# 路由: 删除频道
@app.route('/admin/channel/<int:channel_id>/delete')
@login_required
def delete_channel(channel_id):
    if not current_user.is_admin:
        flash('只有管理员可以执行此操作', 'error')
        return redirect(url_for('main'))

    channel_to_delete = Channel.query.get_or_404(channel_id)

    # 删除与频道相关的消息和语音会话
    Message.query.filter_by(channel_id=channel_to_delete.id).delete()
    VoiceSession.query.filter_by(channel_id=channel_to_delete.id).delete()
    
    db.session.delete(channel_to_delete)
    db.session.commit()
    
    flash(f'频道 {channel_to_delete.name} 已被成功删除', 'success')
    return redirect(url_for('admin_panel'))

if __name__ == '__main__':
    with app.app_context(): # 添加 app_context
        db.create_all() # 创建所有数据库表
        create_initial_data() # 调用初始化数据函数
    socketio.run(app, port=5005, debug=True) 