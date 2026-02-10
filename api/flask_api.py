from flask import Flask, request, jsonify, Response, render_template_string, send_file
import os
import uuid
import httpx
import asyncio
from apify_client import ApifyClient
import json
import time
import requests
from urllib.parse import quote
import io
import tempfile

app = Flask(__name__)

# Initialize Apify client
client = ApifyClient("apify_api_8gRV9FC5Igq5gQ40effgxuEaJBnzYm23Krp2")

# Store sessions in memory
SESSIONS = {}

def get_terabox_download_url(terabox_url):
    """Get download URL from Terabox using Apify"""
    run_input = {
        "links": [terabox_url],
        "proxyConfiguration": {
            "useApifyProxy": True,
            "apifyProxyGroups": ["RESIDENTIAL"],
        },
    }
    
    try:
        run = client.actor("2EXlXqasdIPsVkOWB").call(run_input=run_input)
        
        for item in client.dataset(run["defaultDatasetId"]).iterate_items():
            if item.get("success") and item.get("download_url"):
                return item["download_url"]
        
        return None
    except Exception as e:
        print(f"Apify error: {e}")
        return None

@app.route("/process", methods=["POST"])
def process_terabox():
    """Process Terabox link - Return direct streaming URLs"""
    terabox_url = request.json.get("url")
    
    if not terabox_url:
        return jsonify({"status": "error", "message": "URL missing"}), 400
    
    try:
        # Get Terabox download URL
        download_url = get_terabox_download_url(terabox_url)
        if not download_url:
            return jsonify({"status": "error", "message": "Failed to get download link"}), 500
        
        print(f"Got Terabox URL: {download_url[:100]}...")
        
        # Create session ID
        session_id = str(uuid.uuid4())[:12]
        
        # Store session
        SESSIONS[session_id] = {
            "download_url": download_url,
            "created": time.time(),
            "expire": time.time() + 7200,  # 2 hours
            "status": "ready",
            "filename": f"video_{session_id}.mp4"
        }
        
        # Create URLs
        player_url = f"/player/{session_id}"
        stream_url = f"/stream/{session_id}"
        download_endpoint = f"/download/{session_id}"
        
        # For production, use the actual host URL
        base_url = request.host_url.rstrip('/')
        
        return jsonify({
            "status": "success",
            "session_id": session_id,
            "player_url": f"{base_url}{player_url}",
            "stream_url": f"{base_url}{stream_url}",
            "download_endpoint": f"{base_url}{download_endpoint}",
            "expires_in": 7200,
            "message": "Video ready for streaming and download"
        })
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/stream/<session_id>")
def stream_video(session_id):
    """Stream video with proper headers to bypass Terabox protection"""
    session = SESSIONS.get(session_id)
    
    if not session:
        return "Session expired", 404
    
    video_url = session["download_url"]
    
    # Terabox requires specific headers
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.terabox.com/',
        'Accept': 'video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'Range': request.headers.get('Range', 'bytes=0-'),
        'Origin': 'https://www.terabox.com',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'video',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'cross-site',
        'Pragma': 'no-cache',
        'Cache-Control': 'no-cache',
    }
    
    # Forward the request to Terabox
    try:
        req = requests.get(video_url, headers=headers, stream=True, timeout=30)
        
        if req.status_code == 403 or req.status_code == 404:
            # Try alternative headers
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://terabox.com/',
                'Accept': '*/*',
            }
            req = requests.get(video_url, headers=headers, stream=True, timeout=30)
        
        # Get response headers
        response_headers = dict(req.headers)
        
        # Fix headers for proper streaming
        if 'Content-Length' in response_headers:
            response_headers['Content-Length'] = str(response_headers['Content-Length'])
        
        # Ensure correct content type
        response_headers['Content-Type'] = req.headers.get('Content-Type', 'video/mp4')
        
        # Add CORS headers
        response_headers['Access-Control-Allow-Origin'] = '*'
        response_headers['Access-Control-Allow-Headers'] = '*'
        response_headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
        response_headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range'
        
        # Add range support
        response_headers['Accept-Ranges'] = 'bytes'
        response_headers['Cache-Control'] = 'no-cache'
        
        # Remove problematic headers
        for header in ['Transfer-Encoding', 'Connection', 'Content-Encoding']:
            response_headers.pop(header, None)
        
        # Stream the response
        def generate():
            for chunk in req.iter_content(chunk_size=1024*1024):  # 1MB chunks
                if chunk:
                    yield chunk
        
        return Response(generate(), headers=response_headers, status=req.status_code)
        
    except Exception as e:
        print(f"Stream error: {str(e)}")
        return f"Streaming error: {str(e)}", 500

@app.route("/download/<session_id>")
def download_video(session_id):
    """Instant streaming download - starts downloading immediately"""
    session = SESSIONS.get(session_id)
    
    if not session:
        return "Session expired", 404
    
    video_url = session["download_url"]
    filename = session.get("filename", "terabox_video.mp4")
    
    # Headers for Terabox
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.terabox.com/',
        'Range': request.headers.get('Range', 'bytes=0-'),
    }
    
    try:
        # Stream directly from Terabox to client
        req = requests.get(video_url, headers=headers, stream=True, timeout=300)
        req.raise_for_status()
        
        # Get file size for progress
        file_size = int(req.headers.get('content-length', 0))
        
        # Create a streaming response
        def generate():
            chunk_size = 8192 * 1024  # 8MB chunks for faster download
            for chunk in req.iter_content(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        
        # Set headers for instant download
        response_headers = {
            'Content-Type': 'video/mp4',
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Length': str(file_size) if file_size > 0 else '',
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive'
        }
        
        # Remove Transfer-Encoding header for direct download
        if 'Transfer-Encoding' in req.headers:
            del req.headers['Transfer-Encoding']
        
        # Return streaming response that starts immediately
        return Response(
            generate(),
            headers=response_headers,
            status=200,
            direct_passthrough=True
        )
        
    except Exception as e:
        print(f"Download error: {str(e)}")
        
        # Fallback: Create a simple HTML page with direct link
        html = f'''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>Download Video</title>
            <meta http-equiv="refresh" content="0; url={video_url}">
            <script>
                window.location.href = "{video_url}";
            </script>
        </head>
        <body>
            <p>If download doesn't start automatically, <a href="{video_url}" download>click here</a></p>
        </body>
        </html>
        '''
        return html

@app.route("/direct_download/<session_id>")
def direct_download(session_id):
    """Alternative download endpoint with meta refresh for instant start"""
    session = SESSIONS.get(session_id)
    
    if not session:
        return "Session expired", 404
    
    video_url = session["download_url"]
    filename = session.get("filename", "terabox_video.mp4")
    
    # HTML page with meta refresh and JavaScript for instant download
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Downloading {filename}...</title>
        <style>
            body {{
                margin: 0;
                padding: 40px;
                background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
                color: white;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                text-align: center;
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
            }}
            .container {{
                max-width: 500px;
                padding: 40px;
                background: rgba(255, 255, 255, 0.1);
                border-radius: 20px;
                backdrop-filter: blur(10px);
            }}
            .spinner {{
                width: 60px;
                height: 60px;
                border: 4px solid rgba(255, 255, 255, 0.1);
                border-top-color: #6366f1;
                border-radius: 50%;
                animation: spin 1s linear infinite;
                margin: 0 auto 30px;
            }}
            @keyframes spin {{
                to {{ transform: rotate(360deg); }}
            }}
            .progress {{
                width: 100%;
                height: 8px;
                background: rgba(255, 255, 255, 0.1);
                border-radius: 4px;
                margin: 20px 0;
                overflow: hidden;
            }}
            .progress-bar {{
                height: 100%;
                background: linear-gradient(90deg, #6366f1, #8b5cf6);
                animation: loading 2s infinite;
            }}
            @keyframes loading {{
                0% {{ width: 0%; }}
                50% {{ width: 70%; }}
                100% {{ width: 100%; }}
            }}
            .btn {{
                display: inline-block;
                background: linear-gradient(135deg, #10b981 0%, #059669 100%);
                color: white;
                padding: 14px 28px;
                border-radius: 50px;
                text-decoration: none;
                font-weight: 600;
                margin-top: 20px;
                transition: transform 0.3s;
            }}
            .btn:hover {{
                transform: translateY(-3px);
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="spinner"></div>
            <h2>🎬 Download Starting...</h2>
            <p>Your video download should start automatically.</p>
            
            <div class="progress">
                <div class="progress-bar"></div>
            </div>
            
            <p style="color: #94a3b8; margin: 20px 0;">
                If download doesn't start in 5 seconds, click below:
            </p>
            
            <a href="{video_url}" download="{filename}" class="btn">
                📥 Click to Download Now
            </a>
            
            <p style="margin-top: 30px; font-size: 14px; color: #64748b;">
                <i>Downloading directly from Terabox servers...</i>
            </p>
        </div>
        
        <script>
            // Try to trigger download immediately
            function startDownload() {{
                const link = document.createElement('a');
                link.href = "{video_url}";
                link.download = "{filename}";
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
                
                // Update UI
                document.querySelector('h2').textContent = '✅ Download Started!';
                document.querySelector('p').textContent = 'Your video is now downloading...';
            }}
            
            // Try multiple methods for instant download
            setTimeout(startDownload, 100);
            
            // Fallback after 3 seconds
            setTimeout(() => {{
                window.location.href = "{video_url}";
            }}, 3000);
            
            // Show time elapsed
            let seconds = 0;
            setInterval(() => {{
                seconds++;
                const timer = document.getElementById('timer');
                if (timer) {{
                    timer.textContent = seconds + 's';
                }}
            }}, 1000);
        </script>
    </body>
    </html>
    '''
    
    return html

@app.route("/player/<session_id>")
def video_player(session_id):
    """Fixed HTML video player with correct download button"""
    session = SESSIONS.get(session_id)
    
    if not session:
        return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {
                    margin: 0;
                    padding: 0;
                    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                    color: white;
                    font-family: 'Segoe UI', sans-serif;
                    height: 100vh;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    text-align: center;
                }
                .error {
                    background: rgba(239, 68, 68, 0.1);
                    padding: 30px;
                    border-radius: 15px;
                    border: 2px solid rgba(239, 68, 68, 0.3);
                    max-width: 400px;
                }
            </style>
        </head>
        <body>
            <div class="error">
                <h1 style="color: #ef4444; font-size: 48px;">⏰</h1>
                <h2>Session Expired</h2>
                <p>This video link has expired.</p>
                <p style="color: #94a3b8; margin-top: 20px;">
                    Please get a new link from the Telegram bot.
                </p>
            </div>
        </body>
        </html>
        ''')
    
    stream_url = f"/stream/{session_id}"
    # FIX: Use the direct download endpoint instead of HTML page
    download_url = f"/direct_download/{session_id}"
    
    # Get base URL for absolute paths
    base_url = request.host_url.rstrip('/')
    full_stream_url = f"{base_url}{stream_url}"
    full_download_url = f"{base_url}{download_url}"
    
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>🎬 Terabox Video Player</title>
        <link href="https://vjs.zencdn.net/8.0.4/video-js.css" rel="stylesheet" />
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            
            body {{
                background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
                color: white;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                min-height: 100vh;
                display: flex;
                flex-direction: column;
            }}
            
            .header {{
                background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
                padding: 20px;
                text-align: center;
                box-shadow: 0 4px 20px rgba(99, 102, 241, 0.3);
            }}
            
            .header h1 {{
                font-size: 24px;
                margin-bottom: 5px;
            }}
            
            .header p {{
                opacity: 0.9;
                font-size: 14px;
            }}
            
            .main-container {{
                flex: 1;
                display: flex;
                flex-direction: column;
                align-items: center;
                padding: 20px;
            }}
            
            .video-container {{
                width: 100%;
                max-width: 1000px;
                background: #000;
                border-radius: 10px;
                overflow: hidden;
                margin-bottom: 20px;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
            }}
            
            #my-video {{
                width: 100%;
                height: auto;
                max-height: 70vh;
            }}
            
            .controls {{
                display: flex;
                flex-wrap: wrap;
                gap: 15px;
                justify-content: center;
                margin-top: 20px;
                max-width: 1000px;
                width: 100%;
            }}
            
            .btn {{
                padding: 15px 25px;
                border: none;
                border-radius: 10px;
                font-size: 16px;
                font-weight: 600;
                cursor: pointer;
                display: flex;
                align-items: center;
                gap: 10px;
                text-decoration: none;
                color: white;
                transition: all 0.3s;
            }}
            
            .btn-download {{
                background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            }}
            
            .btn-fullscreen {{
                background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
            }}
            
            .btn:hover {{
                transform: translateY(-3px);
                box-shadow: 0 8px 25px rgba(0, 0, 0, 0.4);
            }}
            
            .info {{
                background: rgba(255, 255, 255, 0.05);
                padding: 20px;
                border-radius: 10px;
                margin-top: 20px;
                max-width: 1000px;
                width: 100%;
                text-align: center;
                color: #94a3b8;
            }}
            
            @media (max-width: 768px) {{
                .controls {{
                    flex-direction: column;
                }}
                
                .btn {{
                    width: 100%;
                    justify-content: center;
                }}
                
                .video-container {{
                    border-radius: 0;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🎬 Terabox Video Player</h1>
            <p>Streaming directly from Terabox • No downloads needed</p>
        </div>
        
        <div class="main-container">
            <div class="video-container">
                <video
                    id="my-video"
                    class="video-js vjs-default-skin vjs-big-play-centered"
                    controls
                    preload="auto"
                    autoplay
                    playsinline
                    data-setup='{{}}'
                >
                    <source src="{full_stream_url}" type="video/mp4" />
                    <p class="vjs-no-js">
                        To view this video please enable JavaScript, and consider upgrading to a
                        web browser that <a href="https://videojs.com/html5-video-support/" target="_blank">supports HTML5 video</a>
                    </p>
                </video>
            </div>
            
            <div class="controls">
                <!-- FIX: Use the direct_download endpoint which will trigger instant download -->
                <a href="{full_download_url}" class="btn btn-download" onclick="startDownload(event, '{full_download_url}')">
                    <svg width="20" height="20" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/>
                    </svg>
                    Download Video
                </a>
                
                <button onclick="toggleFullscreen()" class="btn btn-fullscreen">
                    <svg width="20" height="20" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/>
                    </svg>
                    Fullscreen
                </button>
            </div>
            
            <div class="info">
                <p>🎥 This video is streamed directly from Terabox through our secure proxy.</p>
                <p style="margin-top: 10px; font-size: 14px;">
                    🔒 Secure connection • ⚡ Fast streaming • 📱 Mobile friendly
                </p>
            </div>
        </div>
        
        <script src="https://vjs.zencdn.net/8.0.4/video.min.js"></script>
        <script>
            // Initialize video.js player
            const player = videojs('my-video', {{
                controls: true,
                autoplay: true,
                preload: 'auto',
                fluid: true,
                responsive: true,
                playbackRates: [0.5, 1, 1.5, 2],
                controlBar: {{
                    children: [
                        'playToggle',
                        'volumePanel',
                        'currentTimeDisplay',
                        'timeDivider',
                        'durationDisplay',
                        'progressControl',
                        'remainingTimeDisplay',
                        'playbackRateMenuButton',
                        'fullscreenToggle'
                    ]
                }}
            }});
            
            // Fullscreen function
            function toggleFullscreen() {{
                if (player.isFullscreen()) {{
                    player.exitFullscreen();
                }} else {{
                    player.requestFullscreen();
                }}
            }}
            
            // Handle player events
            player.on('error', function() {{
                console.log('Player error occurred');
            }});
            
            player.on('loadeddata', function() {{
                console.log('Video loaded successfully');
            }});
            
            // Auto-play when video is ready
            player.ready(function() {{
                this.play().catch(function(error) {{
                    console.log('Auto-play was prevented:', error);
                }});
            }});
            
            // Download button handler - starts download in background
            function startDownload(event, url) {{
                event.preventDefault();
                
                // Create invisible iframe for download
                const iframe = document.createElement('iframe');
                iframe.style.display = 'none';
                iframe.src = url;
                document.body.appendChild(iframe);
                
                // Also try direct link method
                setTimeout(() => {{
                    const link = document.createElement('a');
                    link.href = url;
                    link.download = 'terabox_video.mp4';
                    document.body.appendChild(link);
                    link.click();
                    document.body.removeChild(link);
                }}, 100);
                
                return false;
            }}
            
            // Keyboard shortcuts
            document.addEventListener('keydown', function(e) {{
                if (e.code === 'KeyF' || e.code === 'KeyK') {{
                    e.preventDefault();
                    if (player.paused()) {{
                        player.play();
                    }} else {{
                        player.pause();
                    }}
                }}
                if (e.code === 'KeyD') {{
                    e.preventDefault();
                    startDownload({{preventDefault: () => {{}}}}, '{full_download_url}');
                }}
            }});
            
            // Mobile optimization
            if (/Android|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent)) {{
                // Force landscape suggestion on mobile
                setTimeout(function() {{
                    alert('💡 Tip: Rotate your phone sideways for better viewing experience!');
                }}, 3000);
            }}
        </script>
    </body>
    </html>
    '''
    
    return html

@app.route("/cleanup")
def cleanup():
    """Clean expired sessions"""
    now = time.time()
    expired = []
    
    for session_id, session in list(SESSIONS.items()):
        if session["expire"] < now:
            expired.append(session_id)
            del SESSIONS[session_id]
    
    return jsonify({
        "cleaned": len(expired),
        "remaining": len(SESSIONS)
    })

@app.route("/")
def index():
    return jsonify({
        "status": "online",
        "service": "Terabox Video Proxy Service",
        "endpoints": {
            "POST /process": "Process Terabox link",
            "GET /player/<id>": "Video player page",
            "GET /stream/<id>": "Direct video stream",
            "GET /download/<id>": "Download video"
        }
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Starting Terabox Video Proxy on port {port}")
    print("✅ Video streaming with Terabox headers fixed")
    print("✅ Download endpoint working")
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
