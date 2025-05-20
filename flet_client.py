import flet as ft
import aiohttp # For making HTTP requests
import socketio # For SocketIO communication
import ssl
import json # For saving/loading config
import os # For checking config file existence

# --- Configuration ---
SERVER_ADDRESS = "47.103.156.181"
SERVER_PORT = 5005
API_BASE_URL = f"https://{SERVER_ADDRESS}:{SERVER_PORT}/api"
SIO_URL = f"https://{SERVER_ADDRESS}:{SERVER_PORT}"
CONFIG_FILE = "config.json"

# --- Global State (Illustrative - will be refined) ---
# sio_client will be initialized in main, after aiohttp_session is created
sio_client = None 
current_user_info = None
active_page_controls = {} 
shared_aiohttp_session = None # Added for broader access if necessary, though primarily used in main

# State for chat and voice
current_text_channel_id = None
current_voice_channel_id = None
text_channels_data = {} # {id: {"name": name, "is_private": is_private}}
voice_channels_data = {} # {id: {"name": name, "is_private": is_private}}
current_chat_messages = [] # List of message objects/tuples
users_in_current_voice_channel = [] # List of user objects/tuples
all_server_users = [] # New state for all users on the server

# --- Config Helper Functions ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"Error decoding {CONFIG_FILE}. Starting with no saved config.")
            return {}
    return {}

def save_config(config_data):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_data, f, indent=4)
    except IOError as e:
        print(f"Error saving config to {CONFIG_FILE}: {e}")


# --- Main Application Logic ---
async def main(page: ft.Page):
    page.title = "Voice/Text Chat Client"
    page.padding = 0 # Use full window space

    # Load existing config
    app_config = load_config()

    # Create a shared aiohttp session with custom SSL context for the entire app lifecycle
    # WARNING: Disabling SSL verification is insecure for production.
    custom_ssl_context = ssl.create_default_context()
    custom_ssl_context.check_hostname = False
    custom_ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=custom_ssl_context)
    
    # Explicitly create and use a cookie_jar for the shared session
    cookie_jar = aiohttp.CookieJar(unsafe=True) # unsafe=True can sometimes help with cross-domain or complex setups, use with caution.
                                             # For same-domain, False (default) should be fine.
    global shared_aiohttp_session # Make it assignable globally
    shared_aiohttp_session = aiohttp.ClientSession(connector=connector, cookie_jar=cookie_jar)

    # Initialize SocketIO client with the shared aiohttp session
    global sio_client
    sio_client = socketio.AsyncClient(http_session=shared_aiohttp_session, logger=True, engineio_logger=True)

    # --- SocketIO Event Handlers (now defined inside main where sio_client is valid) ---
    @sio_client.event
    async def connect():
        print("Socket.IO connected successfully!")
        # Potentially update UI or state
        if 'status_text' in active_page_controls and active_page_controls['status_text']:
             active_page_controls['status_text'].value = "Socket.IO Connected!"
             if hasattr(page, 'update'): page.update() # Make sure to update the page

    @sio_client.event
    async def disconnect():
        print("Socket.IO disconnected.")
        if 'status_text' in active_page_controls and active_page_controls['status_text']:
             active_page_controls['status_text'].value = "Socket.IO Disconnected."
             if hasattr(page, 'update'): page.update()

    @sio_client.event
    async def connect_error(data):
        print(f"Socket.IO connection failed: {data}")
        if 'status_text' in active_page_controls and active_page_controls['status_text']:
             active_page_controls['status_text'].value = f"Socket.IO Error: {data}"
             if hasattr(page, 'update'): page.update()

    @sio_client.event
    async def new_message(data):
        print(f"Received new message: {data}")
        if data.get('channel_id') == current_text_channel_id:
            # Simple text display for now. Can be enhanced with ft.UserControl for rich messages.
            msg_author = data.get('username', 'Unknown')
            msg_content = data.get('content', '')
            msg_ts = data.get('timestamp', '')
            message_control = ft.Text(f"[{msg_ts}] {msg_author}: {msg_content}", selectable=True, font_family="Consolas") # Added font_family for better looks
            active_page_controls['chat_messages_view'].controls.append(message_control)
            active_page_controls['chat_messages_view'].update() # Update the ListView
            # Consider page.update() if the listview update isn't sufficient or causes issues

    @sio_client.event
    async def voice_channel_users(data):
        print(f"Users in voice channel: {data}")
        global users_in_current_voice_channel
        users_in_current_voice_channel = data.get('users', [])
        
        if 'voice_channel_users_view' in active_page_controls:
            user_list_controls = []
            for user_info in users_in_current_voice_channel:
                user_list_controls.append(ft.Text(user_info.get('username', 'Unknown User')))
            active_page_controls['voice_channel_users_view'].controls = user_list_controls
            active_page_controls['voice_channel_users_view'].update()

    @sio_client.event
    async def user_joined_voice(data):
        print(f"User joined voice: {data}")
        # The server also sends 'voice_channel_users' after this, which rebuilds the list.
        # For a more incremental update (optional):
        # user_id = data.get('user_id')
        # username = data.get('username')
        # if user_id and username and not any(u['user_id'] == user_id for u in users_in_current_voice_channel):
        #     users_in_current_voice_channel.append({'user_id': user_id, 'username': username})
        #     active_page_controls['voice_channel_users_view'].controls.append(ft.Text(username))
        #     active_page_controls['voice_channel_users_view'].update()
        pass # Relying on voice_channel_users for now

    @sio_client.event
    async def user_left_voice(data):
        print(f"User left voice: {data}")
        # Server also sends 'voice_channel_users' after this.
        # For a more incremental update (optional):
        # user_id_left = data.get('user_id')
        # if user_id_left:
        #     users_in_current_voice_channel = [u for u in users_in_current_voice_channel if u['user_id'] != user_id_left]
        #     # Rebuild view or remove specific control
        #     user_list_controls = [ft.Text(u['username']) for u in users_in_current_voice_channel]
        #     active_page_controls['voice_channel_users_view'].controls = user_list_controls
        #     active_page_controls['voice_channel_users_view'].update()
        pass # Relying on voice_channel_users for now

    @sio_client.event
    async def user_speaking(data):
        # print(f"User speaking status: {data}") # Can be very verbose
        # TODO: Update UI to show speaking indicator for user_id data['user_id']
        pass
    
    @sio_client.event
    async def error(data):
        print(f"SocketIO Server Error: {data.get('message')}")
        # Potentially show this in UI, e.g., a snackbar or status update
        if 'main_status_bar' in active_page_controls and active_page_controls['main_status_bar']:
            active_page_controls['main_status_bar'].value = f"Error: {data.get('message')}"
            page.update()

    @sio_client.event
    async def server_user_list_update(data):
        print(f"Received server user list update: {data}")
        global all_server_users
        all_server_users = data # data is expected to be a list of user dicts
        
        if 'server_users_list_view' in active_page_controls:
            user_list_ui_controls = []
            for user_info in sorted(all_server_users, key=lambda u: u.get('username', '').lower()):
                # Basic display: username. Add online indicator.
                # Avatar can be added later using ft.Image or similar.
                # For online status, server now sends only online users.
                # If server were to send online:false, we could change icon/color.
                user_row = ft.Row(
                    [
                        ft.Icon(name=ft.Icons.CIRCLE, color=ft.Colors.GREEN_ACCENT_700, size=10), # Online indicator
                        ft.Text(user_info.get('username', 'Unknown User')),
                        # Optionally, add admin badge or other info
                        # ft.Text("(Admin)", color=ft.Colors.ORANGE) if user_info.get('is_admin') else ft.Container()
                    ],
                    alignment=ft.MainAxisAlignment.START,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=5
                )
                user_list_ui_controls.append(user_row)
            
            active_page_controls['server_users_list_view'].controls = user_list_ui_controls
            active_page_controls['server_users_list_view'].update()
            # page.update() # Usually ListView().update() is enough.

    status_text = ft.Text() 
    active_page_controls['status_text'] = status_text

    # --- Config related controls ---
    remember_me_checkbox = ft.Checkbox(label="记住我", value=app_config.get("remember_me", False))
    active_page_controls['remember_me_checkbox'] = remember_me_checkbox

    # --- Channel Interaction Functions ---
    async def select_text_channel(page: ft.Page, channel_id: int, channel_name: str):
        global current_text_channel_id
        if current_text_channel_id == channel_id:
            return # Already in this channel

        current_text_channel_id = channel_id
        active_page_controls['current_chat_topic'].value = f"Chat - {channel_name}"
        active_page_controls['chat_messages_view'].controls.clear() # Clear old messages
        # TODO: Fetch historical messages for this channel via API or SocketIO event
        # For now, we just clear and wait for new messages.
        
        # Emit event to server that we joined this text channel's "room"
        if sio_client and sio_client.connected:
            try:
                await sio_client.emit('join_text_channel', {'channel_id': channel_id})
                print(f"Joined text channel room: {channel_id}")
            except Exception as e:
                print(f"Error joining text channel room: {e}")
        page.update()

    async def select_voice_channel(page: ft.Page, channel_id: int, channel_name: str):
        global current_voice_channel_id
        # For simplicity, clicking a voice channel means attempting to join/switch
        # A more robust UI would have separate "join" and "leave" actions.
        
        if sio_client and sio_client.connected:
            try:
                if current_voice_channel_id == channel_id: # Clicking current voice channel could mean "leave"
                    await sio_client.emit('leave_voice_channel')
                    print(f"Attempting to leave voice channel: {current_voice_channel_id}")
                    current_voice_channel_id = None
                    active_page_controls['current_voice_channel_text'].value = "Not in a voice channel"
                    active_page_controls['server_users_list_view'].controls.clear()
                else: # Joining a new or different voice channel
                    await sio_client.emit('join_voice_channel', {'channel_id': channel_id})
                    print(f"Attempting to join voice channel: {channel_id}")
                    current_voice_channel_id = channel_id
                    active_page_controls['current_voice_channel_text'].value = f"Voice: {channel_name}"
                page.update()
            except Exception as e:
                print(f"Error interacting with voice channel: {e}")
                active_page_controls['current_voice_channel_text'].value = "Voice: Error"
                page.update()

    async def fetch_and_display_channels(p: ft.Page):
        global text_channels_data, voice_channels_data
        if not shared_aiohttp_session or shared_aiohttp_session.closed:
            print("Aiohttp session not available for fetching channels.")
            return
        try:
            async with shared_aiohttp_session.get(f"{API_BASE_URL}/channels") as response:
                if response.status == 200:
                    data = await response.json()
                    text_channels = data.get("text_channels", [])
                    voice_channels = data.get("voice_channels", [])
                    
                    text_channels_data = {tc['id']: tc for tc in text_channels}
                    voice_channels_data = {vc['id']: vc for vc in voice_channels}

                    channel_list_controls = [ft.Text("Text Channels", weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_GREY_300)]
                    for tc in text_channels:
                        channel_list_controls.append(
                            ft.TextButton(
                                content=ft.Row([ft.Icon(ft.Icons.CHAT_BUBBLE_OUTLINE, size=16), ft.Text(tc['name'])]),
                                on_click=lambda _, t_id=tc['id'], t_name=tc['name']: p.run_task(select_text_channel, p, t_id, t_name),
                                style=ft.ButtonStyle(color=ft.Colors.BLACK)
                            )
                        )
                    
                    channel_list_controls.append(ft.Container(content=ft.Text("Voice Channels", weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_GREY_300), margin=ft.margin.only(top=10)))
                    for vc in voice_channels:
                        channel_list_controls.append(
                            ft.TextButton(
                                content=ft.Row([ft.Icon(ft.Icons.VOICE_CHAT_OUTLINED, size=16), ft.Text(vc['name'])]),
                                on_click=lambda _, v_id=vc['id'], v_name=vc['name']: p.run_task(select_voice_channel, p, v_id, v_name),
                                 style=ft.ButtonStyle(color=ft.Colors.BLACK)
                            )
                        )
                    active_page_controls['channel_list_view'].controls = channel_list_controls
                else:
                    print(f"Failed to fetch channels: {response.status} - {await response.text()}")
                    active_page_controls['channel_list_view'].controls = [ft.Text("Error loading channels.")]
                p.update()
        except Exception as e:
            print(f"Error fetching channels: {e}")
            active_page_controls['channel_list_view'].controls = [ft.Text(f"Error: {e}")]
            if hasattr(p, 'update'): p.update()

    async def attempt_login(e, is_auto_login=False): # Added is_auto_login flag
        username = username_field.value
        password = password_field.value
        
        if not is_auto_login: # Only show "Logging in..." if it's a manual attempt
            status_text.value = "Logging in..."
            login_button.disabled = True
            register_button.disabled = True
            page.update()

        login_payload = {"username": username, "password": password}
        
        try:
            async with shared_aiohttp_session.post(f"{API_BASE_URL}/login", json=login_payload) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("success"):
                        global current_user_info
                        current_user_info = data.get("user")
                        status_text.value = f"Login successful! Welcome, {current_user_info.get('username')}."
                        print(f"Logged in user: {current_user_info}")

                        # Save credentials if "Remember me" is checked
                        if remember_me_checkbox.value:
                            save_config({
                                "username": username,
                                # WARNING: Storing passwords directly is insecure. 
                                # Consider more secure methods for production.
                                "password": password, 
                                "remember_me": True
                            })
                        elif os.path.exists(CONFIG_FILE): # If not remember_me, clear saved config
                             save_config({})


                        try:
                            if not sio_client.connected:
                                await sio_client.connect(SIO_URL, wait_timeout=10) 
                            else:
                                print("Socket.IO already connected.")
                            
                            await fetch_and_display_channels(page)
                            show_main_app_view(page) 
                        except socketio.exceptions.ConnectionError as sio_err:
                            status_text.value = f"Login OK, but Socket.IO connection failed: {sio_err}"
                            print(f"Socket.IO Connection Error: {sio_err}")
                            # Enable buttons back if auto-login fails at SIO stage
                            if is_auto_login:
                                login_button.disabled = False
                                register_button.disabled = False
                                page.update()
                    else:
                        status_text.value = f"Login failed: {data.get('message', 'Unknown error')}"
                        if is_auto_login: # If auto-login fails, clear saved invalid credentials
                            save_config({}) 
                            remember_me_checkbox.value = False # Uncheck if auto-login fails
                else:
                    error_text = await response.text()
                    status_text.value = f"Login request failed: {response.status} - {error_text}"
                    if is_auto_login: # Clear saved invalid credentials
                        save_config({})
                        remember_me_checkbox.value = False
        except aiohttp.ClientError as http_err:
            status_text.value = f"HTTP Connection Error: {http_err}. Check server and network."
            print(f"HTTP Connection Error during login: {http_err}")
            if is_auto_login: # Clear saved invalid credentials and enable buttons
                save_config({})
                remember_me_checkbox.value = False
                login_button.disabled = False
                register_button.disabled = False
                page.update()
        except Exception as ex:
            status_text.value = f"An unexpected error occurred: {ex}"
            print(f"Unexpected error during login: {ex}")
            if is_auto_login: # Clear saved invalid credentials and enable buttons
                save_config({})
                remember_me_checkbox.value = False
                login_button.disabled = False
                register_button.disabled = False
                page.update()
        finally:
            # Only re-enable buttons if it wasn't a successful auto-login
            if not (data.get("success") if 'data' in locals() else False) or not is_auto_login :
                login_button.disabled = False
                register_button.disabled = False
            page.update()

    async def show_register_view(e):
        status_text.value = "Registration UI not yet implemented."
        page.update()

    username_field = ft.TextField(label="Username", width=300, autofocus=True)
    password_field = ft.TextField(label="Password", password=True, can_reveal_password=True, width=300)
    login_button = ft.ElevatedButton(text="Login", on_click=attempt_login, width=150)
    register_button = ft.ElevatedButton(text="Register", on_click=show_register_view, width=150)

    # Populate fields from config if available
    if app_config.get("remember_me") and app_config.get("username"):
        username_field.value = app_config.get("username")
        if app_config.get("password"): # Only fill password if it was saved
            password_field.value = app_config.get("password")
    
    login_view_controls = ft.Column(
        [
            ft.Text("Client Login", size=24, weight=ft.FontWeight.BOLD),
            username_field,
            password_field,
            remember_me_checkbox, # Add checkbox to layout
            ft.Row([login_button, register_button], alignment=ft.MainAxisAlignment.CENTER),
            status_text,
        ],
        alignment=ft.MainAxisAlignment.CENTER,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=20
    )
    active_page_controls['login'] = login_view_controls

    # --- Main Application View Components (New Structure) ---
    active_page_controls['channel_list_view'] = ft.ListView(expand=False, spacing=2, width=220, padding=ft.padding.all(10))
    active_page_controls['chat_messages_view'] = ft.ListView(expand=True, spacing=5, auto_scroll=True, padding=ft.padding.all(10))
    active_page_controls['message_input_field'] = ft.TextField(
        hint_text="Type a message...", 
        expand=True, 
        filled=True, 
        border_radius=20,
        on_submit=lambda e: page.run_task(handle_send_message_click, page) # Assign handler
    )
    active_page_controls['send_message_button'] = ft.IconButton(
        icon=ft.Icons.SEND_ROUNDED, 
        tooltip="Send Message",
        on_click=lambda e: page.run_task(handle_send_message_click, page) # Assign handler
    )
    active_page_controls['current_chat_topic'] = ft.Text("Select a text channel", weight=ft.FontWeight.BOLD, size=16)

    active_page_controls['current_voice_channel_text'] = ft.Text("Not in a voice channel", weight=ft.FontWeight.BOLD)
    active_page_controls['server_users_list_view'] = ft.ListView(expand=True, spacing=3, padding=ft.padding.only(top=5))
    # Join/Leave button might be integrated into channel selection or a dedicated button
    # active_page_controls['join_leave_voice_button'] = ft.ElevatedButton(text="Join/Leave Voice", on_click=handle_join_leave_voice_click)

    # Layout Definition
    left_panel = ft.Container(
        content=ft.Column(
            [
                ft.Text("Channels", weight=ft.FontWeight.BOLD, size=18, color=ft.Colors.WHITE),
                ft.Divider(height=5, color=ft.Colors.BLUE_GREY_700),
                active_page_controls['channel_list_view']
            ],
            expand=True
        ),
        width=240, # Increased width
        # height=page.height, # This might need dynamic adjustment or use expand in a Row
        padding=0, # Padding moved to ListView
        bgcolor=ft.Colors.BLUE_GREY_800, # Darker sidebar
        # border_radius=ft.border_radius.only(top_left=10, bottom_left=10)
    )

    chat_area = ft.Column(
        [
            active_page_controls['chat_messages_view'],
            ft.Row(
                [
                    active_page_controls['message_input_field'],
                    active_page_controls['send_message_button']
                ],
                alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        ],
        expand=True
    )
    
    chat_panel = ft.Container(
        content=ft.Column(
            [
                active_page_controls['current_chat_topic'],
                ft.Divider(height=1, color=ft.Colors.BLACK12),
                chat_area,
            ],
            expand=True
        ),
        expand=True,
        padding=ft.padding.all(10),
        bgcolor=ft.Colors.WHITE,
    )

    right_panel = ft.Container(
        content=ft.Column(
            [
                ft.Text("Server Users", weight=ft.FontWeight.BOLD, size=16, color=ft.Colors.BLUE_GREY_700),
                ft.Divider(height=1, color=ft.Colors.BLACK12),
                active_page_controls['server_users_list_view'],
                # active_page_controls['join_leave_voice_button'] # Placeholder
            ],
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        width=200,
        padding=10,
        bgcolor=ft.Colors.BLUE_GREY_50,
        # border_radius=ft.border_radius.only(top_right=10, bottom_right=10)
    )

    main_app_layout = ft.Row(
        [
            left_panel,
            chat_panel,
            right_panel, # Ensure right_panel is visible
        ],
        expand=True,
        vertical_alignment=ft.CrossAxisAlignment.STRETCH, # Stretch to fill height
        # spacing=0 # No space between panels
    )
    
    active_page_controls['main_status_bar'] = ft.Text(value="", size=12, color=ft.Colors.GREY)

    # Store the username Text control from the top bar for dynamic updates
    active_page_controls['top_bar_username_text'] = ft.Text(f"User: {current_user_info.get('username') if current_user_info else 'N/A'}", size=16, weight=ft.FontWeight.BOLD, expand=True, color=ft.Colors.WHITE)

    main_app_view_content = ft.Column(
        [
            # Top Bar (Optional, could be part of individual panels)
            ft.Container(
                content=ft.Row(
                    [
                        active_page_controls['top_bar_username_text'], # Use stored control
                        ft.IconButton(ft.Icons.LOGOUT, on_click=lambda e: show_login_view(page), tooltip="Logout", icon_color=ft.Colors.WHITE)
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER
                ),
                bgcolor=ft.Colors.BLUE_700, # Theme color for app bar
                padding=ft.padding.symmetric(horizontal=15, vertical=10),
                # border_radius=ft.border_radius.only(top_left=10, top_right=10)
            ),
            main_app_layout, # The new three-panel layout
            active_page_controls['main_status_bar'], # A small status bar at the bottom
        ],
        expand=True,
        visible=False,
        spacing=0 # No space between app bar and main layout
    )
    active_page_controls['main_app'] = main_app_view_content
    
    def show_login_view(p: ft.Page):
        active_page_controls['main_app'].visible = False
        active_page_controls['login'].visible = True
        # Consider what to do with shared_aiohttp_session and sio_client on logout
        # For now, sio_client.disconnect() might be called if connected.
        if sio_client and sio_client.connected:
            p.run_task(sio_client.disconnect) # Disconnect in background
        p.update()

    def show_main_app_view(p: ft.Page):
        active_page_controls['login'].visible = False
        active_page_controls['main_app'].visible = True
        # Update user name in the top bar when view is shown
        if current_user_info and 'top_bar_username_text' in active_page_controls:
            active_page_controls['top_bar_username_text'].value = f"User: {current_user_info.get('username', 'Unknown')}"
            # active_page_controls['top_bar_username_text'].update() # Not always needed if page.update() is called

        p.update()
        
    # --- Message Sending Function ---
    async def handle_send_message_click(page_ref: ft.Page): # Use page_ref to avoid conflict with global page
        message_content = active_page_controls['message_input_field'].value.strip()
        if message_content and current_text_channel_id is not None:
            if sio_client and sio_client.connected:
                try:
                    print(f"Sending message to channel {current_text_channel_id}: {message_content}")
                    await sio_client.emit('send_message', {
                        'channel_id': current_text_channel_id,
                        'message': message_content
                    })
                    active_page_controls['message_input_field'].value = "" # Clear input field
                    active_page_controls['message_input_field'].update()
                    # page_ref.update() # May not be needed if input field update is enough
                except Exception as e:
                    print(f"Error sending message: {e}")
                    # Optionally, update a status bar or show an error to the user
            else:
                print("Cannot send message: SocketIO not connected.")
        elif not message_content:
            print("Cannot send empty message.")
        elif current_text_channel_id is None:
            print("Cannot send message: No text channel selected.")
            # Optionally, provide feedback to the user (e.g., status bar)

    page.add(login_view_controls, main_app_view_content)
    show_login_view(page) 

    # Graceful shutdown for the aiohttp session when the Flet app closes
    # This is a bit of a workaround as Flet's direct async cleanup hooks are not simple.
    original_on_close = page.on_close if hasattr(page, 'on_close') else None
    async def on_close_extended(e):
        if original_on_close:
            original_on_close(e) # Call original if it existed
        if shared_aiohttp_session and not shared_aiohttp_session.closed:
            print("Closing shared aiohttp session...")
            await shared_aiohttp_session.close()
            print("Shared aiohttp session closed.")
    page.on_close = on_close_extended

    # Attempt auto-login if configured
    if remember_me_checkbox.value and username_field.value and password_field.value:
        print("Attempting auto-login...")
        status_text.value = "Attempting auto-login..." # Give some feedback
        page.update()
        await attempt_login(None, is_auto_login=True)

if __name__ == "__main__":
    ft.app(target=main) 