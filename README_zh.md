 <p align="center">
  <img src="assests/icon.png" alt="App Icon" width="150"/>
</p>
 
 # ARC SPEAK 弧光语音

这是一个轻量级的基于 Python、Flask、Socket.IO 和 Flet 构建的语音和文字聊天应用程序，支持多用户、多频道实时通讯。

## 主要功能

*   实时文字聊天，支持多文本频道。
*   实时语音聊天，支持多语音频道。
*   用户认证与会话管理。
*   麦克风静音/取消静音功能。
*   用户发言状态指示（卡片颜色变化）。
*   文字聊天记录持久化，支持向上滚动加载更早的聊天记录。
*   基本的语音设置，包括输入/输出设备选择、麦克风测试。
*   (服务端) 管理员功能雏形 (如用户列表、频道管理接口等，具体UI未完全实现)。

## 技术栈

*   **后端 (服务端)**:
    *   Python
    *   Flask: Web 框架
    *   Flask-SocketIO: 实现 WebSocket 通讯
    *   Flask-Login: 用户会话管理
    *   Flask-SQLAlchemy & SQLAlchemy: ORM 和数据库交互 (SQLite)
    *   Werkzeug: WSGI 工具库
*   **前端 (客户端)**:
    *   Python
    *   Flet: 基于 Flutter 的 Python GUI 框架
    *   python-socketio: 客户端 WebSocket 通讯
    *   aiohttp: 异步 HTTP 请求
    *   sounddevice: 音频输入输出处理
    *   numpy: 音频数据处理
*   **环境管理**:
    *   Conda (推荐用于管理名为 "Flask" 的环境)
    *   pip (用于在 Conda 环境中安装包)

## 先决条件

在运行此项目之前，请确保您的系统已安装以下软件：

1.  **Python**: 版本 3.9 或更高 (项目开发时使用的具体版本请参照 Conda 环境创建或 `requirements.txt` 中的兼容性)。
2.  **Conda**: Anaconda 或 Miniconda 用于环境管理。
3.  **(可选) Git**: 用于克隆项目仓库（如果项目托管在版本控制系统中）。

## 安装与设置

1.  **获取项目文件**:
    *   如果项目在 Git 仓库中, 克隆仓库:
        ```bash
        git clone <repository_url>
        cd <project_directory>
        ```
    *   或者，直接下载项目文件夹。

2.  **创建并激活 Conda 环境**:
    本项目推荐使用名为 `Flask` 的 Conda 环境。
    *   打开您的终端或 Anaconda Prompt。
    *   创建一个新的 Conda 环境 (如果尚不存在)。将 `python=3.x`替换为您希望使用的 Python 版本 (例如 `python=3.10`)：
        ```bash
        conda create -n Flask python=3.10
        ```
    *   激活 Conda 环境:
        ```bash
        conda activate Flask
        ```

3.  **安装依赖项**:
    在已激活的 `Flask` Conda 环境中，使用提供的 `requirements.txt` 文件安装所有必需的 Python 包：
    ```bash
    pip install -r requirements.txt
    ```
    *注意：如果在执行此步骤时遇到网络或 SSL 错误 (特别是关于 `Flask-SSLify` 或其他包的构建依赖)，请检查您的网络连接、防火墙设置，或尝试更新 `pip` 和 `setuptools` (`pip install --upgrade pip setuptools`)。*

4.  **SSL 证书配置 (用于服务端)**:
    服务端 (`app.py`) 配置为使用 SSL (HTTPS)。它期望在项目根目录下找到 `cert.pem` (证书文件) 和 `key.pem` (私钥文件)。
    *   您需要自行生成这些 SSL 证书文件。对于本地开发和测试，可以使用 OpenSSL 等工具生成自签名证书。
    *   例如，使用 OpenSSL 生成自签名证书和私钥：
        ```bash
        openssl req -x509 -newkey rsa:4096 -nodes -out cert.pem -keyout key.pem -days 365
        ```
        在提示时填写所需信息即可。
    *   确保生成的 `cert.pem` 和 `key.pem` 文件位于项目根目录 (与 `app.py`同级)。

## 运行项目

项目提供了批处理脚本 (`.bat` 文件) 以方便启动服务端和客户端。这些脚本会自动尝试激活名为 `Flask` 的 Conda 环境。

1.  **启动服务端**:
    双击运行项目根目录下的 `run_server.bat` 文件。
    这会激活 Conda 环境并执行 `python app.py`。
    您应该会在终端看到服务器启动的日志，包括它正在监听的地址和端口 (默认为 `https://0.0.0.0:5005`)。

2.  **启动客户端**:
    双击运行项目根目录下的 `run_client.bat` 文件。
    这会激活 Conda 环境并执行 `python flet_client.py`。
    客户端图形界面将会启动，并尝试连接到服务器。

## 项目结构 (概览)

```
voicechat/
├── .venv/                   # (可选) Python 标准虚拟环境 (如果未使用Conda)
├── cert.pem                 # SSL 证书文件 (需自行生成)
├── key.pem                  # SSL 私钥文件 (需自行生成)
├── app.py                   # 服务端 Flask 和 SocketIO 应用逻辑
├── flet_client.py           # 客户端 Flet 和 SocketIO 应用逻辑
├── models.py                # SQLAlchemy 数据库模型定义
├── requirements.txt         # Python 包依赖列表
├── config.json              # (客户端) 存储用户配置，如记住我、设备ID
├── voicechat.db             # (服务端) SQLite 数据库文件
├── run_server.bat           # 启动服务端的批处理脚本
├── run_client.bat           # 启动客户端的批处理脚本
├── README.md                # 本项目说明文件
└── ... (其他可能的静态文件或模板，如果项目扩展)
```

## 注意事项

*   首次运行服务端 (`app.py`) 时，会自动创建 `voicechat.db` SQLite 数据库文件和一些默认频道 (如果数据库尚不存在)。
*   客户端 (`flet_client.py`) 在首次启动或更改音频设备后，音频设备列表可能需要一点时间来加载。
*   如果遇到 `sounddevice` 相关的错误，请确保您的系统已正确安装了 PortAudio 库 (通常 `sounddevice` 会尝试捆绑它，但某些系统可能需要手动安装或配置)。

## (可选) 未来可能的改进

*   更美观和用户友好的UI设计。
*   完整的管理员控制面板。
*   私聊功能。
*   头像上传和管理。
*   更详细的用户权限管理。
*   滚动到顶部自动加载更早的聊天记录。
*   WebRTC 用于更低延迟的语音传输 (目前使用的是服务器中继的音频流)。
