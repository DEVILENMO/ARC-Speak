import flet as ft
import aiohttp # For making HTTP requests
import socketio # For SocketIO communication
import ssl

# --- Configuration ---
SERVER_ADDRESS = "47.103.156.181"
SERVER_PORT = 5005
API_BASE_URL = f"https://{SERVER_ADDRESS}:{SERVER_PORT}/api"
SIO_URL = f"https://{SERVER_ADDRESS}:{SERVER_PORT}"

# --- Global State (Illustrative - will be refined) ---
# sio_client will be initialized in main, after aiohttp_session is created
sio_client = None 
current_user_info = None
active_page_controls = {} 

# --- Main Application Logic ---
async def main(page: ft.Page):
    page.title = "Voice/Text Chat Client"
    page.vertical_alignment = ft.MainAxisAlignment.CENTER
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER

    # Create a shared aiohttp session with custom SSL context for the entire app lifecycle
    # WARNING: Disabling SSL verification is insecure for production.
    custom_ssl_context = ssl.create_default_context()
    custom_ssl_context.check_hostname = False
    custom_ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=custom_ssl_context)
    
    # Explicitly create and use a cookie_jar for the shared session
    cookie_jar = aiohttp.CookieJar(unsafe=True) # unsafe=True can sometimes help with cross-domain or complex setups, use with caution.
                                             # For same-domain, False (default) should be fine.
    shared_aiohttp_session = aiohttp.ClientSession(connector=connector, cookie_jar=cookie_jar)

    # Initialize SocketIO client with the shared aiohttp session
    global sio_client
    sio_client = socketio.AsyncClient(http_session=shared_aiohttp_session, logger=True, engineio_logger=True)

    # --- SocketIO Event Handlers (now defined inside main where sio_client is valid) ---
    @sio_client.event
    async def connect():
        print("Socket.IO connected successfully!")
        # Potentially update UI or state
        if 'status_text' in active_page_controls:
             active_page_controls['status_text'].value = "Socket.IO Connected!"
             page.update() # Make sure to update the page if a control is changed from an event

    @sio_client.event
    async def disconnect():
        print("Socket.IO disconnected.")
        if 'status_text' in active_page_controls:
             active_page_controls['status_text'].value = "Socket.IO Disconnected."
             page.update()

    @sio_client.event
    async def connect_error(data):
        print(f"Socket.IO connection failed: {data}")
        if 'status_text' in active_page_controls:
             active_page_controls['status_text'].value = f"Socket.IO Error: {data}"
             page.update()

    status_text = ft.Text() 
    active_page_controls['status_text'] = status_text # Store for access from SIO events

    async def attempt_login(e):
        username = username_field.value
        password = password_field.value
        status_text.value = "Logging in..."
        login_button.disabled = True
        register_button.disabled = True
        page.update()

        login_payload = {"username": username, "password": password}
        
        try:
            # Use the shared_aiohttp_session
            async with shared_aiohttp_session.post(f"{API_BASE_URL}/login", json=login_payload) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("success"):
                        global current_user_info
                        current_user_info = data.get("user")
                        status_text.value = f"Login successful! Welcome, {current_user_info.get('username')}."
                        print(f"Logged in user: {current_user_info}")
                        
                        try:
                            if not sio_client.connected:
                                await sio_client.connect(SIO_URL, wait_timeout=10) # No SSL args needed here
                            else:
                                print("Socket.IO already connected.") # Or re-auth if needed
                            show_main_app_view(page) 
                        except socketio.exceptions.ConnectionError as sio_err:
                            status_text.value = f"Login OK, but Socket.IO connection failed: {sio_err}"
                            print(f"Socket.IO Connection Error: {sio_err}")
                    else:
                        status_text.value = f"Login failed: {data.get('message', 'Unknown error')}"
                else:
                    error_text = await response.text()
                    status_text.value = f"Login request failed: {response.status} - {error_text}"
        except aiohttp.ClientError as http_err: # General aiohttp client error
            status_text.value = f"HTTP Connection Error: {http_err}. Check server and network."
            print(f"HTTP Connection Error during login: {http_err}")
        except Exception as ex:
            status_text.value = f"An unexpected error occurred: {ex}"
            print(f"Unexpected error during login: {ex}")
        finally:
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

    login_view_controls = ft.Column(
        [
            ft.Text("Client Login", size=24, weight=ft.FontWeight.BOLD),
            username_field,
            password_field,
            ft.Row([login_button, register_button], alignment=ft.MainAxisAlignment.CENTER),
            status_text,
        ],
        alignment=ft.MainAxisAlignment.CENTER,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=20
    )
    active_page_controls['login'] = login_view_controls

    main_app_view_content = ft.Column(
        [
            ft.Text("Welcome to the App!", size=24),
            ft.ElevatedButton("Logout", on_click=lambda e: show_login_view(page)) 
        ],
        alignment=ft.MainAxisAlignment.CENTER,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        visible=False 
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
        p.update()
        
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

if __name__ == "__main__":
    ft.app(target=main) 