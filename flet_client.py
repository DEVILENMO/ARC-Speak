import flet as ft
import aiohttp # For making HTTP requests
import socketio # For SocketIO communication
import ssl
import json # For saving/loading config
import os # For checking config file existence
import inspect # For checking if a function is a coroutine

# --- Configuration ---
SERVER_ADDRESS = "47.103.156.181"
SERVER_PORT = 5005
API_BASE_URL = f"https://{SERVER_ADDRESS}:{SERVER_PORT}/api"
SIO_URL = f"https://{SERVER_ADDRESS}:{SERVER_PORT}"
CONFIG_FILE = "config.json"

# --- Global State ---
sio_client = None 
current_user_info = None
active_page_controls = {} 
shared_aiohttp_session = None
current_text_channel_id = None
current_voice_channel_id = None # ID of the voice channel user is actively (confirmed) in
previewing_voice_channel_id = None # ID of voice channel being previewed
is_actively_in_voice_channel = False # Has user clicked "Confirm Join"?

text_channels_data = {} 
voice_channels_data = {} 
current_chat_messages = [] 
all_server_users = [] 
current_voice_channel_active_users = {} # Users in the PREVIEWING or ACTIVE voice channel

# --- Config Helper Functions ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f: return json.load(f)
        except json.JSONDecodeError: return {}
    return {}
def save_config(config_data):
    try:
        with open(CONFIG_FILE, 'w') as f: json.dump(config_data, f, indent=4)
    except IOError: pass

# --- Main Application Logic ---
async def main(page: ft.Page):
    page.title = "Voice/Text Chat Client"
    page.padding = 0
    app_config = load_config()

    global sio_client, shared_aiohttp_session
    custom_ssl_context = ssl.create_default_context()
    custom_ssl_context.check_hostname = False
    custom_ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=custom_ssl_context)
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    shared_aiohttp_session = aiohttp.ClientSession(connector=connector, cookie_jar=cookie_jar)
    sio_client = socketio.AsyncClient(http_session=shared_aiohttp_session, logger=True, engineio_logger=True)

    def update_voice_panel_button_visibility():
        # Controls visibility of Join, Leave, and (future) Mute buttons
        confirm_join_btn = active_page_controls.get('confirm_join_voice_button')
        leave_voice_btn = active_page_controls.get('leave_voice_button')
        voice_settings_ctrl = active_page_controls.get('voice_settings_area')
        # mute_btn = active_page_controls.get('mute_button') # Future

        if previewing_voice_channel_id is not None: # If we are previewing *any* voice channel
            if is_actively_in_voice_channel:
                if confirm_join_btn: confirm_join_btn.visible = False
                if leave_voice_btn: leave_voice_btn.visible = True
                if voice_settings_ctrl: voice_settings_ctrl.visible = True
                # if mute_btn: mute_btn.visible = True # Future
            else: # Previewing but not actively joined
                if confirm_join_btn: confirm_join_btn.visible = True
                if leave_voice_btn: leave_voice_btn.visible = False
                if voice_settings_ctrl: voice_settings_ctrl.visible = False
                # if mute_btn: mute_btn.visible = False # Future
        else: # Not previewing any voice channel (e.g., text channel selected)
            if confirm_join_btn: confirm_join_btn.visible = False
            if leave_voice_btn: leave_voice_btn.visible = False
            if voice_settings_ctrl: voice_settings_ctrl.visible = False
            # if mute_btn: mute_btn.visible = False # Future
        
        if hasattr(page, 'update'): page.update() # Update the page to reflect button visibility

    def update_voice_channel_user_list_ui():
        # This function now updates the list for the PREVIEWING or ACTIVE voice channel
        vc_users_list_ctrl = active_page_controls.get('voice_channel_internal_users_list')
        if not vc_users_list_ctrl: return

        vc_user_controls = []
        # current_voice_channel_active_users should hold users for previewing_voice_channel_id
        sorted_users_in_vc = sorted(current_voice_channel_active_users.values(), key=lambda u: u.get('username', '').lower())
        for user_data in sorted_users_in_vc:
            is_speaking_active = is_actively_in_voice_channel and user_data.get('speaking', False)
            speaking_indicator = ft.Icon(
                name=ft.Icons.RECORD_VOICE_OVER if is_speaking_active else ft.Icons.VOICE_OVER_OFF,
                color=ft.Colors.GREEN_ACCENT_700 if is_speaking_active else ft.Colors.GREY,
                size=16
            )
            vc_user_controls.append(ft.Row([speaking_indicator, ft.Text(user_data.get('username', 'Unknown'))], alignment=ft.MainAxisAlignment.START, spacing=5))
        vc_users_list_ctrl.controls = vc_user_controls
        if hasattr(vc_users_list_ctrl, 'update'): vc_users_list_ctrl.update()

        topic_display = active_page_controls.get('voice_channel_topic_display')
        if topic_display and previewing_voice_channel_id and voice_channels_data.get(previewing_voice_channel_id):
            ch_name = voice_channels_data[previewing_voice_channel_id]['name']
            prefix = "Voice:" if is_actively_in_voice_channel else "Preview:"
            topic_display.value = f"{prefix} {ch_name}"
            if hasattr(topic_display, 'update'): topic_display.update()
        update_voice_panel_button_visibility() # Ensure buttons are correct state

    # --- SocketIO Event Handlers ---
    @sio_client.event
    async def connect():
        if active_page_controls.get('status_text'): active_page_controls['status_text'].value = "Socket.IO Connected!"
        if hasattr(page, 'update'): page.update()

    @sio_client.event
    async def disconnect():
        if active_page_controls.get('status_text'): active_page_controls['status_text'].value = "Socket.IO Disconnected."
        if hasattr(page, 'update'): page.update()

    @sio_client.event
    async def connect_error(data):
        if active_page_controls.get('status_text'): active_page_controls['status_text'].value = f"Socket.IO Error: {data}"
        if hasattr(page, 'update'): page.update()
    
    @sio_client.event
    async def new_message(data):
        if data.get('channel_id') == current_text_channel_id and active_page_controls.get('chat_messages_view'):
            active_page_controls['chat_messages_view'].controls.append(ft.Text(f"[{data.get('timestamp')}] {data.get('username')}: {data.get('content')}", selectable=True, font_family="Consolas"))
            active_page_controls['chat_messages_view'].update()

    @sio_client.event
    async def voice_channel_users(data): # Users in a specific voice channel (could be due to our join or other updates)
        channel_id_of_update = data.get('channel_id') # Server should send this!
        if channel_id_of_update == previewing_voice_channel_id: # Only update if it matches the channel we are previewing/in
            global current_voice_channel_active_users
            current_voice_channel_active_users.clear()
            for user_info in data.get('users', []):
                current_voice_channel_active_users[user_info['user_id']] = {'id': user_info['user_id'], 'username': user_info['username'], 'speaking': False}
            update_voice_channel_user_list_ui()

    @sio_client.event
    async def user_joined_voice(data):
        channel_id_of_update = data.get('channel_id')
        if channel_id_of_update == previewing_voice_channel_id: # And matches previewing_voice_channel_id
            user_id, username = data.get('user_id'), data.get('username')
            if user_id and username and user_id not in current_voice_channel_active_users:
                current_voice_channel_active_users[user_id] = {'id': user_id, 'username': username, 'speaking': False}
                update_voice_channel_user_list_ui()

    @sio_client.event
    async def user_left_voice(data):
        channel_id_of_update = data.get('channel_id')
        if channel_id_of_update == previewing_voice_channel_id: # And matches previewing_voice_channel_id
            user_id_left = data.get('user_id')
            if user_id_left in current_voice_channel_active_users:
                del current_voice_channel_active_users[user_id_left]
                update_voice_channel_user_list_ui()

    @sio_client.event
    async def user_speaking(data):
        user_id, is_speaking_status, target_channel_id = data.get('user_id'), data.get('speaking'), data.get('channel_id')
        if is_actively_in_voice_channel and target_channel_id == current_voice_channel_id and user_id in current_voice_channel_active_users:
            current_voice_channel_active_users[user_id]['speaking'] = is_speaking_status
            update_voice_channel_user_list_ui()
    
    @sio_client.event
    async def error(data):
        if active_page_controls.get('main_status_bar'): active_page_controls['main_status_bar'].value = f"Error: {data.get('message')}"
        if hasattr(page, 'update'): page.update()

    @sio_client.event
    async def server_user_list_update(data):
        global all_server_users
        all_server_users = data
        if active_page_controls.get('server_users_list_view'):
            controls = [ft.Row([ft.Icon(name=ft.Icons.CIRCLE, color=ft.Colors.GREEN_ACCENT_700, size=10), ft.Text(u.get('username','N/A'))], alignment=ft.MainAxisAlignment.START, spacing=5) for u in sorted(all_server_users, key=lambda u: u.get('username', '').lower())]
            active_page_controls['server_users_list_view'].controls = controls
            active_page_controls['server_users_list_view'].update()

    active_page_controls['status_text'] = ft.Text()
    remember_me_checkbox = ft.Checkbox(label="记住我", value=app_config.get("remember_me", False))

    async def _leave_current_voice_channel_if_any(page_ref: ft.Page, switch_to_text: bool = False, new_text_channel_id = None, new_text_channel_name = "Select a text channel"):
        global current_voice_channel_id, previewing_voice_channel_id, is_actively_in_voice_channel, current_voice_channel_active_users
        
        effective_vc_id_to_leave = current_voice_channel_id if is_actively_in_voice_channel else previewing_voice_channel_id

        if sio_client and sio_client.connected and effective_vc_id_to_leave is not None:
            try: 
                print(f"Client emitting leave_voice_channel for channel_id: {effective_vc_id_to_leave}") # Debug print
                await sio_client.emit('leave_voice_channel', {'channel_id': effective_vc_id_to_leave})
            except Exception as e: 
                print(f"Error emitting leave_voice_channel: {e}")
        
        is_actively_in_voice_channel = False
        current_voice_channel_id = None
        previewing_voice_channel_id = None # Fully exit voice context
        current_voice_channel_active_users.clear()
        update_voice_channel_user_list_ui() # Update UI (empty list, buttons hidden)
        if active_page_controls.get('current_voice_channel_text'): active_page_controls['current_voice_channel_text'].value = "Not in a voice channel"
        
        if switch_to_text:
            switch_middle_panel_view("text", new_text_channel_name)
            if new_text_channel_id is not None:
                if active_page_controls.get('chat_messages_view'): active_page_controls['chat_messages_view'].controls.clear(); active_page_controls['chat_messages_view'].update()
                if sio_client and sio_client.connected: await sio_client.emit('join_text_channel', {'channel_id': new_text_channel_id})
        update_voice_panel_button_visibility()
        if hasattr(page_ref, 'update'): page_ref.update()
        

    def switch_middle_panel_view(view_type: str, channel_name: str = ""):
        is_text_view = view_type == "text"
        if active_page_controls.get('chat_panel_content_group'): active_page_controls['chat_panel_content_group'].visible = is_text_view
        if active_page_controls.get('voice_panel_content_group'): active_page_controls['voice_panel_content_group'].visible = not is_text_view
        if is_text_view and active_page_controls.get('current_chat_topic'): active_page_controls['current_chat_topic'].value = f"Chat - {channel_name}"
        # Voice panel topic is updated within update_voice_channel_user_list_ui
        update_voice_panel_button_visibility()
        if hasattr(page, 'update'): page.update()

    async def select_text_channel(page_ref: ft.Page, channel_id: int, channel_name: str):
        global current_text_channel_id, previewing_voice_channel_id, is_actively_in_voice_channel
        if current_text_channel_id == channel_id and active_page_controls.get('chat_panel_content_group', {}).get('visible', True): return

        if previewing_voice_channel_id is not None: # Was previewing or in a voice channel
            await _leave_current_voice_channel_if_any(page_ref, switch_to_text=False) # Leave silently, don't switch panel yet
        
        current_text_channel_id = channel_id
        switch_middle_panel_view("text", channel_name)
        if active_page_controls.get('chat_messages_view'): active_page_controls['chat_messages_view'].controls.clear(); active_page_controls['chat_messages_view'].update()
        if sio_client and sio_client.connected: await sio_client.emit('join_text_channel', {'channel_id': channel_id})
        if hasattr(page_ref, 'update'): page_ref.update()

    async def select_voice_channel(page_ref: ft.Page, channel_id: int, channel_name: str):
        global previewing_voice_channel_id, is_actively_in_voice_channel, current_voice_channel_active_users, current_text_channel_id

        if previewing_voice_channel_id == channel_id and not is_actively_in_voice_channel and active_page_controls.get('voice_panel_content_group',{}).get('visible',False):
             return # Already previewing this and view is correct
        if current_voice_channel_id == channel_id and is_actively_in_voice_channel and active_page_controls.get('voice_panel_content_group',{}).get('visible',False):
             return # Already actively in this channel and view is correct

        if sio_client and sio_client.connected:
            # Leave any previous voice channel (active or preview) before starting new preview
            if previewing_voice_channel_id is not None and previewing_voice_channel_id != channel_id:
                 await _leave_current_voice_channel_if_any(page_ref, switch_to_text=False)
            elif is_actively_in_voice_channel and current_voice_channel_id != channel_id:
                 await _leave_current_voice_channel_if_any(page_ref, switch_to_text=False)

            previewing_voice_channel_id = channel_id
            is_actively_in_voice_channel = False # Start in preview mode
            current_text_channel_id = None # Clear selected text channel when focusing on voice
            current_voice_channel_active_users.clear()
            
            try:
                await sio_client.emit('join_voice_channel', {'channel_id': channel_id}) # Soft join to get users
                switch_middle_panel_view("voice", channel_name) # Switch view, updates buttons via its call to update_voice_panel_button_visibility
                # update_voice_channel_user_list_ui will be called by SIO event voice_channel_users
                if active_page_controls.get('current_voice_channel_text'): active_page_controls['current_voice_channel_text'].value = f"Preview: {channel_name}"
            except Exception as e: print(f"Error joining/previewing voice channel: {e}")
        if hasattr(page_ref, 'update'): page_ref.update()

    async def handle_confirm_join_voice_button_click(page_ref: ft.Page):
        global is_actively_in_voice_channel, current_voice_channel_id, previewing_voice_channel_id
        if previewing_voice_channel_id is not None:
            is_actively_in_voice_channel = True
            current_voice_channel_id = previewing_voice_channel_id # Confirm active channel
            # No need to emit join again, already did for preview
            update_voice_channel_user_list_ui() # Update topic and button visibility
            if active_page_controls.get('current_voice_channel_text'): active_page_controls['current_voice_channel_text'].value = f"Voice: {voice_channels_data.get(current_voice_channel_id, {}).get('name', 'Unknown')}"
        if hasattr(page_ref, 'update'): page_ref.update()

    async def handle_leave_voice_click(page_ref: ft.Page):
        # This button is only visible if is_actively_in_voice_channel is True
        global current_text_channel_id
        # Determine which text channel to switch back to
        target_text_channel_id, target_text_channel_name = None, "Select a text channel"
        if current_text_channel_id and text_channels_data.get(current_text_channel_id):
            target_text_channel_id, target_text_channel_name = current_text_channel_id, text_channels_data[current_text_channel_id]['name']
        elif text_channels_data:
            first_text_ch = next(iter(text_channels_data.values()), None)
            if first_text_ch: target_text_channel_id, target_text_channel_name = first_text_ch['id'], first_text_ch['name']
        
        await _leave_current_voice_channel_if_any(page_ref, switch_to_text=True, new_text_channel_id=target_text_channel_id, new_text_channel_name=target_text_channel_name)
        current_text_channel_id = target_text_channel_id # Ensure this is set after leaving voice

    async def fetch_and_display_channels(p: ft.Page): # Simplified, no changes from previous full code
        global text_channels_data, voice_channels_data
        if not shared_aiohttp_session or shared_aiohttp_session.closed: return
        async with shared_aiohttp_session.get(f"{API_BASE_URL}/channels") as response:
            if response.status == 200:
                data = await response.json()
                text_channels, voice_channels = data.get("text_channels", []), data.get("voice_channels", [])
                text_channels_data = {tc['id']: tc for tc in text_channels}
                voice_channels_data = {vc['id']: vc for vc in voice_channels}
                channel_list_controls = [ft.Text("Text Channels", weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_GREY_300)]
                for tc in text_channels: channel_list_controls.append(ft.TextButton(content=ft.Row([ft.Icon(ft.Icons.CHAT_BUBBLE_OUTLINE, size=16), ft.Text(tc['name'])]), on_click=lambda _, t_id=tc['id'], t_name=tc['name']: p.run_task(select_text_channel, p, t_id, t_name), style=ft.ButtonStyle(color=ft.Colors.BLACK)))
                channel_list_controls.append(ft.Container(content=ft.Text("Voice Channels", weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_GREY_300), margin=ft.margin.only(top=10)))
                for vc in voice_channels: channel_list_controls.append(ft.TextButton(content=ft.Row([ft.Icon(ft.Icons.VOICE_CHAT_OUTLINED, size=16), ft.Text(vc['name'])]), on_click=lambda _, v_id=vc['id'], v_name=vc['name']: p.run_task(select_voice_channel, p, v_id, v_name), style=ft.ButtonStyle(color=ft.Colors.BLACK)))
                if active_page_controls.get('channel_list_view'): active_page_controls['channel_list_view'].controls = channel_list_controls
            else: print(f"Failed to fetch channels: {response.status}")
            if hasattr(p, 'update'): p.update()
    
    async def attempt_login(e, is_auto_login=False): # Simplified, no changes from previous full code
        username, password = username_field.value, password_field.value
        if not is_auto_login and active_page_controls.get('status_text'): 
            active_page_controls['status_text'].value = "Logging in..."
            if login_button: login_button.disabled = True ; login_button.update()
            if register_button: register_button.disabled = True; register_button.update()
        login_payload, data_response = {"username": username, "password": password}, {}
        try:
            async with shared_aiohttp_session.post(f"{API_BASE_URL}/login", json=login_payload) as response:
                data_response = await response.json()
                if response.status == 200 and data_response.get("success"):
                    global current_user_info; current_user_info = data_response.get("user")
                    if active_page_controls.get('status_text'): active_page_controls['status_text'].value = f"Welcome, {current_user_info.get('username')}."
                    if remember_me_checkbox.value: save_config({"username": username, "password": password, "remember_me": True})
                    elif os.path.exists(CONFIG_FILE): save_config({})
                    if not sio_client.connected: await sio_client.connect(SIO_URL, wait_timeout=10)
                    await fetch_and_display_channels(page)
                    show_main_app_view(page)
                    first_text_ch = next(iter(text_channels_data.values()), None)
                    if first_text_ch: await select_text_channel(page, first_text_ch['id'], first_text_ch['name'])
                    else: switch_middle_panel_view("text", "No text channels available")
                else:
                    msg = data_response.get('message', 'Error') if isinstance(data_response, dict) else await response.text()
                    if active_page_controls.get('status_text'): active_page_controls['status_text'].value = f"Login failed: {msg}"
                    if is_auto_login: save_config({}); remember_me_checkbox.value = False; remember_me_checkbox.update()
        except Exception as ex:
            if active_page_controls.get('status_text'): active_page_controls['status_text'].value = f"Login error: {ex}"
            if is_auto_login: save_config({}); remember_me_checkbox.value = False; remember_me_checkbox.update()
        finally:
            if not (data_response.get("success") if data_response else False) or not is_auto_login:
                if login_button: login_button.disabled = False; login_button.update()
                if register_button: register_button.disabled = False; register_button.update()
            if hasattr(page, 'update'): page.update()

    async def show_register_view(e): # Simplified
        if active_page_controls.get('status_text'): active_page_controls['status_text'].value = "Reg not implemented."

    username_field = ft.TextField(label="Username", width=300, autofocus=True, value=app_config.get("username", ""))
    password_field = ft.TextField(label="Password", password=True, can_reveal_password=True, width=300, value=app_config.get("password", ""))
    login_button = ft.ElevatedButton(text="Login", on_click=lambda e: page.run_task(attempt_login, e, False), width=150)
    register_button = ft.ElevatedButton(text="Register", on_click=show_register_view, width=150)
    active_page_controls['login'] = ft.Column([ft.Text("Client Login", size=24, weight=ft.FontWeight.BOLD), username_field, password_field, ft.Row([remember_me_checkbox], alignment=ft.MainAxisAlignment.CENTER), ft.Row([login_button, register_button], alignment=ft.MainAxisAlignment.CENTER), active_page_controls['status_text']], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=20)

    active_page_controls['channel_list_view'] = ft.ListView(expand=False, spacing=2, width=220, padding=10)
    active_page_controls['current_chat_topic'] = ft.Text("Select a text channel", weight=ft.FontWeight.BOLD, size=16)
    active_page_controls['chat_messages_view'] = ft.ListView(expand=True, spacing=5, auto_scroll=True, padding=10)
    active_page_controls['message_input_field'] = ft.TextField(hint_text="Type...", expand=True, filled=True, border_radius=20, on_submit=lambda e: page.run_task(handle_send_message_click, page))
    active_page_controls['send_message_button'] = ft.IconButton(icon=ft.Icons.SEND_ROUNDED, on_click=lambda e: page.run_task(handle_send_message_click, page))
    active_page_controls['chat_panel_content_group'] = ft.Column([active_page_controls['current_chat_topic'], ft.Divider(height=1), active_page_controls['chat_messages_view'], ft.Row([active_page_controls['message_input_field'], active_page_controls['send_message_button']])], expand=True, visible=True)

    active_page_controls['voice_channel_topic_display'] = ft.Text("Voice Channel", weight=ft.FontWeight.BOLD, size=16)
    active_page_controls['voice_channel_internal_users_list'] = ft.ListView(expand=True, spacing=5, padding=10)
    active_page_controls['voice_settings_area'] = ft.Column(
        [ft.Text("语音设置 (待实现)", weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_GREY_400)],
        visible=False,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=10
    )
    active_page_controls['confirm_join_voice_button'] = ft.ElevatedButton(text="加入语音", icon=ft.Icons.CALL, on_click=lambda e: page.run_task(handle_confirm_join_voice_button_click, page), visible=False, style=ft.ButtonStyle(bgcolor=ft.Colors.GREEN_ACCENT_700, color=ft.Colors.WHITE))
    active_page_controls['leave_voice_button'] = ft.ElevatedButton(text="离开语音", icon=ft.Icons.CALL_END, on_click=lambda e: page.run_task(handle_leave_voice_click, page), visible=False, style=ft.ButtonStyle(bgcolor=ft.Colors.RED_ACCENT_700, color=ft.Colors.WHITE))
    active_page_controls['voice_panel_content_group'] = ft.Column([
        active_page_controls['voice_channel_topic_display'], 
        ft.Divider(height=1), 
        ft.Container(content=ft.Text("Users in channel:", weight=ft.FontWeight.W_600), margin=ft.margin.only(top=10, bottom=5)),
        active_page_controls['voice_channel_internal_users_list'],
        active_page_controls['voice_settings_area'],
        active_page_controls['confirm_join_voice_button'],
        active_page_controls['leave_voice_button']
    ], expand=True, visible=False)
    
    middle_panel_container = ft.Container(ft.Stack([active_page_controls['chat_panel_content_group'], active_page_controls['voice_panel_content_group']]), expand=True, padding=10, bgcolor=ft.Colors.WHITE)
    left_panel = ft.Container(ft.Column([ft.Text("Channels",weight=ft.FontWeight.BOLD,size=18,color=ft.Colors.WHITE), ft.Divider(height=5,color=ft.Colors.BLUE_GREY_700),active_page_controls['channel_list_view']], expand=True), width=240,padding=0,bgcolor=ft.Colors.BLUE_GREY_800)
    active_page_controls['current_voice_channel_text'] = ft.Text("Not in voice", weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE, size=12, italic=True)
    active_page_controls['server_users_list_view'] = ft.ListView(expand=True, spacing=3, padding=ft.padding.only(top=5))
    right_panel = ft.Container(ft.Column([ft.Text("Server Users",weight=ft.FontWeight.BOLD,size=16,color=ft.Colors.BLUE_GREY_700),ft.Divider(height=1),active_page_controls['server_users_list_view']], expand=True,horizontal_alignment=ft.CrossAxisAlignment.CENTER), width=200,padding=10,bgcolor=ft.Colors.BLUE_GREY_50)
    main_app_layout = ft.Row([left_panel, middle_panel_container, right_panel], expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH)
    
    active_page_controls['top_bar_username_text'] = ft.Text("User: N/A", size=16, weight=ft.FontWeight.BOLD, expand=True, color=ft.Colors.WHITE)
    active_page_controls['main_status_bar'] = ft.Text(value="", size=12, color=ft.Colors.GREY)
    main_app_view_content = ft.Column([
        ft.Container(ft.Row([active_page_controls['top_bar_username_text'], active_page_controls['current_voice_channel_text'], ft.IconButton(ft.Icons.LOGOUT, on_click=lambda e: show_login_view(page),tooltip="Logout",icon_color=ft.Colors.WHITE)],vertical_alignment=ft.CrossAxisAlignment.CENTER),bgcolor=ft.Colors.BLUE_700,padding=ft.padding.symmetric(horizontal=15,vertical=10)),
        main_app_layout, active_page_controls['main_status_bar']
    ], expand=True, visible=False, spacing=0)
    active_page_controls['main_app'] = main_app_view_content
    
    def show_login_view(p: ft.Page):
        active_page_controls['main_app'].visible = False
        active_page_controls['login'].visible = True
        global current_voice_channel_id, current_text_channel_id, previewing_voice_channel_id, is_actively_in_voice_channel, current_voice_channel_active_users, current_chat_messages, all_server_users
        if sio_client and sio_client.connected: p.run_task(sio_client.disconnect) 
        current_voice_channel_id, current_text_channel_id, previewing_voice_channel_id = None, None, None
        is_actively_in_voice_channel = False
        current_voice_channel_active_users.clear(); current_chat_messages.clear(); all_server_users.clear()
        for k_ in ['server_users_list_view', 'chat_messages_view', 'voice_channel_internal_users_list']: 
            if active_page_controls.get(k_): active_page_controls[k_].controls.clear(); active_page_controls[k_].update()
        if active_page_controls.get('current_voice_channel_text'): active_page_controls['current_voice_channel_text'].value = "Not in voice"
        update_voice_panel_button_visibility()
        if hasattr(p, 'update'): p.update()

    def show_main_app_view(p: ft.Page):
        active_page_controls['login'].visible = False
        active_page_controls['main_app'].visible = True
        if current_user_info and active_page_controls.get('top_bar_username_text'): active_page_controls['top_bar_username_text'].value = f"User: {current_user_info.get('username', 'N/A')}"
        update_voice_panel_button_visibility() # Ensure correct buttons on view show
        if hasattr(p, 'update'): p.update()
        
    async def handle_send_message_click(page_ref: ft.Page):
        msg_content = active_page_controls['message_input_field'].value.strip()
        if msg_content and current_text_channel_id is not None and sio_client and sio_client.connected:
            await sio_client.emit('send_message', {'channel_id': current_text_channel_id, 'message': msg_content})
            active_page_controls['message_input_field'].value = ""; active_page_controls['message_input_field'].update()

    page.add(active_page_controls['login'], main_app_view_content)
    original_on_close = page.on_close if hasattr(page, 'on_close') else None
    async def on_close_extended(e):
        if original_on_close: await original_on_close(e) if inspect.iscoroutinefunction(original_on_close) else original_on_close(e)
        await _leave_current_voice_channel_if_any(page, switch_to_text=False) # Ensure we leave voice on app close
        if sio_client and sio_client.connected: await sio_client.disconnect()
        if shared_aiohttp_session and not shared_aiohttp_session.closed: await shared_aiohttp_session.close()
    page.on_close = on_close_extended

    if remember_me_checkbox.value and username_field.value and password_field.value:
        if active_page_controls.get('status_text'): active_page_controls['status_text'].value = "Auto-login..."
        if hasattr(page, 'update'): page.update()
        await attempt_login(None, is_auto_login=True)

if __name__ == "__main__":
    ft.app(target=main) 