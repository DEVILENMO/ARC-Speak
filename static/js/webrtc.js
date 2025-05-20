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

console.log("webrtc.js loaded"); // 确认文件加载

// 初始化WebRTC模块，传入socket实例和当前用户ID
function initializeWebRTC(socketInstance, currentUserId) {
    webrtcSocket = socketInstance;
    webrtcCurrentUserId = currentUserId;
    console.debug("[WebRTC] Initializing WebRTC with Socket and User ID:", webrtcCurrentUserId);

    // 将所有依赖 socket 的事件监听器移到这里
    webrtcSocket.on('voice_signal', handleVoiceSignal);
    webrtcSocket.on('user_speaking', handleUserSpeaking);
    webrtcSocket.on('user_mute_status', handleUserMuteStatus);
    console.debug("[WebRTC] Event listeners for voice_signal, user_speaking, user_mute_status attached.");
}

// 初始化媒体流
async function initializeMedia() {
    console.debug("[WebRTC] Attempting to initialize media...");
    try {
        const preferredMicrophone = localStorage.getItem('selectedAudioInput'); // <--- 确保这里用的是 selectedAudioInput
        const constraints = {
            audio: preferredMicrophone && preferredMicrophone !== 'default' ? 
                { deviceId: { exact: preferredMicrophone } } : 
                true
        };
        console.debug("[WebRTC] getUserMedia constraints:", constraints);
        
        localStream = await navigator.mediaDevices.getUserMedia(constraints);
        console.log('[WebRTC] Microphone initialized (localStream):', localStream);

        if (localStream && localStream.getAudioTracks().length > 0) {
            console.debug("[WebRTC] Local stream has audio tracks:", localStream.getAudioTracks());
        } else {
            console.warn("[WebRTC] Local stream acquired but has NO audio tracks.");
        }

        if (DEBUG_LOOPBACK && localStream) {
            console.debug("[WebRTC] DEBUG_LOOPBACK enabled. Creating local audio loopback.");
            const audioContext = new (window.AudioContext || window.webkitAudioContext)();
            const source = audioContext.createMediaStreamSource(localStream);
            const gainNode = audioContext.createGain();
            gainNode.gain.value = 0.7; // 设置一个适中的回环音量，避免过大
            source.connect(gainNode);
            gainNode.connect(audioContext.destination); // 连接到默认输出
            console.debug("[WebRTC] Local debug loopback enabled, volume 0.7");
        }

        // 在成功获取localStream后，设置音量检测
        // (由 main.html 中的 joinVoiceChannel 调用此函数后，再调用 setupVolumeDetection)

    } catch (error) {
        console.error('[WebRTC] Error initializing media (getUserMedia):', error);
        alert('无法访问麦克风。请检查浏览器权限设置和控制台错误。错误: ' + error.message);
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
    console.debug(`[WebRTC] Creating PeerConnection for user: ${userId}`);
    if (peerConnections[userId]) {
        console.debug(`[WebRTC] PeerConnection for user ${userId} already exists.`);
        return peerConnections[userId];
    }
    
    const RTCPeerConnection = window.RTCPeerConnection || window.mozRTCPeerConnection || window.webkitRTCPeerConnection;
    if (!RTCPeerConnection) {
        console.error("[WebRTC] FATAL: RTCPeerConnection is not supported by this browser.");
        alert("WebRTC PeerConnection is not supported by this browser.");
        return null;
    }
    console.debug("[WebRTC] RTCPeerConnection constructor available.");

    const pc = new RTCPeerConnection(configuration);
    peerConnections[userId] = pc;
    console.debug(`[WebRTC] PeerConnection created for user ${userId}. Configuration:`, JSON.parse(JSON.stringify(configuration))); // Deep copy for logging
    
    if (localStream) {
        console.debug(`[WebRTC] Adding localStream tracks to PeerConnection for user ${userId}.`);
        localStream.getTracks().forEach(track => {
            try {
                pc.addTrack(track, localStream);
                console.debug(`[WebRTC] Added track ${track.kind} (id: ${track.id}) to PC for ${userId}`);
            } catch (e) {
                console.error(`[WebRTC] Error adding track ${track.id} to PC for ${userId}:`, e);
            }
        });
    } else {
        console.warn(`[WebRTC] localStream is not available when creating PeerConnection for ${userId}. Remote user might not receive audio.`);
    }
    
    pc.onicecandidate = event => {
        if (event.candidate) {
            console.debug(`[WebRTC] ICE candidate generated for user ${userId}:`, event.candidate);
            if (webrtcSocket) {
                webrtcSocket.emit('voice_signal', {
                    type: 'ice_candidate',
                    candidate: event.candidate,
                    recipient_id: userId
                });
                console.debug(`[WebRTC] Sent ICE candidate to user ${userId}.`);
            } else {
                console.warn("[WebRTC] onicecandidate: webrtcSocket not available to send ICE candidate.");
            }
        } else {
            console.debug(`[WebRTC] All ICE candidates have been sent for user ${userId}.`);
        }
    };
    
    pc.onconnectionstatechange = event => {
        console.log(`[WebRTC] PeerConnection state change for user ${userId}: ${pc.connectionState}`);
        if (pc.connectionState === 'failed') {
            console.error(`[WebRTC] PeerConnection for user ${userId} failed. Check ICE server configuration and network.`);
        }
        // Further handling for states like 'disconnected', 'closed' can be added here.
    };

    pc.onicecandidateerror = event => {
        console.error(`[WebRTC] ICE candidate error for user ${userId}:`, event);
    };

    // Temporarily comment out onnegotiationneeded to simplify initiation logic
    /*
    pc.onnegotiationneeded = async event => {
        console.debug(`[WebRTC] Negotiation needed for user ${userId}. Event:`, event);
        // Condition to prevent repeated negotiation if already in progress or state is not stable
        // The signalingState check helps prevent glare, !makingOffer prevents re-entrancy.
        if (!makingOffer[userId] && pc.signalingState === 'stable') {
            console.debug(`[WebRTC] Inside onnegotiationneeded, about to call negotiateConnection for ${userId}. SignalingState: ${pc.signalingState}, MakingOffer: ${makingOffer[userId]}`);
            await negotiateConnection(userId);
        } else {
            console.debug(`[WebRTC] Skipping negotiation for ${userId} due to signalingState ${pc.signalingState} or offer already in progress (${makingOffer[userId]}).`);
        }
    };
    */

    pc.ontrack = event => {
        console.log(`[WebRTC] Received remote track from user ${userId}:`, event.track, 'Stream(s):', event.streams);
        const remoteAudioContainer = document.getElementById('remote-audio-container');
        if (!remoteAudioContainer) {
            console.error("[WebRTC] ontrack: Remote audio container (remote-audio-container) not found!");
            return;
        }

        let remoteAudio = document.getElementById(`audio-${userId}`);
        if (!remoteAudio) {
            console.debug(`[WebRTC] Creating <audio> element for remote user ${userId}`);
            remoteAudio = document.createElement('audio');
            remoteAudio.id = `audio-${userId}`;
            remoteAudio.autoplay = true; // Autoplay is crucial
            
            const preferredSpeaker = localStorage.getItem('selectedAudioOutput'); // 从 settings.html 读取选择
            if (preferredSpeaker && preferredSpeaker !== 'default' && typeof remoteAudio.setSinkId === 'function') {
                console.debug(`[WebRTC] Attempting to set SinkId to ${preferredSpeaker} for user ${userId}`);
                remoteAudio.setSinkId(preferredSpeaker)
                    .then(() => console.log(`[WebRTC] Audio output successfully set to ${preferredSpeaker} for user ${userId}`))
                    .catch(error => console.error(`[WebRTC] Error setting SinkId for user ${userId}:`, error));
            } else {
                console.debug(`[WebRTC] Using default audio output for user ${userId}.`);
            }
            remoteAudioContainer.appendChild(remoteAudio);
            console.debug(`[WebRTC] Appended <audio id='audio-${userId}'> to remote-audio-container.`);
        }
        
        if (remoteAudio.srcObject !== event.streams[0]) {
            console.log(`[WebRTC] Attaching remote stream from user ${userId} to <audio> element.`);
            remoteAudio.srcObject = event.streams[0];
            remoteAudio.play().then(() => {
                console.log(`[WebRTC] Remote audio for user ${userId} is playing.`);
            }).catch(e => {
                console.error(`[WebRTC] Error attempting to play remote audio for user ${userId}:`, e, 
                              "This might be due to browser autoplay policies. User interaction might be needed.");
                // alert(`Could not automatically play audio from user ${userId}. Please interact with the page (e.g., click) and try again.`);
            });
        } else {
            console.debug(`[WebRTC] Remote stream for user ${userId} already attached to <audio> element.`);
        }
    };
    
    pc.ondatachannel = event => {
        console.log(`[WebRTC] Received data channel from user ${userId}:`, event.channel);
        // Handle data channel event
    };
    
    return pc;
}

// 协商连接
async function negotiateConnection(userId) {
    console.debug(`[WebRTC] Initiating negotiation with user: ${userId}`);
    if (!webrtcSocket) {
        console.error("[WebRTC] negotiateConnection: webrtcSocket not available.");
        return;
    }
    try {
        const pc = createPeerConnection(userId);
        if (!pc) {
            console.error(`[WebRTC] negotiateConnection: Failed to create PeerConnection for user ${userId}.`);
            return;
        }
        
        console.debug(`[WebRTC] Creating offer for user ${userId}...`);
        const offer = await pc.createOffer();
        console.debug(`[WebRTC] Offer created for user ${userId}:`, offer);
        
        await pc.setLocalDescription(offer);
        console.debug(`[WebRTC] Local description set for user ${userId}. SDP:`, pc.localDescription);
        
        webrtcSocket.emit('voice_signal', {
            type: 'offer',
            sdp: pc.localDescription,
            recipient_id: userId
        });
        console.log(`[WebRTC] Sent offer to user ${userId}.`);
    } catch (error) {
        console.error(`[WebRTC] Error during negotiation (offer) with user ${userId}:`, error);
    }
}

// 处理信令 (作为回调函数)
async function handleVoiceSignal(data) {
    console.debug("[WebRTC] Received voice_signal:", data);
    if (!webrtcSocket || !data.sender_id) {
        console.error("[WebRTC] handleVoiceSignal: webrtcSocket or sender_id missing in data.", data);
        return;
    }
    
    const senderId = data.sender_id;
    console.debug(`[WebRTC] Processing voice_signal from user ${senderId}, type: ${data.type}`);
    
    const pc = createPeerConnection(senderId); 
    if (!pc) {
        console.error(`[WebRTC] handleVoiceSignal: Failed to create/get PeerConnection for sender ${senderId}.`);
        return;
    }

    try {
        if (data.type === 'offer') {
            console.debug(`[WebRTC] Received offer from ${senderId}. SDP:`, data.sdp);
            await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
            console.debug(`[WebRTC] Remote description (offer) set for ${senderId}.`);
            
            console.debug(`[WebRTC] Creating answer for ${senderId}...`);
            const answer = await pc.createAnswer();
            console.debug(`[WebRTC] Answer created for ${senderId}:`, answer);
            
            await pc.setLocalDescription(answer);
            console.debug(`[WebRTC] Local description (answer) set for ${senderId}. SDP:`, pc.localDescription);
            
            webrtcSocket.emit('voice_signal', {
                type: 'answer',
                sdp: pc.localDescription,
                recipient_id: senderId
            });
            console.log(`[WebRTC] Sent answer to ${senderId}.`);

        } else if (data.type === 'answer') {
            console.debug(`[WebRTC] Received answer from ${senderId}. SDP:`, data.sdp);
            await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
            console.debug(`[WebRTC] Remote description (answer) set for ${senderId}.`);

        } else if (data.type === 'ice_candidate' && data.candidate) {
            console.debug(`[WebRTC] Received ICE candidate from ${senderId}:`, data.candidate);
            try {
                await pc.addIceCandidate(new RTCIceCandidate(data.candidate));
                console.debug(`[WebRTC] Added ICE candidate from ${senderId}.`);
            } catch (e) {
                console.warn(`[WebRTC] Failed to add ICE candidate from ${senderId}: ${e.message}. PC state: ${pc.signalingState}`);
                // Ice candidates might arrive before the remote description is set, especially if it's an offer.
                // Browsers typically queue them, but explicit queuing might be needed in some race conditions.
            }
        } else {
            console.warn("[WebRTC] Received unknown or incomplete voice_signal type:", data);
        }
    } catch (error) {
        console.error(`[WebRTC] Error processing voice_signal from ${senderId} (type ${data.type}):`, error);
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