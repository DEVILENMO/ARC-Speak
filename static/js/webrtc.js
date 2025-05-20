// 全局变量
let localStream = null;
let peerConnections = {};
const configuration = { 
    iceServers: [
        { urls: 'stun:stun.l.google.com:19302' },
        { urls: 'stun:stun1.l.google.com:19302' }
    ] 
};
let isMuted = false;
let webrtcSocket = null; // 用于存储Socket.IO实例
let webrtcCurrentUserId = null; // 用于存储当前用户ID
let currentChannelIdForWebRTC = null;
const DEBUG_LOOPBACK = false; // <--- 添加这个开关，设为 true 来启用本地回环

// 初始化WebRTC模块，传入socket实例和当前用户ID
function initializeWebRTC(socketInstance, currentUserId) {
    webrtcSocket = socketInstance;
    webrtcCurrentUserId = currentUserId;

    // 将所有依赖 socket 的事件监听器移到这里
    webrtcSocket.on('voice_signal', handleVoiceSignal);
    webrtcSocket.on('user_speaking', handleUserSpeaking);
    webrtcSocket.on('user_mute_status', handleUserMuteStatus);
    console.log("WebRTC signaling initialized with socket and user ID.");
}

// 初始化媒体流
async function initializeMedia() {
    try {
        const preferredMicrophone = localStorage.getItem('selectedAudioInput'); // <--- 确保这里用的是 selectedAudioInput
        const constraints = {
            audio: preferredMicrophone && preferredMicrophone !== 'default' ? 
                { deviceId: { exact: preferredMicrophone } } : 
                true
        };
        
        localStream = await navigator.mediaDevices.getUserMedia(constraints);
        console.log('麦克风已初始化', localStream);

        if (DEBUG_LOOPBACK && localStream) {
            const loopbackAudio = document.createElement('audio');
            loopbackAudio.autoplay = true;
            loopbackAudio.muted = true; // 开始时静音，防止啸叫，用户可以通过浏览器控制播放
            loopbackAudio.srcObject = localStream;
            // 可以考虑将这个元素添加到DOM中并隐藏，或者不添加，直接播放
            // document.getElementById('remote-audio-container').appendChild(loopbackAudio); 
            // console.log("本地回环已启用。确保解除静音以听到自己声音。");
            // 实际上，为了直接听到，我们可以不设置 muted = true，但要小心可能的啸叫
            // 如果直接播放，则需要确保输出设备和输入设备不同，或者用户戴耳机
            // 更安全的方式是创建一个新的AudioContext来处理回环并控制音量

            // 安全的回环方式，避免直接播放可能导致的啸叫
            const audioContext = new (window.AudioContext || window.webkitAudioContext)();
            const source = audioContext.createMediaStreamSource(localStream);
            const gainNode = audioContext.createGain();
            gainNode.gain.value = 0.7; // 设置一个适中的回环音量，避免过大
            source.connect(gainNode);
            gainNode.connect(audioContext.destination); // 连接到默认输出
            console.log("本地调试回环已启用，音量 0.7");
        }

        // 在成功获取localStream后，设置音量检测
        // (由 main.html 中的 joinVoiceChannel 调用此函数后，再调用 setupVolumeDetection)

    } catch (error) {
        console.error('无法访问麦克风:', error);
        alert('无法访问麦克风。请检查浏览器权限设置。');
    }
}

// 创建音量控制节点
function createGainNode(stream, initialVolume) {
    const audioContext = new (window.AudioContext || window.webkitAudioContext)();
    const sourceNode = audioContext.createMediaStreamSource(stream);
    const gainNode = audioContext.createGain();
    gainNode.gain.value = initialVolume;
    sourceNode.connect(gainNode);
    
    const destinationNode = audioContext.createMediaStreamDestination();
    gainNode.connect(destinationNode);
    
    const newStream = destinationNode.stream;
    return { stream: newStream, gainNode };
}

// 静音控制
function toggleMute() {
    if (!localStream || !webrtcSocket) return;
    
    isMuted = !isMuted;
    localStream.getAudioTracks().forEach(track => {
        track.enabled = !isMuted;
    });
    
    const muteBtn = document.getElementById('mute-btn');
    if (muteBtn) {
        muteBtn.textContent = isMuted ? '取消静音' : '静音';
        muteBtn.style.backgroundColor = isMuted ? '#7289da' : '#f04747';
    }
    
    // 使用 webrtcSocket 发送
    webrtcSocket.emit('update_mute_status', { is_muted: isMuted });
}

// 创建对等连接
function createPeerConnection(userId) {
    if (peerConnections[userId]) return peerConnections[userId];
    
    // 兼容性检查
    const RTCPeerConnection = window.RTCPeerConnection || window.mozRTCPeerConnection || window.webkitRTCPeerConnection;
    if (!RTCPeerConnection) {
        console.error("WebRTC PeerConnection is not supported by this browser.");
        alert("WebRTC PeerConnection is not supported by this browser.");
        return null;
    }

    const pc = new RTCPeerConnection(configuration);
    peerConnections[userId] = pc;
    
    if (localStream) {
        localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
    }
    
    pc.onicecandidate = event => {
        if (event.candidate && webrtcSocket) {
            webrtcSocket.emit('voice_signal', {
                type: 'ice_candidate',
                candidate: event.candidate,
                recipient_id: userId
            });
        }
    };
    
    pc.onconnectionstatechange = event => {
        console.log(`连接状态 (${userId}):`, pc.connectionState);
        if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected' || pc.connectionState === 'closed') {
            // 可以考虑在这里清理 peerConnections[userId]
        }
    };
    
    pc.ontrack = event => {
        console.log('收到远程流 for user:', userId, event);
        const remoteAudioContainer = document.getElementById('remote-audio-container');
        if (!remoteAudioContainer) {
            console.error("Remote audio container not found!");
            return;
        }

        let remoteAudio = document.getElementById(`audio-${userId}`);
        if (!remoteAudio) {
            remoteAudio = document.createElement('audio');
            remoteAudio.id = `audio-${userId}`;
            remoteAudio.autoplay = true;
            
            const preferredSpeaker = localStorage.getItem('selectedAudioOutput'); // 从 settings.html 读取选择
            if (preferredSpeaker && preferredSpeaker !== 'default' && typeof remoteAudio.setSinkId === 'function') {
                remoteAudio.setSinkId(preferredSpeaker)
                    .then(() => console.log(`Audio output set to ${preferredSpeaker} for user ${userId}`))
                    .catch(error => console.error('扬声器设置失败:', error));
            }
            remoteAudioContainer.appendChild(remoteAudio);
        }
        
        if (remoteAudio.srcObject !== event.streams[0]) {
            remoteAudio.srcObject = event.streams[0];
            remoteAudio.play().catch(e => console.error("Error playing remote audio:", e));
        }
    };
    
    return pc;
}

// 协商连接
async function negotiateConnection(userId) {
    if (!webrtcSocket) return;
    try {
        const pc = createPeerConnection(userId);
        if (!pc) return;
        
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        
        webrtcSocket.emit('voice_signal', {
            type: 'offer',
            sdp: pc.localDescription,
            recipient_id: userId
        });
    } catch (error) {
        console.error(`连接协商失败 (${userId}):`, error);
    }
}

// 处理信令 (作为回调函数)
async function handleVoiceSignal(data) {
    if (!webrtcSocket || !data.sender_id) return;
    try {
        const pc = createPeerConnection(data.sender_id); // 确保对端连接已创建或获取
        if (!pc) return;

        if (data.type === 'offer') {
            await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
            const answer = await pc.createAnswer();
            await pc.setLocalDescription(answer);
            webrtcSocket.emit('voice_signal', {
                type: 'answer',
                sdp: pc.localDescription,
                recipient_id: data.sender_id
            });
        } else if (data.type === 'answer') {
            await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
        } else if (data.type === 'ice_candidate' && data.candidate) {
            try {
                await pc.addIceCandidate(new RTCIceCandidate(data.candidate));
            } catch (e) {
                console.warn('Failed to add ICE candidate immediately, possibly still connecting:', e.message);
                // 不再使用setTimeout缓存，依赖onconnectionstatechange或其他机制
            }
        }
    } catch (error) {
        console.error(`处理信令错误 (from ${data.sender_id}, type ${data.type}):`, error);
    }
}

// 显示语音活动 (更新以适配新的卡片结构和类名)
function showSpeakingIndicator(userId, isSpeaking) {
    const userCard = document.getElementById(`voice-user-${userId}`);
    if (userCard) {
        if (isSpeaking) {
            userCard.classList.add('speaking');
        } else {
            userCard.classList.remove('speaking');
        }
    }
}

// 麦克风音量检测 (使用 app.py 中定义的事件名)
function setupVolumeDetection(channelId) {
    if (!localStream || !webrtcSocket || !webrtcCurrentUserId) return;
    currentChannelIdForWebRTC = channelId;
    
    const audioContext = new (window.AudioContext || window.webkitAudioContext)();
    const analyser = audioContext.createAnalyser();
    const microphone = audioContext.createMediaStreamSource(localStream);
    
    analyser.smoothingTimeConstant = 0.5; // 增加平滑时间，减少波动
    analyser.fftSize = 256; // 更小的fftSize，响应更快，但也可能更噪
    const dataArray = new Uint8Array(analyser.frequencyBinCount);
    microphone.connect(analyser);

    let speakingCurrently = false;
    let silenceCounter = 0; // 用于检测持续的静默
    const REQUIRED_SILENCE_FRAMES = 10; // 需要多少帧静默才认为停止说话 (10 * ~16ms = ~160ms)
    const SPEAKING_AVG_THRESHOLD = 10; // 提高平均音量阈值
    const SPEAKING_PEAK_THRESHOLD = 30; // 添加一个峰值阈值，防止单个突波误判

    function detectSpeaking() {
        if (!localStream || !analyser) { 
            return;
        }
        analyser.getByteFrequencyData(dataArray);
        let sum = 0;
        let peak = 0;
        for (const amplitude of dataArray) {
            sum += amplitude;
            if (amplitude > peak) {
                peak = amplitude;
            }
        }
        const average = sum / dataArray.length;
        
        // console.log(`Avg: ${average.toFixed(2)}, Peak: ${peak}`); // 调试时取消注释

        if ((average > SPEAKING_AVG_THRESHOLD || peak > SPEAKING_PEAK_THRESHOLD) && !isMuted) {
            silenceCounter = 0; // 重置静默计数器
            if (!speakingCurrently) {
                speakingCurrently = true;
                webrtcSocket.emit('user_speaking_status', { speaking: true, channel_id: currentChannelIdForWebRTC });
                showSpeakingIndicator(webrtcCurrentUserId, true);
                // console.log("User started speaking"); // 调试
            }
        } else {
            silenceCounter++;
            if (speakingCurrently && silenceCounter >= REQUIRED_SILENCE_FRAMES) {
                speakingCurrently = false;
                webrtcSocket.emit('user_speaking_status', { speaking: false, channel_id: currentChannelIdForWebRTC }); 
                showSpeakingIndicator(webrtcCurrentUserId, false);
                // console.log("User stopped speaking"); // 调试
            }
        }
        requestAnimationFrame(detectSpeaking);
    }
    requestAnimationFrame(detectSpeaking);
    console.log("Volume detection setup with new thresholds and logic.");
}

// 处理其他用户说话状态 (使用 app.py 中定义的事件名)
function handleUserSpeaking(data) {
    // data: { user_id: ..., speaking: ...}
    if (data.user_id !== webrtcCurrentUserId) { // 只处理其他用户的状态
        showSpeakingIndicator(data.user_id, data.speaking);
    }
}

// 处理用户静音状态 (保持不变，但确保webrtcSocket可用)
function handleUserMuteStatus(data) {
    const userElement = document.getElementById(`voice-user-${data.user_id}`);
    if (userElement) {
        let muteIcon = userElement.querySelector('.mute-icon');
        if (!muteIcon && data.is_muted) {
            muteIcon = document.createElement('span');
            muteIcon.className = 'mute-icon';
            muteIcon.textContent = '🔇';
            muteIcon.style.marginLeft = '5px';
            userElement.appendChild(muteIcon);
        } else if (muteIcon && !data.is_muted) {
            muteIcon.remove();
        }
    }
}

// 当页面关闭时确保清理资源
window.addEventListener('beforeunload', function() {
    if (localStream) {
        localStream.getTracks().forEach(track => track.stop());
    }
    Object.keys(peerConnections).forEach(userId => {
        if (peerConnections[userId]) {
            peerConnections[userId].close();
        }
    });
    // inVoiceChannel 变量在 webrtc.js 中未定义，这个逻辑可能需要移到 main.html 或通过回调处理
    // if (inVoiceChannel && webrtcSocket) { 
    //     webrtcSocket.emit('leave_voice_channel');
    // }
}); 

// 暴露需要从外部调用的函数 (如果 webrtc.js 被当作一个模块)
// export { initializeWebRTC, initializeMedia, toggleMute, createPeerConnection, negotiateConnection, setupVolumeDetection }; 