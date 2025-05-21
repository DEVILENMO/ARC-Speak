import flet as ft
import aiohttp # For making HTTP requests
import socketio # For SocketIO communication
import ssl
import json # For saving/loading config
import os # For checking config file existence
import inspect # For checking if a function is a coroutine
import asyncio # For running sync code in thread
import threading # For audio processing thread
import numpy as np # For RMS calculation
try:
    import sounddevice as sd
    SOUNDDEVICE_AVAILABLE = True
except Exception as e:
    print(f"Sounddevice library not found or failed to import: {e}. Audio device listing will be unavailable.")
    SOUNDDEVICE_AVAILABLE = False
    sd = None # Ensure sd is defined

# --- Color Palette ---
COLOR_PRIMARY_PURPLE = ft.Colors.DEEP_PURPLE
COLOR_TEXT_DARK_PURPLE = ft.Colors.DEEP_PURPLE_900 # New very dark purple for text
COLOR_TEXT_ON_PURPLE = ft.Colors.WHITE
COLOR_BACKGROUND_WHITE = ft.Colors.WHITE
COLOR_TEXT_ON_WHITE = COLOR_TEXT_DARK_PURPLE # Changed from ft.Colors.BLACK
COLOR_BORDER = COLOR_TEXT_DARK_PURPLE        # Changed from ft.Colors.BLACK
COLOR_DIVIDER_ON_WHITE = ft.Colors.GREY_300
COLOR_DIVIDER_ON_PURPLE = ft.Colors.with_opacity(0.5, ft.Colors.WHITE)
COLOR_INPUT_FIELD_BG_FILLED = ft.Colors.with_opacity(0.05, COLOR_PRIMARY_PURPLE) # Subtle purple tint
COLOR_ICON_ON_WHITE = COLOR_TEXT_DARK_PURPLE # Changed from ft.Colors.BLACK
COLOR_ICON_ON_PURPLE = ft.Colors.WHITE
COLOR_BUTTON_TEXT = ft.Colors.WHITE 
COLOR_STATUS_TEXT_MUTED = ft.Colors.GREY_600

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
is_mic_muted = False # Added global state for mic mute
selected_input_device_id = None # Added
selected_output_device_id = None # Added

# Mic Test Specific Globals
is_mic_testing = False 
current_mic_test_volume: float = 0.0
mic_test_volume_lock = threading.Lock()
mic_test_thread: threading.Thread = None
mic_test_stop_event = threading.Event()
mic_test_ui_update_task: asyncio.Task = None

# --- Audio Streaming Globals ---
is_sending_audio = False
audio_stream_thread: threading.Thread = None
audio_stream_stop_event = threading.Event()
AUDIO_RMS_THRESHOLD = 0.02 # Tune this threshold for VAD (e.g. 0.01 to 0.1)

# --- Audio Playback Globals ---
audio_output_stream: sd.OutputStream = None
audio_output_buffer = asyncio.Queue()
is_audio_playback_active: bool = False
# It's good practice to define a fixed playback samplerate, or ensure it matches input if possible.
# For simplicity, let's assume a common samplerate like 48000 for output.
# The server should ideally inform clients of the audio format, or clients agree on one.
PLAYBACK_SAMPLERATE = 48000 

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

# --- Audio Device Helper (Sync) ---
def _get_audio_devices_sync():
    if not SOUNDDEVICE_AVAILABLE or sd is None:
        return [], [] # Return empty lists if sounddevice is not available
    try:
        devices = sd.query_devices()
        input_devices = []
        output_devices = []
        default_input_idx = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
        default_output_idx = sd.default.device[1] if isinstance(sd.default.device, (list, tuple)) else sd.default.device

        for i, device in enumerate(devices):
            device_name = f"{device['name']} ({sd.query_hostapis(device['hostapi'])['name']})"
            if i == default_input_idx and device['max_input_channels'] > 0:
                # Prepend '(Default)' to the default input device name
                input_devices.insert(0, {'id': i, 'name': f"(Default) {device_name}"})
            elif device['max_input_channels'] > 0:
                input_devices.append({'id': i, 'name': device_name})
            
            if i == default_output_idx and device['max_output_channels'] > 0:
                 # Prepend '(Default)' to the default output device name
                output_devices.insert(0, {'id': i, 'name': f"(Default) {device_name}"})
            elif device['max_output_channels'] > 0:
                output_devices.append({'id': i, 'name': device_name})
        
        # Ensure the default marked item is truly at the top if it wasn't added first due to iteration order
        # This is a bit redundant given the insert(0,...) logic but as a safeguard.
        input_devices.sort(key=lambda x: not x['name'].startswith('(Default)'))
        output_devices.sort(key=lambda x: not x['name'].startswith('(Default)'))

        return input_devices, output_devices
    except Exception as e:
        print(f"Error querying audio devices: {e}")
        return [], [] # Return empty on error

# --- Mic Test Audio Processing (Run in a separate thread) ---
def _mic_test_audio_callback(indata, outdata, frames, time, status):
    global current_mic_test_volume, mic_test_volume_lock
    if status:
        print(f"Mic Test Callback Status: {status}")
    outdata[:] = indata # Loopback
    volume_norm = np.linalg.norm(indata) * 10 # Arbitrary scaling for better visibility
    with mic_test_volume_lock:
        current_mic_test_volume = min(1.0, volume_norm) # Cap at 1.0 for progress bar

def _run_mic_test_loop(page_instance: ft.Page, input_dev_id: int, output_dev_id: int, stop_event: threading.Event):
    global current_mic_test_volume, mic_test_volume_lock
    stream = None
    try:
        samplerate = sd.query_devices(input_dev_id, 'input')['default_samplerate']
        # If output_dev_id is None, sounddevice will try to use the system default output.
        stream = sd.Stream(
            device=(input_dev_id, output_dev_id),
            samplerate=samplerate,
            channels=1, # Mono for simplicity
            callback=_mic_test_audio_callback,
            blocksize=0 # Let sounddevice choose, or specify (e.g., 1024)
        )
        stream.start()
        print("Mic test audio stream started.")
        while not stop_event.is_set():
            sd.sleep(100) # Keep thread alive while stream is running, check stop event periodically
        print("Mic test stop event received.")

    except Exception as e:
        print(f"Error in mic test audio loop: {e}")
        error_message = f"Mic Test Error: {str(e)[:50]}..."
        async def show_error_async(): # Helper to call async method from thread
            sb = ft.SnackBar(ft.Text(error_message, color=COLOR_TEXT_ON_WHITE), bgcolor=ft.Colors.RED_ACCENT_700, open=True)
            page_instance.overlay.append(sb)
            page_instance.update()
        if page_instance: # Check if page_instance is valid
             asyncio.run_coroutine_threadsafe(show_error_async(), page_instance.loop)       

    finally:
        if stream:
            try:
                stream.stop()
                stream.close()
                print("Mic test audio stream stopped and closed.")
            except Exception as e:
                print(f"Error stopping/closing mic test stream: {e}")
        with mic_test_volume_lock:
            current_mic_test_volume = 0.0 # Reset volume on stop
        print("Mic test loop finished.")

# --- Audio Streaming Functions (Voice Chat) ---
def _audio_stream_callback(indata, frames, time, status):
    """Callback for the main audio input stream."""
    global is_mic_muted, current_voice_channel_id, sio_client, AUDIO_RMS_THRESHOLD, page, is_sending_audio

    if status:
        # Log significant statuses, but be mindful of log spam
        if status.input_overflow or status.input_underflow or status.output_overflow or status.output_underflow:
            print(f"Audio Stream Callback Status Warning: {status}")

    if not is_sending_audio or not is_actively_in_voice_channel or current_voice_channel_id is None or not sio_client or not sio_client.connected or not page or not page.loop:
        return

    if is_mic_muted:
        # If muted, ensure server knows we are not speaking.
        # This might be redundant if user_speaking_status is regularly sent,
        # but good for ensuring state consistency on mute toggle.
        try:
            asyncio.run_coroutine_threadsafe(
                sio_client.emit('user_speaking_status', {
                    'channel_id': current_voice_channel_id,
                    'speaking': False
                }),
                page.loop
            )
        except Exception as e:
            print(f"Error emitting speaking status (muted) from audio callback: {e}")
        return

    # VAD: Calculate RMS of the input data
    # indata is a NumPy array. Assuming mono, so indata.shape might be (n_frames, 1) or (n_frames,)
    rms = np.linalg.norm(indata) / np.sqrt(len(indata.flat)) # Use .flat for robust length calculation
    is_currently_speaking = rms > AUDIO_RMS_THRESHOLD

    # Emit speaking status
    try:
        asyncio.run_coroutine_threadsafe(
            sio_client.emit('user_speaking_status', {
                'channel_id': current_voice_channel_id,
                'speaking': is_currently_speaking
            }),
            page.loop
        )
    except Exception as e:
        print(f"Error emitting speaking status from audio callback: {e}")

    if is_currently_speaking:
        # Send audio data (raw PCM as list of floats for now)
        # Assuming mono, take the first channel or flatten if already mono
        audio_data_list = indata[:, 0].tolist() if indata.ndim > 1 else indata.tolist()
        try:
            asyncio.run_coroutine_threadsafe(
                sio_client.emit('voice_data_stream', {
                    'channel_id': current_voice_channel_id,
                    'audio_data': audio_data_list
                }),
                page.loop
            )
        except Exception as e:
            print(f"Error emitting voice data stream from audio callback: {e}")

def _run_audio_stream_loop(input_dev_id: int, stop_event: threading.Event, page_instance_ref: ft.Page):
    """Runs the audio capture and transmission loop in a separate thread."""
    global is_sending_audio, sio_client, current_voice_channel_id # page_instance_ref is 'page'
    stream = None
    try:
        if not SOUNDDEVICE_AVAILABLE or sd is None:
            print("Sounddevice not available for audio streaming.")
            # Optionally notify UI, but this check should ideally happen before starting the thread
            return

        samplerate = sd.query_devices(input_dev_id, 'input')['default_samplerate']
        
        # For InputStream, callback should not block for long.
        # blocksize=0 lets sounddevice choose an optimal size.
        # A common blocksize for voice is around 20ms of audio data.
        # e.g., for 48000 Hz, 20ms is 960 frames. sd.default.blocksize might be a good start.
        # Using a smaller blocksize (e.g. 480 frames for 10ms @ 48kHz) can reduce latency for VAD and transmission.
        stream = sd.InputStream(
            device=input_dev_id,
            samplerate=samplerate,
            channels=1, # Mono for simplicity
            callback=_audio_stream_callback,
            blocksize=int(samplerate * 0.02) # Approx 20ms blocks, adjust as needed
        )
        stream.start()
        print(f"Audio stream started for input device {input_dev_id} with samplerate {samplerate} and blocksize {stream.blocksize}.")
        # is_sending_audio = True # Set by the caller before starting the thread usually

        while not stop_event.is_set():
            if not is_sending_audio: # Additional check in case flag is set externally
                print("is_sending_audio became false, stopping stream loop.")
                break
            sd.sleep(100) # Keep thread alive; callback does the work. Check stop event periodically.
        
        print("Audio stream stop event received or is_sending_audio is false.")

    except Exception as e:
        print(f"Error in audio stream loop: {e}")
        error_message = f"Audio Stream Error: {str(e)[:100]}..."
        if page_instance_ref and page_instance_ref.loop and hasattr(page_instance_ref, 'overlay') and hasattr(page_instance_ref, 'update'):
            async def show_error_async():
                sb = ft.SnackBar(ft.Text(error_message, color=COLOR_TEXT_ON_WHITE), bgcolor=ft.Colors.RED_ACCENT_700, open=True)
                page_instance_ref.overlay.append(sb)
                page_instance_ref.update()
            asyncio.run_coroutine_threadsafe(show_error_async(), page_instance_ref.loop)
    finally:
        if stream:
            try:
                if stream.active: # Check if stream is active before stopping
                    stream.stop()
                stream.close()
                print("Audio stream stopped and closed.")
            except Exception as e:
                print(f"Error stopping/closing audio stream: {e}")
        
        # Ensure is_sending_audio is false when loop exits
        # is_sending_audio = False # Caller should manage this more directly usually

        # Send a final "not speaking" status if client was connected and in a channel
        if sio_client and sio_client.connected and current_voice_channel_id is not None and \
           page_instance_ref and page_instance_ref.loop and is_actively_in_voice_channel : # Check if still active
            try:
                print("Sending final 'not speaking' status from audio loop finally block.")
                asyncio.run_coroutine_threadsafe(
                    sio_client.emit('user_speaking_status', {
                        'channel_id': current_voice_channel_id,
                        'speaking': False
                    }),
                    page_instance_ref.loop
                )
            except Exception as e:
                print(f"Error sending final speaking status: {e}")
        print("Audio stream loop finished.")

# --- Audio Playback Functions (Receiving Voice Chat) ---
def _audio_playback_callback(outdata, frames, time, status):
    """Callback for the audio output stream."""
    global audio_output_buffer, is_audio_playback_active

    if status.output_underflow:
        print("Output underflow: Playback stream isn't getting data fast enough!")
    if status:
        # print(f"Audio Playback Callback Status: {status}") # Can be very verbose
        pass 

    if not is_audio_playback_active:
        outdata[:] = 0 # Output silence if playback is not active
        return

    try:
        # Try to get a block of data from the buffer without waiting indefinitely
        data_block = audio_output_buffer.get_nowait()
        # Ensure data_block is a NumPy array and has the correct shape for outdata
        if not isinstance(data_block, np.ndarray):
            # This shouldn't happen if we put NumPy arrays into the queue
            print(f"Warning: Data in playback buffer is not a NumPy array. Type: {type(data_block)}")
            outdata[:] = 0
            return

        # Check if the retrieved block is shorter than required frames
        if len(data_block) < frames:
            # Pad with zeros if data is shorter
            outdata[:len(data_block)] = data_block.reshape(-1, 1) # Reshape to (n, 1) for mono
            outdata[len(data_block):] = 0
            # print(f"Playback: Got {len(data_block)} frames, needed {frames}. Padded.") # Debug
        else:
            # If data_block is long enough, use the required number of frames
            outdata[:] = data_block[:frames].reshape(-1, 1) # Reshape to (n, 1) for mono
            # Put the rest back if it's too long (though ideally blocks are pre-sized)
            if len(data_block) > frames:
                # This part can be tricky with asyncio.Queue; simpler to assume blocks are consumable
                # print(f"Warning: Playback data_block {len(data_block)} longer than frames {frames}. Truncating.")
                pass # For now, just use the frames needed
        audio_output_buffer.task_done() # Notify queue that item processing is complete
    except asyncio.QueueEmpty:
        # Buffer is empty, output silence
        outdata[:] = 0
        # print("Playback buffer empty, outputting silence.") # Debug
    except Exception as e:
        print(f"Error in audio playback callback: {e}")
        outdata[:] = 0 # Output silence on error

async def _start_audio_playback_stream(page_ref: ft.Page, output_device_idx: int = None):
    """Helper function to start the audio playback stream."""
    global audio_output_stream, is_audio_playback_active, PLAYBACK_SAMPLERATE, sd, SOUNDDEVICE_AVAILABLE

    if not SOUNDDEVICE_AVAILABLE or sd is None:
        print("Cannot start audio playback: Sounddevice not available.")
        # Optionally show a snackbar if page_ref is valid
        return

    # Determine output device
    actual_output_device_id = output_device_idx
    if actual_output_device_id is None: # If no specific device, try to use sounddevice's default
        try:
            default_devices = sd.default.device
            actual_output_device_id = default_devices[1] if isinstance(default_devices, (list, tuple)) and len(default_devices) > 1 else default_devices
            print(f"No output device specified for playback, using system default: {actual_output_device_id}")
        except Exception as e:
            print(f"Error getting default output device: {e}. Playback cannot start.")
            # Show snackbar error
            if hasattr(page_ref, 'overlay'):
                sb = ft.SnackBar(ft.Text("Failed to get default audio output device.", color=COLOR_TEXT_ON_WHITE), bgcolor=ft.Colors.RED_ACCENT_700, open=True)
                page_ref.overlay.append(sb)
                if hasattr(page_ref, 'update'): page_ref.update()
            return

    if is_audio_playback_active and audio_output_stream and audio_output_stream.active:
        print("Audio playback stream is already active.")
        return

    try:
        # Clear the buffer before starting a new stream
        while not audio_output_buffer.empty():
            audio_output_buffer.get_nowait()
            audio_output_buffer.task_done()
        print("Audio output buffer cleared before starting playback stream.")

        # Query device for its default samplerate if using a specific device, 
        # otherwise use PLAYBACK_SAMPLERATE (e.g. 48000)
        # For simplicity, we'll use a fixed PLAYBACK_SAMPLERATE. 
        # The server should ideally send audio in a consistent format, or transcode.
        # If server sends variable samplerates, client needs to resample or reinitialize stream.
        
        # Blocksize for output can also be chosen, e.g., matching input or a fixed duration
        # Using sd.default.blocksize or a fixed value like int(PLAYBACK_SAMPLERATE * 0.02) (20ms)
        # If None, sounddevice will choose.

        audio_output_stream = sd.OutputStream(
            device=actual_output_device_id,
            samplerate=PLAYBACK_SAMPLERATE, # Fixed playback sample rate
            channels=1, # Mono playback
            callback=_audio_playback_callback,
            blocksize=0 # Let sounddevice choose, or set (e.g., int(PLAYBACK_SAMPLERATE * 0.02))
        )
        audio_output_stream.start()
        is_audio_playback_active = True
        print(f"Audio playback stream started on device {actual_output_device_id} with samplerate {PLAYBACK_SAMPLERATE} and blocksize {audio_output_stream.blocksize}.")
    except Exception as e:
        is_audio_playback_active = False
        print(f"Error starting audio playback stream: {e}")
        if hasattr(page_ref, 'overlay'):
            sb = ft.SnackBar(ft.Text(f"Audio Playback Error: {str(e)[:50]}...", color=COLOR_TEXT_ON_WHITE), bgcolor=ft.Colors.RED_ACCENT_700, open=True)
            page_ref.overlay.append(sb)
            if hasattr(page_ref, 'update'): page_ref.update()

async def _stop_audio_playback_stream_if_running():
    """Helper function to stop the audio playback stream if it's running."""
    global audio_output_stream, is_audio_playback_active

    if is_audio_playback_active and audio_output_stream:
        print("Stopping audio playback stream...")
        try:
            if audio_output_stream.active:
                audio_output_stream.stop()
            audio_output_stream.close()
            print("Audio playback stream stopped and closed.")
        except Exception as e:
            print(f"Error stopping/closing audio playback stream: {e}")
        finally:
            audio_output_stream = None
            is_audio_playback_active = False
            # Clear the buffer on stop
            while not audio_output_buffer.empty():
                try:
                    audio_output_buffer.get_nowait()
                    audio_output_buffer.task_done()
                except asyncio.QueueEmpty:
                    break # Should not happen if not empty check is reliable
            print("Audio output buffer cleared after stopping playback stream.")
    else:
        is_audio_playback_active = False # Ensure flag is reset
        print("Audio playback stream was not running or already stopped.")

# --- Main Application Logic ---
async def main(page: ft.Page):
    page.title = "Voice/Text Chat Client"
    page.padding = 0
    page.bgcolor = COLOR_BACKGROUND_WHITE # Global page background
    page.theme_mode = ft.ThemeMode.LIGHT # Explicitly set to light
    app_config = load_config()

    global sio_client, shared_aiohttp_session, selected_input_device_id, selected_output_device_id
    global is_mic_testing, mic_test_thread, mic_test_stop_event, mic_test_ui_update_task, current_mic_test_volume, mic_test_volume_lock
    custom_ssl_context = ssl.create_default_context()
    custom_ssl_context.check_hostname = False
    custom_ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=custom_ssl_context)
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    shared_aiohttp_session = aiohttp.ClientSession(connector=connector, cookie_jar=cookie_jar)
    sio_client = socketio.AsyncClient(http_session=shared_aiohttp_session, logger=True, engineio_logger=True)

    def update_voice_panel_button_visibility():
        global page # 确保 page 对象可访问以用于更新
        print(f"[DEBUG] update_voice_panel_button_visibility: previewing_vc_id={previewing_voice_channel_id}, is_active={is_actively_in_voice_channel}") # 调试打印当前状态
        confirm_join_btn = active_page_controls.get('confirm_join_voice_button')
        leave_voice_btn = active_page_controls.get('leave_voice_button')
        voice_settings_ctrl = active_page_controls.get('voice_settings_area')

        if previewing_voice_channel_id is not None: # 如果正在预览某个语音频道
            if is_actively_in_voice_channel: # 如果用户已主动加入此语音频道
                if confirm_join_btn: confirm_join_btn.visible = False
                if leave_voice_btn: leave_voice_btn.visible = True
                if voice_settings_ctrl: voice_settings_ctrl.visible = True
            else: # 用户正在预览此语音频道，但未主动加入
                if confirm_join_btn: confirm_join_btn.visible = True
                if leave_voice_btn: leave_voice_btn.visible = False
                if voice_settings_ctrl: voice_settings_ctrl.visible = False
        else: # 用户没有预览任何语音频道 (例如，当前在文本频道视图)
            if confirm_join_btn: confirm_join_btn.visible = False
            if leave_voice_btn: leave_voice_btn.visible = False
            if voice_settings_ctrl: voice_settings_ctrl.visible = False
        
        # 设置完 visible 属性后，打印这些控件的 visible 状态以供调试
        if confirm_join_btn: print(f"  [DEBUG BTN] confirm_join_btn.visible = {confirm_join_btn.visible}")
        if leave_voice_btn: print(f"  [DEBUG BTN] leave_voice_btn.visible = {leave_voice_btn.visible}")
        if voice_settings_ctrl: print(f"  [DEBUG CTRL] voice_settings_ctrl.visible = {voice_settings_ctrl.visible}")

        # 单独更新每个相关控件的UI
        if confirm_join_btn and hasattr(confirm_join_btn, 'update'): confirm_join_btn.update()
        if leave_voice_btn and hasattr(leave_voice_btn, 'update'): leave_voice_btn.update()
        if voice_settings_ctrl and hasattr(voice_settings_ctrl, 'update'): voice_settings_ctrl.update()
        
        # 如果单个控件更新不够，可以考虑调用 page.update()，但这可能会刷新整个页面，通常应避免
        # if hasattr(page, 'update'): page.update()

    def update_voice_channel_user_list_ui():
        # This function now updates the list for the PREVIEWING or ACTIVE voice channel
        vc_users_list_ctrl = active_page_controls.get('voice_channel_internal_users_list')
        if not vc_users_list_ctrl: return

        vc_user_controls = []
        # current_voice_channel_active_users should hold users for previewing_voice_channel_id
        sorted_users_in_vc = sorted(current_voice_channel_active_users.values(), key=lambda u: u.get('username', '').lower())
        
        for user_data in sorted_users_in_vc:
            is_user_actually_speaking = is_actively_in_voice_channel and user_data.get('speaking', False)

            if is_user_actually_speaking:
                user_card_text_color = COLOR_TEXT_ON_PURPLE # Should be ft.Colors.WHITE
                user_card_icon_color = COLOR_ICON_ON_PURPLE   # Should be ft.Colors.WHITE
                user_card_icon_name = ft.Icons.RECORD_VOICE_OVER
                user_card_bgcolor = COLOR_PRIMARY_PURPLE
                user_card_border = None 
            else:
                user_card_text_color = COLOR_TEXT_ON_WHITE # Should be COLOR_TEXT_DARK_PURPLE
                user_card_icon_color = COLOR_ICON_ON_WHITE   # Should be COLOR_TEXT_DARK_PURPLE
                user_card_icon_name = ft.Icons.VOICE_OVER_OFF
                user_card_bgcolor = COLOR_BACKGROUND_WHITE 
                user_card_border = ft.border.all(1, COLOR_DIVIDER_ON_WHITE) # Grey outline

            user_display_row = ft.Row(
                [
                    ft.Icon(name=user_card_icon_name, color=user_card_icon_color, size=16),
                    ft.Text(user_data.get('username', 'Unknown'), color=user_card_text_color, weight=ft.FontWeight.NORMAL, size=12)
                ],
                alignment=ft.MainAxisAlignment.START,
                spacing=5,
                # vertical_alignment=ft.CrossAxisAlignment.CENTER # Ensure elements within the row are centered vertically
            )

            user_card = ft.Container(
                content=user_display_row,
                bgcolor=user_card_bgcolor,
                border=user_card_border,
                border_radius=ft.border_radius.all(4), # Slightly rounded corners
                padding=ft.padding.symmetric(vertical=4, horizontal=6), # Adjusted padding
                margin=ft.margin.only(bottom=4) # Space between user cards
            )
            vc_user_controls.append(user_card)

        vc_users_list_ctrl.controls = vc_user_controls
        if hasattr(vc_users_list_ctrl, 'update'): vc_users_list_ctrl.update()

        topic_display = active_page_controls.get('voice_channel_topic_display')
        if topic_display and previewing_voice_channel_id and voice_channels_data.get(previewing_voice_channel_id):
            ch_name = voice_channels_data[previewing_voice_channel_id]['name']
            prefix = "Voice:" if is_actively_in_voice_channel else "Preview:"
            topic_display.value = f"{prefix} {ch_name}"
            # topic_display.color = COLOR_TEXT_ON_WHITE # Already handled as it's part of middle panel
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
            active_page_controls['chat_messages_view'].controls.append(
                ft.Text(f"[{data.get('timestamp')}] {data.get('username')}: {data.get('content')}", 
                        selectable=True, 
                        font_family="Consolas", 
                        color=COLOR_TEXT_ON_WHITE # Explicitly set text color for messages
                       ) 
            )
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
    async def voice_data_stream_chunk(data):
        """Handles incoming audio data chunks from other users."""
        global audio_output_buffer, is_audio_playback_active, current_user_info, page, selected_output_device_id

        if not is_audio_playback_active and is_actively_in_voice_channel:
            # If we are in a voice channel but playback isn't active, try to start it.
            # This might happen if it failed to start initially or was stopped due to an error.
            print("Audio playback is not active while in a voice channel. Attempting to start playback stream.")
            # Use selected_output_device_id, which _start_audio_playback_stream can use or fallback to default
            await _start_audio_playback_stream(page, selected_output_device_id) 

        if not is_audio_playback_active: # Check again, if start failed, don't process
            # print("Still no active playback stream after attempting to start. Discarding audio chunk.")
            return
        
        sender_user_id = data.get('user_id')
        # Crucial: Do not play back our own audio if server somehow sends it back
        if current_user_info and sender_user_id == current_user_info.get('id'):
            return

        audio_chunk_list = data.get('audio_data')
        if audio_chunk_list and isinstance(audio_chunk_list, list):
            try:
                # Convert list of floats to NumPy array
                audio_np_array = np.array(audio_chunk_list, dtype=np.float32)
                await audio_output_buffer.put(audio_np_array)
                # print(f"Received audio chunk from user {sender_user_id}, size: {len(audio_np_array)}, buffer size: {audio_output_buffer.qsize()}") # Debug
            except Exception as e:
                print(f"Error processing or queuing audio chunk: {e}")
        # else:
            # print(f"Received voice_data_stream_chunk with invalid audio_data from user {sender_user_id}")
    
    @sio_client.event
    async def error(data):
        if active_page_controls.get('main_status_bar'): active_page_controls['main_status_bar'].value = f"Error: {data.get('message')}"
        if hasattr(page, 'update'): page.update()

    @sio_client.event
    async def server_user_list_update(data):
        global all_server_users
        all_server_users = data
        if active_page_controls.get('server_users_list_view'):
            controls = [ft.Row([ft.Icon(name=ft.Icons.CIRCLE, color=ft.Colors.GREEN_ACCENT_700, size=10), ft.Text(u.get('username','N/A'), color=COLOR_TEXT_ON_WHITE)], alignment=ft.MainAxisAlignment.START, spacing=5) for u in sorted(all_server_users, key=lambda u: u.get('username', '').lower())]
            active_page_controls['server_users_list_view'].controls = controls
            active_page_controls['server_users_list_view'].update()

    async def _update_mic_test_bar_task_loop():
        global is_mic_testing, current_mic_test_volume, mic_test_volume_lock
        mic_test_bar = active_page_controls.get('voice_settings_mic_test_bar')
        if not mic_test_bar:
            return
        print("Mic test UI update loop started.")
        try:
            while is_mic_testing:
                with mic_test_volume_lock:
                    volume = current_mic_test_volume
                mic_test_bar.value = volume
                if hasattr(mic_test_bar, "update"): mic_test_bar.update()
                await asyncio.sleep(0.05) # Update roughly 20 times per second
        except asyncio.CancelledError:
            print("Mic test UI update loop cancelled.")
        finally:
            if mic_test_bar: # Reset bar on exit
                mic_test_bar.value = 0
                if hasattr(mic_test_bar, "update"): mic_test_bar.update()
            print("Mic test UI update loop finished.")

    async def populate_audio_device_dropdowns():
        global selected_input_device_id, selected_output_device_id, page # Added page
        print("Attempting to populate audio device dropdowns...")
        input_dropdown = active_page_controls.get('voice_settings_input_device_dropdown')
        output_dropdown = active_page_controls.get('voice_settings_output_device_dropdown')

        if not input_dropdown or not output_dropdown:
            print("Audio dropdown controls not found.")
            return

        current_config = load_config()
        saved_input_id = current_config.get("saved_input_device_id")
        saved_output_id = current_config.get("saved_output_device_id")
        print(f"Loaded saved device IDs - Input: {saved_input_id}, Output: {saved_output_id}")

        # Convert saved IDs to int if they exist, None otherwise. This is crucial.
        # The IDs from sounddevice are integers. Storing them as int and comparing as int.
        if saved_input_id is not None:
            try:
                saved_input_id = int(saved_input_id)
            except ValueError:
                print(f"Warning: Could not convert saved_input_device_id '{saved_input_id}' to int. Ignoring.")
                saved_input_id = None
        
        if saved_output_id is not None:
            try:
                saved_output_id = int(saved_output_id)
            except ValueError:
                print(f"Warning: Could not convert saved_output_device_id '{saved_output_id}' to int. Ignoring.")
                saved_output_id = None

        if not SOUNDDEVICE_AVAILABLE:
            input_dropdown.options = [ft.dropdown.Option(key="-1", text="Audio N/A - Check Install")]
            output_dropdown.options = [ft.dropdown.Option(key="-1", text="Audio N/A - Check Install")]
            input_dropdown.value = "-1"
            output_dropdown.value = "-1"
            selected_input_device_id = None # Explicitly set global state
            selected_output_device_id = None # Explicitly set global state
            if hasattr(page, 'update'): page.update()
            return

        try:
            input_devices, output_devices = await asyncio.to_thread(_get_audio_devices_sync)
            print(f"Input devices found: {len(input_devices)}")
            print(f"Output devices found: {len(output_devices)}")

            input_dropdown.options.clear()
            applied_saved_input = False
            if not input_devices:
                input_dropdown.options.append(ft.dropdown.Option(key="-1", text="No Input Devices Found"))
                input_dropdown.value = "-1"
                selected_input_device_id = None
            else:
                for device in input_devices:
                    # Device IDs from sounddevice are integers. Store keys as strings for dropdown.
                    input_dropdown.options.append(ft.dropdown.Option(key=str(device['id']), text=device['name']))
                
                # Try to apply saved ID
                if saved_input_id is not None and any(device['id'] == saved_input_id for device in input_devices):
                    input_dropdown.value = str(saved_input_id)
                    selected_input_device_id = saved_input_id # Store as int
                    applied_saved_input = True
                    print(f"Applied saved input device ID: {saved_input_id}")
                
                if not applied_saved_input: # Fallback to default or first
                    default_input = next((d for d in input_devices if d['name'].startswith("(Default)")), None)
                    if default_input:
                        input_dropdown.value = str(default_input['id'])
                        selected_input_device_id = default_input['id'] # Store as int
                    elif input_devices: 
                        input_dropdown.value = str(input_devices[0]['id'])
                        selected_input_device_id = input_devices[0]['id'] # Store as int
                    print(f"Default/fallback input device ID: {selected_input_device_id}")
            
            output_dropdown.options.clear()
            applied_saved_output = False
            if not output_devices:
                output_dropdown.options.append(ft.dropdown.Option(key="-1", text="No Output Devices Found"))
                output_dropdown.value = "-1"
                selected_output_device_id = None
            else:
                for device in output_devices:
                    output_dropdown.options.append(ft.dropdown.Option(key=str(device['id']), text=device['name']))

                if saved_output_id is not None and any(device['id'] == saved_output_id for device in output_devices):
                    output_dropdown.value = str(saved_output_id)
                    selected_output_device_id = saved_output_id # Store as int
                    applied_saved_output = True
                    print(f"Applied saved output device ID: {saved_output_id}")

                if not applied_saved_output: # Fallback to default or first
                    default_output = next((d for d in output_devices if d['name'].startswith("(Default)")), None)
                    if default_output:
                        output_dropdown.value = str(default_output['id'])
                        selected_output_device_id = default_output['id'] # Store as int
                    elif output_devices: 
                        output_dropdown.value = str(output_devices[0]['id'])
                        selected_output_device_id = output_devices[0]['id'] # Store as int
                    print(f"Default/fallback output device ID: {selected_output_device_id}")

        except Exception as e:
            print(f"Error populating audio dropdowns: {e}")
            input_dropdown.options = [ft.dropdown.Option(key="-1", text="Error Loading Devices")]
            output_dropdown.options = [ft.dropdown.Option(key="-1", text="Error Loading Devices")]
            input_dropdown.value = "-1"; selected_input_device_id = None
            output_dropdown.value = "-1"; selected_output_device_id = None

        if hasattr(input_dropdown, 'update'): input_dropdown.update()
        if hasattr(output_dropdown, 'update'): output_dropdown.update()
        if hasattr(page, 'update'): page.update() # Ensure page is accessible here

    async def handle_save_audio_settings_click(e):
        global selected_input_device_id, selected_output_device_id, page # Added page
        current_config = load_config()
        
        # Ensure IDs are integers for saving, or None
        input_id_to_save = int(selected_input_device_id) if selected_input_device_id is not None else None
        output_id_to_save = int(selected_output_device_id) if selected_output_device_id is not None else None

        if input_id_to_save is not None:
            current_config["saved_input_device_id"] = input_id_to_save
        else: 
            current_config.pop("saved_input_device_id", None)
            
        if output_id_to_save is not None:
            current_config["saved_output_device_id"] = output_id_to_save
        else:
            current_config.pop("saved_output_device_id", None)

        save_config(current_config)
        print(f"Audio settings saved. Input ID: {input_id_to_save}, Output ID: {output_id_to_save}")
        
        if hasattr(page, 'overlay'): # Check if page and overlay are available
            sb = ft.SnackBar(
                ft.Text("Audio settings saved!", color=COLOR_TEXT_ON_WHITE),
                bgcolor=ft.Colors.with_opacity(0.8, COLOR_PRIMARY_PURPLE),
                open=True
            )
            page.overlay.append(sb)
            page.update()
        else:
            print("Page or page.overlay not available for SnackBar.")

    async def handle_input_device_change(e):
        global selected_input_device_id, is_mic_testing
        selected_input_device_id = int(e.control.value) if e.control.value and e.control.value != "-1" else None
        print(f"Selected Input Device ID: {selected_input_device_id}")
        if is_mic_testing: 
            await handle_mic_test_button_click(None) # Stop current test if ongoing
        # If actively in a voice channel and sending audio, restart audio stream with new device
        if is_actively_in_voice_channel and is_sending_audio and selected_input_device_id is not None:
            print("Input device changed while in voice. Restarting audio stream.")
            await _stop_audio_stream_if_running() # Stop existing stream
            await _start_audio_stream(page, selected_input_device_id) # Start new one
        elif is_actively_in_voice_channel and is_sending_audio and selected_input_device_id is None:
            print("Input device unselected while in voice. Stopping audio stream.")
            await _stop_audio_stream_if_running()
            # Optionally show a message that input device is required
            sb = ft.SnackBar(ft.Text("Input device unselected. Voice transmission stopped.", color=COLOR_TEXT_ON_WHITE), bgcolor=ft.Colors.ORANGE_ACCENT_700, open=True)
            if hasattr(page, 'overlay'): page.overlay.append(sb); page.update()

    async def handle_output_device_change(e):
        global selected_output_device_id, is_mic_testing, is_audio_playback_active, page
        new_output_device_id = int(e.control.value) if e.control.value and e.control.value != "-1" else None
        
        if selected_output_device_id != new_output_device_id:
            selected_output_device_id = new_output_device_id
            print(f"Selected Output Device ID: {selected_output_device_id}")

            if is_mic_testing: # If mic test is running, stop and restart it to use new output for loopback
                print("Output device changed during mic test. Restarting test.")
                await handle_mic_test_button_click(None) # Stop current test
                # User will need to manually start it again if they wish, as it might be disruptive.
                # Or, we could try to restart it automatically:
                # await handle_mic_test_button_click(None) # Start new test
            
            # If audio playback is active, restart it with the new device
            if is_audio_playback_active:
                print("Output device changed while audio playback is active. Restarting playback stream.")
                await _stop_audio_playback_stream_if_running()
                if selected_output_device_id is not None: # only restart if a valid device is selected
                    await _start_audio_playback_stream(page, selected_output_device_id)
                else:
                    print("No output device selected. Audio playback stopped.")
                    sb = ft.SnackBar(ft.Text("Output device unselected. Audio playback stopped.", color=COLOR_TEXT_ON_WHITE), bgcolor=ft.Colors.ORANGE_ACCENT_700, open=True)
                    if hasattr(page, 'overlay'): page.overlay.append(sb); page.update()
        # else: print("Output device selection did not change.")

    async def handle_mute_mic_click(e): 
        global is_mic_muted, current_voice_channel_id, sio_client
        is_mic_muted = not is_mic_muted
        
        mute_button = active_page_controls.get('voice_settings_mute_button')
        if mute_button:
            mute_button.icon = ft.Icons.MIC_OFF if is_mic_muted else ft.Icons.MIC
            mute_button.tooltip = "Unmute Microphone" if is_mic_muted else "Mute Microphone"
            mute_button.update()

        print(f"Mic muted state: {is_mic_muted}")

        if is_mic_muted and sio_client and sio_client.connected and current_voice_channel_id is not None:
            try:
                await sio_client.emit('user_speaking_status', {
                    'channel_id': current_voice_channel_id, 
                    'speaking': False 
                })
                print(f"Emitted user_speaking_status (false) due to mute for channel {current_voice_channel_id}")
            except Exception as ex:
                print(f"Error emitting user_speaking_status on mute: {ex}")
        
        if hasattr(page, 'update'): page.update()

    async def handle_mic_test_button_click(e):
        global is_mic_testing, selected_input_device_id, selected_output_device_id, mic_test_thread, mic_test_stop_event, mic_test_ui_update_task
        
        mic_test_btn = active_page_controls.get('voice_settings_mic_test_button')
        mic_test_bar = active_page_controls.get('voice_settings_mic_test_bar')

        if not mic_test_btn or not mic_test_bar:
            print("Mic test UI elements not found.")
            return

        if is_mic_testing: # If currently testing, stop it
            print("Attempting to stop mic test...")
            is_mic_testing = False # Signal UI loop to stop
            mic_test_stop_event.set() # Signal audio thread to stop
            
            if mic_test_ui_update_task and not mic_test_ui_update_task.done():
                mic_test_ui_update_task.cancel()
                try:
                    await mic_test_ui_update_task # Allow cancellation to complete
                except asyncio.CancelledError:
                    print("Mic test UI update task successfully cancelled.")
            mic_test_ui_update_task = None

            if mic_test_thread and mic_test_thread.is_alive():
                print("Waiting for mic test audio thread to join...")
                mic_test_thread.join(timeout=1.0) # Wait for thread to finish
                if mic_test_thread.is_alive():
                    print("Warning: Mic test audio thread did not join in time.")
            mic_test_thread = None
            
            mic_test_btn.text = "Start Mic Test"
            mic_test_btn.icon = ft.Icons.PLAY_ARROW
            mic_test_bar.value = 0
            print("Mic test stopped.")
        else: # If not testing, start it
            if not SOUNDDEVICE_AVAILABLE:
                sb = ft.SnackBar(ft.Text("Sounddevice library not available. Mic test disabled.", color=COLOR_TEXT_ON_WHITE), bgcolor=ft.Colors.RED_ACCENT_700, open=True)
                page.overlay.append(sb)
                page.update()
                return

            if selected_input_device_id is None:
                sb = ft.SnackBar(ft.Text("Please select an input device first.", color=COLOR_TEXT_ON_WHITE), bgcolor=ft.Colors.with_opacity(0.8, COLOR_PRIMARY_PURPLE),open=True)
                page.overlay.append(sb)
                page.update()
                return
            
            current_output_dev_id = selected_output_device_id
            if current_output_dev_id is None: # Try to get default output if none selected
                _, output_devices = _get_audio_devices_sync() # This is a sync call, but quick for this check
                default_output = next((d for d in output_devices if d['name'].startswith("(Default)")), None)
                if default_output:
                    current_output_dev_id = default_output['id']
                    print(f"Using default output device for mic test: {default_output['name']}")
                elif output_devices: # Fallback to first available if no explicit default
                    current_output_dev_id = output_devices[0]['id']
                    print(f"Using first available output device for mic test: {output_devices[0]['name']}")
                else:
                    sb = ft.SnackBar(ft.Text("No output device available for mic test loopback.", color=COLOR_TEXT_ON_WHITE), bgcolor=ft.Colors.RED_ACCENT_700,open=True)
                    page.overlay.append(sb)
                    page.update()
                    return

            is_mic_testing = True
            mic_test_stop_event.clear()
            mic_test_btn.text = "Stop Mic Test"
            mic_test_btn.icon = ft.Icons.STOP
            print(f"Starting mic test for input: {selected_input_device_id}, output: {current_output_dev_id}.")
            
            # Pass the current page instance to the thread for UI updates (errors)
            mic_test_thread = threading.Thread(target=_run_mic_test_loop, args=(page, selected_input_device_id, current_output_dev_id, mic_test_stop_event))
            mic_test_thread.daemon = True # Allow main program to exit even if thread is running
            mic_test_thread.start()

            if mic_test_ui_update_task is None or mic_test_ui_update_task.done():
                mic_test_ui_update_task = asyncio.create_task(_update_mic_test_bar_task_loop())
            else:
                print("Mic test UI update task seems to be already running or not None.")

        if hasattr(mic_test_btn, 'update'): mic_test_btn.update()
        if hasattr(mic_test_bar, 'update'): mic_test_bar.update() 
        # page.update() # May not be strictly needed if individual controls update

    async def _start_audio_stream(page_ref: ft.Page, input_device_id: int):
        """Helper function to start the audio stream."""
        global is_sending_audio, audio_stream_thread, audio_stream_stop_event

        if not SOUNDDEVICE_AVAILABLE or sd is None:
            print("Cannot start audio stream: Sounddevice not available.")
            sb = ft.SnackBar(ft.Text("Audio system not available. Cannot send voice.", color=COLOR_TEXT_ON_WHITE), bgcolor=ft.Colors.RED_ACCENT_700, open=True)
            if hasattr(page_ref, 'overlay'): page_ref.overlay.append(sb); page_ref.update()
            return

        if input_device_id is None:
            print("Cannot start audio stream: No input device selected.")
            sb = ft.SnackBar(ft.Text("No input device selected for voice.", color=COLOR_TEXT_ON_WHITE), bgcolor=ft.Colors.RED_ACCENT_700, open=True)
            if hasattr(page_ref, 'overlay'): page_ref.overlay.append(sb); page_ref.update()
            return

        if is_sending_audio and audio_stream_thread and audio_stream_thread.is_alive():
            print("Audio stream already running. Not starting another.")
            return

        is_sending_audio = True
        audio_stream_stop_event.clear()
        audio_stream_thread = threading.Thread(
            target=_run_audio_stream_loop,
            args=(input_device_id, audio_stream_stop_event, page_ref)
        )
        audio_stream_thread.daemon = True
        audio_stream_thread.start()
        print(f"Audio streaming thread started for device ID: {input_device_id}")

    async def _stop_audio_stream_if_running():
        """Helper function to stop the audio stream if it's running."""
        global is_sending_audio, audio_stream_thread, audio_stream_stop_event

        if is_sending_audio and audio_stream_thread and audio_stream_thread.is_alive():
            print("Stopping audio stream...")
            is_sending_audio = False # Signal the loop to stop
            audio_stream_stop_event.set() # Signal the thread to stop
            await asyncio.to_thread(audio_stream_thread.join, timeout=1.0) # Wait for thread with timeout
            if audio_stream_thread.is_alive():
                print("Warning: Audio stream thread did not join in time.")
            audio_stream_thread = None
            print("Audio stream stopped.")
        else:
            # Ensure flag is reset even if thread was not alive or not an instance
            is_sending_audio = False 
            print("Audio stream was not running or already stopped.")

    active_page_controls['status_text'] = ft.Text(color=COLOR_STATUS_TEXT_MUTED) # Muted status text
    remember_me_checkbox = ft.Checkbox(label="记住我", value=app_config.get("remember_me", False),
                                        check_color=COLOR_PRIMARY_PURPLE,
                                        label_style=ft.TextStyle(color=COLOR_TEXT_ON_WHITE))

    async def _leave_current_voice_channel_if_any(page_ref: ft.Page, called_from_select_new_voice: bool = False):
        """
        Handles leaving the currently active or previewed voice channel.
        If called_from_select_new_voice is True, it means we are about to select a new voice channel,
        so we don't switch the middle panel to text, and we don't clear previewing_voice_channel_id yet.
        """
        global current_voice_channel_id, previewing_voice_channel_id, is_actively_in_voice_channel, current_voice_channel_active_users
        
        channel_id_to_leave_on_server = None
        was_actively_in_voice = is_actively_in_voice_channel

        # Only need to tell server to leave if we were *actively* in a channel
        if was_actively_in_voice and current_voice_channel_id is not None:
            channel_id_to_leave_on_server = current_voice_channel_id
            print(f"User was active in VC {current_voice_channel_id}. Preparing to leave on server.")
        # No 'else if previewing' because join is no longer sent on preview.

        if sio_client and sio_client.connected and channel_id_to_leave_on_server is not None:
            try:
                print(f"Client emitting leave_voice_channel for channel_id: {channel_id_to_leave_on_server} (due to leaving active voice session)")
                await sio_client.emit('leave_voice_channel', {'channel_id': channel_id_to_leave_on_server})
            except Exception as e:
                print(f"Error emitting leave_voice_channel: {e}")
        
        # Stop audio streaming if it was active
        if was_actively_in_voice:
            await _stop_audio_stream_if_running()
            await _stop_audio_playback_stream_if_running() # Stop playback when leaving active voice

        is_actively_in_voice_channel = False
        current_voice_channel_id = None # Always reset this when leaving active state
        
        if not called_from_select_new_voice: # If true, new preview ID will be set by caller
            previewing_voice_channel_id = None 
        
        current_voice_channel_active_users.clear()
        
        # UI Updates
        if active_page_controls.get('current_voice_channel_text'):
            active_page_controls['current_voice_channel_text'].value = "Not in voice"
            active_page_controls['current_voice_channel_text'].update()

        update_voice_channel_user_list_ui() # This will clear the list and update buttons via its internal call

        if not called_from_select_new_voice and not was_actively_in_voice : # if just previewing and now leaving preview (not to another voice chan)
            # If we were only previewing and now we are leaving that preview (not to switch to another voice channel)
            # then switch to a default text view if available.
            current_text_channel_name = "Select a text channel"
            if current_text_channel_id and text_channels_data.get(current_text_channel_id):
                current_text_channel_name = text_channels_data[current_text_channel_id]['name']
            else: # select first text channel if any
                first_text_ch = next(iter(text_channels_data.values()), None)
                if first_text_ch:
                    current_text_channel_id = first_text_ch['id']
                    current_text_channel_name = first_text_ch['name']
                    # No need to emit join_text_channel here, switch_middle_panel_view will handle view
                    # and if user explicitly clicks it, select_text_channel will handle emit.
            switch_middle_panel_view("text", current_text_channel_name)

        # update_voice_panel_button_visibility() # Called by update_voice_channel_user_list_ui
        if hasattr(page_ref, 'update'): page_ref.update()
        print(f"_leave_current_voice_channel_if_any executed. called_from_select_new_voice: {called_from_select_new_voice}")


    def switch_middle_panel_view(view_type: str, channel_name: str = ""):
        is_text_view = view_type == "text"
        if active_page_controls.get('chat_panel_content_group'): 
            active_page_controls['chat_panel_content_group'].visible = is_text_view
            active_page_controls['chat_panel_content_group'].update()
        if active_page_controls.get('voice_panel_content_group'): 
            active_page_controls['voice_panel_content_group'].visible = not is_text_view
            active_page_controls['voice_panel_content_group'].update()

        if is_text_view:
            if active_page_controls.get('current_chat_topic'): 
                active_page_controls['current_chat_topic'].value = f"Chat - {channel_name}"
                active_page_controls['current_chat_topic'].update()
        else: # Voice view
            # Voice panel topic display is handled by update_voice_channel_user_list_ui
            pass
        
        update_voice_panel_button_visibility() # This is crucial after view switch
        # if hasattr(page, 'update'): page.update() # Button visibility should handle its own page update


    async def select_text_channel(page_ref: ft.Page, channel_id: int, channel_name: str):
        global current_text_channel_id
        
        chat_panel = active_page_controls.get('chat_panel_content_group')
        # If already on this text channel and its view is visible, do nothing
        if current_text_channel_id == channel_id and chat_panel and chat_panel.visible:
            return

        print(f"Selecting text channel: {channel_name} (ID: {channel_id})")
        current_text_channel_id = channel_id
        
        # Switch middle panel to text view
        switch_middle_panel_view("text", channel_name)
        
        if active_page_controls.get('chat_messages_view'):
            active_page_controls['chat_messages_view'].controls.clear()
            active_page_controls['chat_messages_view'].update()
            
        if sio_client and sio_client.connected:
            try:
                await sio_client.emit('join_text_channel', {'channel_id': channel_id})
            except Exception as e:
                print(f"Error emitting join_text_channel: {e}")

        # Update the top bar if user is actively in a voice channel
        if is_actively_in_voice_channel and current_voice_channel_id and voice_channels_data.get(current_voice_channel_id):
            vc_name = voice_channels_data[current_voice_channel_id]['name']
            if active_page_controls.get('current_voice_channel_text'):
                active_page_controls['current_voice_channel_text'].value = f"Voice: {vc_name}"
                active_page_controls['current_voice_channel_text'].update()
        elif previewing_voice_channel_id and voice_channels_data.get(previewing_voice_channel_id):
             # If only previewing a voice channel, and text channel is selected, top bar should reflect that we are not "in" voice
            if active_page_controls.get('current_voice_channel_text'):
                # vc_name = voice_channels_data[previewing_voice_channel_id]['name']
                # active_page_controls['current_voice_channel_text'].value = f"Preview: {vc_name}" # Or "Not in voice"
                active_page_controls['current_voice_channel_text'].value = "Not in voice" # Simpler: if text view active, show "Not in voice" unless *actively* in one
                active_page_controls['current_voice_channel_text'].update()
        else:
            if active_page_controls.get('current_voice_channel_text'):
                active_page_controls['current_voice_channel_text'].value = "Not in voice"
                active_page_controls['current_voice_channel_text'].update()


        if hasattr(page_ref, 'update'): page_ref.update()


    async def select_voice_channel(page_ref: ft.Page, channel_id: int, channel_name: str):
        global previewing_voice_channel_id, is_actively_in_voice_channel, current_voice_channel_active_users, current_text_channel_id, current_voice_channel_id

        print(f"--- select_voice_channel START for {channel_name} (ID: {channel_id}) ---")
        current_controls_state_debug = f"Globals before processing: is_active={is_actively_in_voice_channel}, current_vc_id={current_voice_channel_id}, preview_vc_id={previewing_voice_channel_id}"
        print(current_controls_state_debug)

        voice_panel = active_page_controls.get('voice_panel_content_group')

        # Case 1: Re-selecting the channel we are ALREADY ACTIVE in.
        if is_actively_in_voice_channel and current_voice_channel_id == channel_id:
            print(f"Re-selecting active voice channel: {channel_name} (ID: {channel_id})")
            previewing_voice_channel_id = channel_id # Crucial: align preview ID with the active channel being viewed

            if not (voice_panel and voice_panel.visible): # If voice panel not visible, show it
                switch_middle_panel_view("voice", channel_name)
            # Always refresh the voice UI details (users, topic, buttons) for the active channel
            update_voice_channel_user_list_ui() 
            if hasattr(page_ref, 'update'): page_ref.update()
            print(f"--- select_voice_channel END (already active) for {channel_name} ---")
            return

        # Case 2: Re-selecting the channel we are ALREADY PREVIEWING (but not active in).
        if not is_actively_in_voice_channel and previewing_voice_channel_id == channel_id:
            print(f"Re-selecting previewing voice channel: {channel_name} (ID: {channel_id})")
            # previewing_voice_channel_id is already channel_id, is_actively_in_voice_channel is false.

            if not (voice_panel and voice_panel.visible): # If voice panel not visible, show it
                switch_middle_panel_view("voice", channel_name)
            # Always refresh the voice UI details for the previewed channel
            update_voice_channel_user_list_ui()
            if hasattr(page_ref, 'update'): page_ref.update()
            print(f"--- select_voice_channel END (already previewing) for {channel_name} ---")
            return
            
        # Case 3: Selecting a NEW voice channel (different from current active or previewed one).
        print(f"Selecting NEW voice channel to preview: {channel_name} (ID: {channel_id})")
        
        # Leave any current voice channel (active or previewed if different).
        # `called_from_select_new_voice=True` ensures we don't fully clear state if just switching previews
        # and don't switch to text view if leaving active to preview another.
        if is_actively_in_voice_channel and current_voice_channel_id is not None and current_voice_channel_id != channel_id:
            print(f"Leaving active voice channel {current_voice_channel_id} before selecting new voice channel {channel_name}.")
            await _leave_current_voice_channel_if_any(page_ref, called_from_select_new_voice=True)
            is_actively_in_voice_channel = False 
            current_voice_channel_id = None
            print(f"[DEBUG select_voice_channel] After leaving active: is_active={is_actively_in_voice_channel}, current_vc_id={current_voice_channel_id}")
        elif not is_actively_in_voice_channel and previewing_voice_channel_id is not None and previewing_voice_channel_id != channel_id:
            print(f"Leaving previous previewed voice channel {previewing_voice_channel_id} before selecting new voice channel {channel_name}.")
            # _leave_current_voice_channel_if_any will handle UI reset for the old preview (user list etc.)
            # It only emits 'leave_voice_channel' to server if was_actively_in_voice was true, which is fine.
            await _leave_current_voice_channel_if_any(page_ref, called_from_select_new_voice=True)
            print(f"[DEBUG select_voice_channel] After leaving preview: is_active={is_actively_in_voice_channel}, old_preview_vc_id={previewing_voice_channel_id}")


        # Setup for previewing the NEWLY selected channel_id:
        previewing_voice_channel_id = channel_id
        is_actively_in_voice_channel = False # Selecting a new channel always starts as a preview
        current_voice_channel_id = None      # Not active in this new channel yet
        current_text_channel_id = None       # Selecting a voice channel implies focus is on voice, clear text channel context
        current_voice_channel_active_users.clear() # Clear users for the new preview, server will send new list if applicable

        print(f"[DEBUG select_voice_channel_3_setup_new_preview] is_active={is_actively_in_voice_channel}, preview_id={previewing_voice_channel_id}, current_vc_id={current_voice_channel_id}")

        if active_page_controls.get('current_voice_channel_text'):
            active_page_controls['current_voice_channel_text'].value = f"Preview: {channel_name}"
            active_page_controls['current_voice_channel_text'].update()

        switch_middle_panel_view("voice", channel_name) # Show the voice panel for the new channel
        
        # SIO emit for join_voice_channel during preview was removed. User list populates on confirm join.
        
        update_voice_channel_user_list_ui() # Update UI (buttons will show "Join", topic prefix "Preview:")
        
        if hasattr(page_ref, 'update'): 
            page_ref.update() 
        
        print(f"--- select_voice_channel END (new preview setup) for {channel_name} ---")


    async def handle_confirm_join_voice_button_click(page_ref: ft.Page):
        global is_actively_in_voice_channel, current_voice_channel_id, previewing_voice_channel_id, selected_input_device_id, selected_output_device_id
        
        if previewing_voice_channel_id is None:
            print("Error: Confirm join clicked but no channel is being previewed.")
            return

        print(f"Confirming join to voice channel ID: {previewing_voice_channel_id}")
        is_actively_in_voice_channel = True
        current_voice_channel_id = previewing_voice_channel_id # This is now the active channel
        # previewing_voice_channel_id remains the same, as we are active in the channel we were previewing
        
        vc_name = "Unknown"
        if current_voice_channel_id and voice_channels_data.get(current_voice_channel_id):
            vc_name = voice_channels_data[current_voice_channel_id]['name']
        
        if active_page_controls.get('current_voice_channel_text'):
            active_page_controls['current_voice_channel_text'].value = f"Voice: {vc_name}"
            active_page_controls['current_voice_channel_text'].update()
        
        # Emit join_voice_channel event now that user confirms joining
        if sio_client and sio_client.connected and current_voice_channel_id is not None:
            try:
                print(f"Client emitting join_voice_channel for channel_id: {current_voice_channel_id} (on confirm join)")
                await sio_client.emit('join_voice_channel', {'channel_id': current_voice_channel_id})
            except Exception as e:
                print(f"Error emitting join_voice_channel on confirm: {e}")
        
        # Start audio streaming (input)
        if selected_input_device_id is not None:
            await _start_audio_stream(page_ref, selected_input_device_id)
        else:
            print("No input device selected. Cannot start audio stream.")
            sb = ft.SnackBar(ft.Text("Please select an input device in settings to send voice.", color=COLOR_TEXT_ON_WHITE), bgcolor=ft.Colors.ORANGE_ACCENT_700, open=True)
            if hasattr(page_ref, 'overlay'): page_ref.overlay.append(sb); page_ref.update()

        # Start audio playback (output)
        # _start_audio_playback_stream will use selected_output_device_id or fallback to default
        await _start_audio_playback_stream(page_ref, selected_output_device_id)

        update_voice_channel_user_list_ui() # Update UI elements (buttons, topic prefix)
        if hasattr(page_ref, 'update'): page_ref.update()
        print(f"Successfully joined voice channel: {vc_name} (ID: {current_voice_channel_id})")


    async def handle_leave_voice_click(page_ref: ft.Page):
        global is_actively_in_voice_channel, current_voice_channel_id, previewing_voice_channel_id
        
        if not is_actively_in_voice_channel or current_voice_channel_id is None:
            print("Error: Leave voice clicked but not actively in a voice channel.")
            return

        channel_id_being_left = current_voice_channel_id
        channel_name_being_left = "Unknown Voice Channel"
        if channel_id_being_left and voice_channels_data.get(channel_id_being_left):
            channel_name_being_left = voice_channels_data[channel_id_being_left]['name']

        print(f"Leaving voice channel: {channel_name_being_left} (ID: {channel_id_being_left})")

        if sio_client and sio_client.connected:
            try:
                await sio_client.emit('leave_voice_channel', {'channel_id': channel_id_being_left})
            except Exception as e:
                print(f"Error emitting leave_voice_channel: {e}")

        is_actively_in_voice_channel = False
        # current_voice_channel_id is now None (no longer *active* in it)
        # previewing_voice_channel_id remains channel_id_being_left (we return to previewing it)
        previewing_voice_channel_id = channel_id_being_left 
        current_voice_channel_id = None # Explicitly set to None as we are no longer *active*

        if active_page_controls.get('current_voice_channel_text'):
            active_page_controls['current_voice_channel_text'].value = f"Preview: {channel_name_being_left}"
            active_page_controls['current_voice_channel_text'].update()
        
        # User list (current_voice_channel_active_users) should ideally remain as it was for the preview.
        # Server's voice_channel_users (if re-requested or resent on join) would repopulate.
        # For now, update_voice_channel_user_list_ui will use existing data but change button visibility.
        update_voice_channel_user_list_ui() 
        
        if hasattr(page_ref, 'update'): page_ref.update()
        print(f"Returned to preview mode for voice channel: {channel_name_being_left}")

    async def fetch_and_display_channels(p: ft.Page):
        global text_channels_data, voice_channels_data
        if not shared_aiohttp_session or shared_aiohttp_session.closed: return
        async with shared_aiohttp_session.get(f"{API_BASE_URL}/channels") as response:
            if response.status == 200:
                data = await response.json()
                text_channels, voice_channels = data.get("text_channels", []), data.get("voice_channels", [])
                text_channels_data = {tc['id']: tc for tc in text_channels}
                voice_channels_data = {vc['id']: vc for vc in voice_channels}
                
                channel_list_controls = [ft.Text("Text Channels", weight=ft.FontWeight.BOLD, color=COLOR_TEXT_DARK_PURPLE)]
                for tc in text_channels: 
                    channel_list_controls.append(ft.TextButton(
                        content=ft.Row([ft.Icon(ft.Icons.CHAT_BUBBLE_OUTLINE, size=16, color=COLOR_TEXT_DARK_PURPLE), ft.Text(tc['name'])]),
                        on_click=lambda _, t_id=tc['id'], t_name=tc['name']: p.run_task(select_text_channel, p, t_id, t_name), 
                        style=ft.ButtonStyle(color=COLOR_TEXT_DARK_PURPLE)
                    ))
                
                channel_list_controls.append(ft.Container(
                    content=ft.Text("Voice Channels", weight=ft.FontWeight.BOLD, color=COLOR_TEXT_DARK_PURPLE), 
                    margin=ft.margin.only(top=10))
                )
                for vc in voice_channels: 
                    channel_list_controls.append(ft.TextButton(
                        content=ft.Row([ft.Icon(ft.Icons.VOICE_CHAT_OUTLINED, size=16, color=COLOR_TEXT_DARK_PURPLE), ft.Text(vc['name'])]),
                        on_click=lambda _, v_id=vc['id'], v_name=vc['name']: p.run_task(select_voice_channel, p, v_id, v_name), 
                        style=ft.ButtonStyle(color=COLOR_TEXT_DARK_PURPLE)
                    ))
                if active_page_controls.get('channel_list_view'): active_page_controls['channel_list_view'].controls = channel_list_controls
            else: 
                print(f"Failed to fetch channels: {response.status}")
                if active_page_controls.get('channel_list_view'): 
                    active_page_controls['channel_list_view'].controls = [ft.Text("Error loading channels.", color=COLOR_TEXT_DARK_PURPLE)]

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

    username_field = ft.TextField(label="Username", width=300, autofocus=True, value=app_config.get("username", ""),
                                  border_color=COLOR_BORDER, focused_border_color=COLOR_PRIMARY_PURPLE,
                                  label_style=ft.TextStyle(color=COLOR_TEXT_ON_WHITE),
                                  text_style=ft.TextStyle(color=COLOR_TEXT_ON_WHITE))
    password_field = ft.TextField(label="Password", password=True, can_reveal_password=True, width=300, value=app_config.get("password", ""),
                                  border_color=COLOR_BORDER, focused_border_color=COLOR_PRIMARY_PURPLE,
                                  label_style=ft.TextStyle(color=COLOR_TEXT_ON_WHITE),
                                  text_style=ft.TextStyle(color=COLOR_TEXT_ON_WHITE))
    login_button = ft.ElevatedButton(text="Login", on_click=lambda e: page.run_task(attempt_login, e, False), width=150,
                                     bgcolor=COLOR_PRIMARY_PURPLE, color=COLOR_BUTTON_TEXT)
    register_button = ft.ElevatedButton(text="Register", on_click=show_register_view, width=150,
                                        bgcolor=COLOR_PRIMARY_PURPLE, color=COLOR_BUTTON_TEXT)
    active_page_controls['login'] = ft.Column([
        ft.Text("Client Login", size=24, weight=ft.FontWeight.BOLD, color=COLOR_TEXT_ON_WHITE), 
            username_field,
            password_field,
        ft.Row([remember_me_checkbox], alignment=ft.MainAxisAlignment.CENTER), 
            ft.Row([login_button, register_button], alignment=ft.MainAxisAlignment.CENTER),
        active_page_controls['status_text']
    ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=20, expand=True) # Added expand for better centering on page

    active_page_controls['channel_list_view'] = ft.ListView(expand=False, spacing=2, width=220, padding=10)
    active_page_controls['current_chat_topic'] = ft.Text("Select a text channel", weight=ft.FontWeight.BOLD, size=16, color=COLOR_TEXT_ON_WHITE)
    active_page_controls['chat_messages_view'] = ft.ListView(expand=True, spacing=5, auto_scroll=True, padding=10) # Text color within messages will be default black on white
    active_page_controls['message_input_field'] = ft.TextField(
        hint_text="Type...", expand=True, filled=True, border_radius=20, 
        on_submit=lambda e: page.run_task(handle_send_message_click, page),
        bgcolor=COLOR_INPUT_FIELD_BG_FILLED,
        border_color=COLOR_BORDER,
        focused_border_color=COLOR_PRIMARY_PURPLE,
        text_style=ft.TextStyle(color=COLOR_TEXT_ON_WHITE)
    )
    active_page_controls['send_message_button'] = ft.IconButton(
        icon=ft.Icons.SEND_ROUNDED, 
        on_click=lambda e: page.run_task(handle_send_message_click, page),
        icon_color=COLOR_PRIMARY_PURPLE # Icon color for send button
    )
    active_page_controls['chat_panel_content_group'] = ft.Column([
        active_page_controls['current_chat_topic'], 
        ft.Divider(height=1, color=COLOR_DIVIDER_ON_WHITE), 
        active_page_controls['chat_messages_view'], 
        ft.Row([active_page_controls['message_input_field'], active_page_controls['send_message_button']])
    ], expand=True, visible=True)

    active_page_controls['voice_channel_topic_display'] = ft.Text("Voice Channel", weight=ft.FontWeight.BOLD, size=16, color=COLOR_TEXT_ON_WHITE)
    active_page_controls['voice_channel_internal_users_list'] = ft.ListView(expand=True, spacing=5, padding=10) # Text color handled in update_voice_channel_user_list_ui
    
    # --- Voice Settings Area Definition ---
    active_page_controls['voice_settings_input_device_dropdown'] = ft.Dropdown(
        options=[ft.dropdown.Option(key="-1", text="Loading...")],
        label="Input Device",
        width=250,
        text_style=ft.TextStyle(color=COLOR_TEXT_ON_WHITE, size=12),
        label_style=ft.TextStyle(color=COLOR_TEXT_ON_WHITE, size=12),
        border_color=COLOR_BORDER,
        focused_border_color=COLOR_PRIMARY_PURPLE,
        on_change=handle_input_device_change
    )
    active_page_controls['voice_settings_output_device_dropdown'] = ft.Dropdown(
        options=[ft.dropdown.Option(key="-1", text="Loading...")],
        label="Output Device",
        width=250,
        text_style=ft.TextStyle(color=COLOR_TEXT_ON_WHITE, size=12),
        label_style=ft.TextStyle(color=COLOR_TEXT_ON_WHITE, size=12),
        border_color=COLOR_BORDER,
        focused_border_color=COLOR_PRIMARY_PURPLE,
        on_change=handle_output_device_change
    )
    active_page_controls['voice_settings_input_volume_slider'] = ft.Slider(
        min=0, max=100, divisions=100, value=100, 
        label="{value}%",
        active_color=COLOR_PRIMARY_PURPLE,
        inactive_color=ft.Colors.with_opacity(0.3, COLOR_PRIMARY_PURPLE)
    )
    active_page_controls['voice_settings_mute_button'] = ft.IconButton(
        icon=ft.Icons.MIC,
        tooltip="Mute Microphone",
        on_click=handle_mute_mic_click,
        icon_color=COLOR_TEXT_ON_WHITE,
        icon_size=18
    )
    active_page_controls['voice_settings_mic_test_bar'] = ft.ProgressBar(
        width=180,
        value=0, 
        color=COLOR_PRIMARY_PURPLE, 
        bgcolor=ft.Colors.with_opacity(0.2, COLOR_PRIMARY_PURPLE)
    )
    active_page_controls['voice_settings_mic_test_button'] = ft.ElevatedButton(
        text="Start Mic Test",
        icon=ft.Icons.PLAY_ARROW,
        on_click=handle_mic_test_button_click,
        style=ft.ButtonStyle(
            bgcolor=COLOR_PRIMARY_PURPLE, 
            color=COLOR_BUTTON_TEXT,
            shape=ft.RoundedRectangleBorder(radius=5)
        ),
        height=36
    )
    active_page_controls['voice_settings_save_button'] = ft.ElevatedButton(
        text="Save Audio Settings",
        icon=ft.Icons.SAVE_OUTLINED,
        on_click=handle_save_audio_settings_click, # No page.run_task needed for async handlers directly assigned
        style=ft.ButtonStyle(bgcolor=COLOR_PRIMARY_PURPLE, color=COLOR_BUTTON_TEXT, shape=ft.RoundedRectangleBorder(radius=5)),
        height=36,
        tooltip="Save selected Input/Output devices"
    )

    active_page_controls['voice_settings_area'] = ft.Column(
        [
            ft.Text("Voice Settings", weight=ft.FontWeight.BOLD, size=14, color=COLOR_TEXT_ON_WHITE),
            ft.Divider(height=5, color=COLOR_DIVIDER_ON_WHITE),
            active_page_controls['voice_settings_input_device_dropdown'],
            active_page_controls['voice_settings_output_device_dropdown'],
            active_page_controls['voice_settings_input_volume_slider'], # Slider directly added
            ft.Row( # Mic test row, centered
                [
                    active_page_controls['voice_settings_mic_test_button'],
                    active_page_controls['voice_settings_mic_test_bar']
                ],
                alignment=ft.MainAxisAlignment.CENTER, # Centered this row's content
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=10
            ),
            active_page_controls['voice_settings_mute_button'], # Mute button directly added
            ft.Row( # Save button centered in a new Row
                [active_page_controls['voice_settings_save_button']],
                alignment=ft.MainAxisAlignment.CENTER
            )
        ],
        visible=False,
        spacing=8, # Reduced spacing
        width=280,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER # Added horizontal alignment for the column
    )
    active_page_controls['confirm_join_voice_button'] = ft.ElevatedButton(
        text="加入语音", icon=ft.Icons.CALL, 
        on_click=lambda e: page.run_task(handle_confirm_join_voice_button_click, page), 
        visible=False, 
        style=ft.ButtonStyle(bgcolor=COLOR_PRIMARY_PURPLE, color=COLOR_BUTTON_TEXT),
        icon_color=COLOR_ICON_ON_PURPLE
    )
    active_page_controls['leave_voice_button'] = ft.ElevatedButton(
        text="离开语音", icon=ft.Icons.CALL_END, 
        on_click=lambda e: page.run_task(handle_leave_voice_click, page), 
        visible=False, 
        style=ft.ButtonStyle(bgcolor=COLOR_PRIMARY_PURPLE, color=COLOR_BUTTON_TEXT),
        icon_color=COLOR_ICON_ON_PURPLE
    )
    active_page_controls['voice_panel_content_group'] = ft.Column([
        active_page_controls['voice_channel_topic_display'], 
        ft.Divider(height=1, color=COLOR_DIVIDER_ON_WHITE), 
        ft.Container(content=ft.Text("Users in channel:", weight=ft.FontWeight.W_600, color=COLOR_TEXT_ON_WHITE), margin=ft.margin.only(top=10, bottom=5)),
        active_page_controls['voice_channel_internal_users_list'],
        active_page_controls['voice_settings_area'], 
        active_page_controls['confirm_join_voice_button'],
        active_page_controls['leave_voice_button']
    ], expand=True, visible=False)
    
    middle_panel_container = ft.Container(
        ft.Stack([active_page_controls['chat_panel_content_group'], active_page_controls['voice_panel_content_group']]), 
        expand=True, padding=10, bgcolor=COLOR_BACKGROUND_WHITE,
        border=ft.border.all(1, COLOR_BORDER)
    )
    left_panel = ft.Container(
        ft.Column([
            ft.Text("Channels",weight=ft.FontWeight.BOLD,size=18,color=COLOR_TEXT_DARK_PURPLE),
            ft.Divider(height=5,color=COLOR_DIVIDER_ON_WHITE),
            active_page_controls['channel_list_view']
        ], expand=True), 
        width=240,padding=0,bgcolor=COLOR_BACKGROUND_WHITE,
        border=ft.border.all(1, COLOR_BORDER)
    )
    active_page_controls['current_voice_channel_text'] = ft.Text("Not in voice", weight=ft.FontWeight.BOLD, color=COLOR_TEXT_ON_PURPLE, size=12, italic=True)
    active_page_controls['server_users_list_view'] = ft.ListView(expand=True, spacing=3, padding=ft.padding.only(top=5))
    right_panel = ft.Container(
        ft.Column([
            ft.Text("Server Users",weight=ft.FontWeight.BOLD,size=16,color=COLOR_TEXT_ON_WHITE),
            ft.Divider(height=1, color=COLOR_DIVIDER_ON_WHITE),
            active_page_controls['server_users_list_view']
        ], expand=True,horizontal_alignment=ft.CrossAxisAlignment.CENTER), 
        width=200,padding=10,bgcolor=COLOR_BACKGROUND_WHITE,
        border=ft.border.all(1, COLOR_BORDER)
    )
    main_app_layout = ft.Row([left_panel, middle_panel_container, right_panel], expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH)
    
    active_page_controls['top_bar_username_text'] = ft.Text("User: N/A", size=16, weight=ft.FontWeight.BOLD, expand=True, color=COLOR_TEXT_ON_PURPLE)
    active_page_controls['main_status_bar'] = ft.Text(value="", size=12, color=COLOR_STATUS_TEXT_MUTED)
    main_app_view_content = ft.Column([
        ft.Container(
            ft.Row([
                active_page_controls['top_bar_username_text'], 
                active_page_controls['current_voice_channel_text'], 
                ft.IconButton(ft.Icons.LOGOUT, on_click=lambda e: show_login_view(page),tooltip="Logout",icon_color=COLOR_ICON_ON_PURPLE)
            ],vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=COLOR_PRIMARY_PURPLE,
            padding=ft.padding.symmetric(horizontal=15,vertical=10)
        ),
        main_app_layout, 
        active_page_controls['main_status_bar']
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
        global page # Ensure page global is set for other functions that might need it like save
        page = p # Assign the current page to the global `page` variable
        active_page_controls['login'].visible = False
        active_page_controls['main_app'].visible = True
        if current_user_info and active_page_controls.get('top_bar_username_text'): active_page_controls['top_bar_username_text'].value = f"User: {current_user_info.get('username', 'N/A')}"
        update_voice_panel_button_visibility()
        if hasattr(p, 'update'): p.update()
        
        if SOUNDDEVICE_AVAILABLE:
            # Pass the page instance if needed by the task, or ensure 'page' global is accessible
            p.run_task(populate_audio_device_dropdowns) 

    async def handle_send_message_click(page_ref: ft.Page):
        msg_content = active_page_controls['message_input_field'].value.strip()
        if msg_content and current_text_channel_id is not None and sio_client and sio_client.connected:
            await sio_client.emit('send_message', {'channel_id': current_text_channel_id, 'message': msg_content})
            active_page_controls['message_input_field'].value = ""; active_page_controls['message_input_field'].update()

    page.add(active_page_controls['login'], main_app_view_content)
    original_on_close = page.on_close if hasattr(page, 'on_close') else None
    async def on_close_extended(e):
        if original_on_close: await original_on_close(e) if inspect.iscoroutinefunction(original_on_close) else original_on_close(e)
        await _leave_current_voice_channel_if_any(page, called_from_select_new_voice=False) # Ensure we leave voice on app close
        await _stop_audio_stream_if_running() # Ensure audio stream is stopped
        await _stop_audio_playback_stream_if_running() # Ensure audio playback is stopped
        if sio_client and sio_client.connected: await sio_client.disconnect()
        if shared_aiohttp_session and not shared_aiohttp_session.closed: await shared_aiohttp_session.close()
    page.on_close = on_close_extended

    if remember_me_checkbox.value and username_field.value and password_field.value:
        if active_page_controls.get('status_text'): active_page_controls['status_text'].value = "Auto-login..."
        if hasattr(page, 'update'): page.update()
        await attempt_login(None, is_auto_login=True)

if __name__ == "__main__":
    ft.app(target=main) 