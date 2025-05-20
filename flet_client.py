import flet as ft
import aiohttp # For making HTTP requests
import socketio # For SocketIO communication

# --- Configuration ---
SERVER_ADDRESS = "47.103.156.181"
SERVER_PORT = 5005
API_BASE_URL = f"https://{SERVER_ADDRESS}:{SERVER_PORT}/api"
SIO_URL = f"https://{SERVER_ADDRESS}:{SERVER_PORT}" # python-socketio uses http/https for connect

# --- Global State (Illustrative - will be refined) ---
sio_client = socketio.AsyncClient(logger=True, engineio_logger=True)
current_user_info = None
active_page_controls = {} # To manage controls on different 'pages'

# --- SocketIO Event Handlers (will be defined later) ---
@sio_client.event
async def connect():
    print("Socket.IO connected successfully!")
    # We might need to send an auth token or re-verify session here if not handled by cookies

@sio_client.event
async def disconnect():
    print("Socket.IO disconnected.")

@sio_client.event
async def connect_error(data):
    print(f"Socket.IO connection failed: {data}")

# Add more handlers as needed: new_message, user_joined_voice, voice_signal etc.

# --- Main Application Logic ---
async def main(page: ft.Page):
    page.title = "Voice/Text Chat Client"
    page.vertical_alignment = ft.MainAxisAlignment.CENTER
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER

    status_text = ft.Text() # To display login status or errors

    async def attempt_login(e):
        username = username_field.value
        password = password_field.value
        status_text.value = "Logging in..."
        login_button.disabled = True
        register_button.disabled = True
        page.update()

        login_payload = {"username": username, "password": password}
        
        # Create a new SSL context that doesn't verify the certificate
        # WARNING: This is insecure and should ONLY be used for development/testing
        # with self-signed certificates. For production, use a proper CA-signed cert.
        # ssl_context = ssl.create_default_context()
        # ssl_context.check_hostname = False
        # ssl_context.verify_mode = ssl.CERT_NONE
        # connector = aiohttp.TCPConnector(ssl=ssl_context)

        # For now, let's try without custom SSL context for aiohttp, 
        # assuming the server's cert is valid or aiohttp handles it.
        # If you get SSL errors, we might need to revisit the connector above.
        
        try:
            async with aiohttp.ClientSession() as session: # Removed connector for now
                async with session.post(f"{API_BASE_URL}/login", json=login_payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("success"):
                            global current_user_info
                            current_user_info = data.get("user")
                            status_text.value = f"Login successful! Welcome, {current_user_info.get('username')}."
                            print(f"Logged in user: {current_user_info}")
                            
                            # Attempt to connect to Socket.IO
                            try:
                                await sio_client.connect(SIO_URL, wait_timeout=10)
                                # If connection successful, navigate to main app view
                                show_main_app_view(page) 
                            except socketio.exceptions.ConnectionError as sio_err:
                                status_text.value = f"Login OK, but Socket.IO connection failed: {sio_err}"
                                print(f"Socket.IO Connection Error: {sio_err}")
                        else:
                            status_text.value = f"Login failed: {data.get('message', 'Unknown error')}"
                    else:
                        error_text = await response.text()
                        status_text.value = f"Login request failed: {response.status} - {error_text}"
        except aiohttp.ClientConnectorSSLError as ssl_error:
            status_text.value = f"SSL Error: {ssl_error}. Ensure server SSL is correctly configured and accessible."
            print(f"SSL Error during login: {ssl_error}")
        except aiohttp.ClientError as http_err:
            status_text.value = f"HTTP Error: {http_err}. Check server and network."
            print(f"HTTP Error during login: {http_err}")
        except Exception as ex:
            status_text.value = f"An unexpected error occurred: {ex}"
            print(f"Unexpected error during login: {ex}")
        finally:
            login_button.disabled = False
            register_button.disabled = False
            page.update()

    async def show_register_view(e):
        # For now, just a placeholder, will implement actual registration UI later
        status_text.value = "Registration UI not yet implemented."
        page.update()

    # --- Login View Controls ---
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

    # --- Main App View (Placeholder) ---
    main_app_view_content = ft.Column(
        [
            ft.Text("Welcome to the App!", size=24),
            # More controls will go here: channel list, user list, chat area, etc.
            ft.ElevatedButton("Logout", on_click=lambda e: show_login_view(page)) # Logout re-shows login
        ],
        alignment=ft.MainAxisAlignment.CENTER,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        visible=False # Initially hidden
    )
    active_page_controls['main_app'] = main_app_view_content
    
    # --- View Navigation Logic ---
    def show_login_view(p: ft.Page):
        active_page_controls['main_app'].visible = False
        active_page_controls['login'].visible = True
        # Ensure SocketIO is disconnected if we are showing login view after a logout
        if sio_client.connected:
            # Create a task to disconnect to avoid blocking UI update
            # p.run_task(sio_client.disconnect) # Flet's way to run async tasks
            # For now, let's assume disconnect is handled elsewhere or not critical on simple logout
            pass
        p.update()

    def show_main_app_view(p: ft.Page):
        active_page_controls['login'].visible = False
        active_page_controls['main_app'].visible = True
        # Update main view with user-specific info if needed
        # For example: main_app_view_content.controls[0].value = f"Welcome, {current_user_info.get('username')}"
        p.update()
        
    # --- Initial Page Setup ---
    page.add(login_view_controls, main_app_view_content)
    show_login_view(page) # Start with the login view

# Run the Flet app
# To run this: save as flet_client.py and then in terminal: flet run flet_client.py
if __name__ == "__main__":
    ft.app(target=main) 