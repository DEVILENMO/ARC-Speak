<p align="center">
  <img src="assests/icon.png" alt="App Icon" width="150"/>
</p>

# ARC SPEAK

[简体中文](./README_zh.md)

This is a extremely light voice and text chat application built with Python, Flask, Socket.IO, and Flet, supporting multi-user, multi-channel real-time communication.

## Key Features

*   Real-time text chat with support for multiple text channels.
*   Real-time voice chat with support for multiple voice channels.
*   User authentication and session management.
*   Microphone mute/unmute functionality.
*   User speaking status indication (card color change).
*   Persistent text chat history with support for scrolling up to load older messages.
*   Basic voice settings, including input/output device selection and microphone testing.
*   (Server-side) Rudimentary admin functions (e.g., user list, channel management APIs, UI not fully implemented).

## Technology Stack

*   **Backend (Server-side)**:
    *   Python
    *   Flask: Web framework
    *   Flask-SocketIO: Implements WebSocket communication
    *   Flask-Login: User session management
    *   Flask-SQLAlchemy & SQLAlchemy: ORM and database interaction (SQLite)
    *   Werkzeug: WSGI utility library
*   **Frontend (Client-side)**:
    *   Python
    *   Flet: Python GUI framework based on Flutter
    *   python-socketio: Client-side WebSocket communication
    *   aiohttp: Asynchronous HTTP requests
    *   sounddevice: Audio input/output processing
    *   numpy: Audio data processing
*   **Environment Management**:
    *   Conda (Recommended for managing an environment named "Flask")
    *   pip (Used for installing packages within the Conda environment)

## Prerequisites

Before running this project, ensure your system has the following software installed:

1.  **Python**: Version 3.9 or higher (refer to Conda environment creation or `requirements.txt` for specific version compatibility used during development).
2.  **Conda**: Anaconda or Miniconda for environment management.
3.  **(Optional) Git**: For cloning the project repository (if the project is hosted in a version control system).

## Installation and Setup

1.  **Get Project Files**:
    *   If the project is in a Git repository, clone the repository:
        ```bash
        git clone <repository_url>
        cd <project_directory>
        ```
    *   Alternatively, download the project folder directly.

2.  **Create and Activate Conda Environment**:
    This project recommends using a Conda environment named `Flask`.
    *   Open your terminal or Anaconda Prompt.
    *   Create a new Conda environment (if it doesn't already exist). Replace `python=3.x` with your desired Python version (e.g., `python=3.10`):
        ```bash
        conda create -n Flask python=3.10
        ```
    *   Activate the Conda environment:
        ```bash
        conda activate Flask
        ```

3.  **Install Dependencies**:
    In the activated `Flask` Conda environment, install all required Python packages using the provided `requirements.txt` file:
    ```bash
    pip install -r requirements.txt
    ```
    *Note: If you encounter network or SSL errors during this step (especially regarding `Flask-SSLify` or other package build dependencies), check your internet connection, firewall settings, or try updating `pip` and `setuptools` (`pip install --upgrade pip setuptools`).*

4.  **SSL Certificate Configuration (for Server-side)**:
    The server (`app.py`) is configured to use SSL (HTTPS). It expects to find `cert.pem` (certificate file) and `key.pem` (private key file) in the project root directory.
    *   You need to generate these SSL certificate files yourself. For local development and testing, you can use tools like OpenSSL to generate self-signed certificates.
    *   For example, to generate a self-signed certificate and private key using OpenSSL:
        ```bash
        openssl req -x509 -newkey rsa:4096 -nodes -out cert.pem -keyout key.pem -days 365
        ```
        Fill in the required information when prompted.
    *   Ensure the generated `cert.pem` and `key.pem` files are located in the project root directory (same level as `app.py`).

## Running the Project

The project provides batch scripts (`.bat` files) to conveniently start the server and client. These scripts automatically attempt to activate the Conda environment named `Flask`.

1.  **Start the Server**:
    Double-click the `run_server.bat` file in the project root directory.
    This will activate the Conda environment and execute `python app.py`.
    You should see server startup logs in the terminal, including the address and port it's listening on (default is `https://0.0.0.0:5005`).

2.  **Start the Client**:
    Double-click the `run_client.bat` file in the project root directory.
    This will activate the Conda environment and execute `python flet_client.py`.
    The client GUI will start and attempt to connect to the server.

## Project Structure (Overview)

```
voicechat/
├── icon.png                 # Application Icon
├── .venv/                   # (Optional) Standard Python virtual environment (if not using Conda)
├── cert.pem                 # SSL certificate file (self-generated)
├── key.pem                  # SSL private key file (self-generated)
├── app.py                   # Server-side Flask and SocketIO application logic
├── flet_client.py           # Client-side Flet and SocketIO application logic
├── models.py                # SQLAlchemy database model definitions
├── requirements.txt         # Python package dependency list
├── config.json              # (Client-side) Stores user configurations like remember me, device IDs
├── voicechat.db             # (Server-side) SQLite database file
├── run_server.bat           # Batch script to start the server
├── run_client.bat           # Batch script to start the client
├── README.md                # This project description file (English)
├── README_zh.md             # Project description file (Chinese)
└── ... (other possible static files or templates if the project expands)
```

## Notes

*   When running the server (`app.py`) for the first time, it will automatically create the `voicechat.db` SQLite database file and some default channels (if the database doesn't already exist).
*   The client (`flet_client.py`) might take a moment to load the audio device list upon first launch or after changing audio devices.
*   If you encounter `sounddevice`-related errors, ensure your system has the PortAudio library correctly installed (usually `sounddevice` attempts to bundle it, but some systems might require manual installation or configuration).

## (Optional) Potential Future Improvements

*   More aesthetically pleasing and user-friendly UI design.
*   Full-fledged administrator control panel.
*   Private messaging functionality.
*   Avatar uploading and management.
*   More detailed user permission management.
*   Automatic loading of older chat messages when scrolling to the top.
*   WebRTC for lower-latency voice transmission (currently uses server-relayed audio streams).

---

Hope this `README.md` is helpful! 