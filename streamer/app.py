from flask import Flask, render_template, request, jsonify
import subprocess
import json
import os
import signal
import threading
import time

app = Flask(__name__)

CONFIG_FILE = 'config/config.json'
FFMPEG_PROCESS = None
DEFAULT_CONFIG = {
    "input_type": "dji_rtmp",  # "dji_rtmp" or "usb_cam"
    "dji_stream_key": "dji_stream",
    "usb_device": "/dev/video0",
    "usb_input_format": "mjpeg", # Common for USB cams, try "yuyv422" if MJPEG fails
    "usb_resolution": "1280x720",
    "usb_framerate": "30",
    "output_rtsp_url": "rtsp://your-target-rtsp-server:8554/mystream",
    "ffmpeg_loglevel": "info", # "quiet", "panic", "fatal", "error", "warning", "info", "verbose", "debug"
    "re_encode_video": False, # If true, re-encodes video (CPU intensive)
    "video_codec": "libx264", # e.g., libx264, libx265, copy
    "video_preset": "ultrafast", # For libx264/libx265 if re-encoding
    "video_bitrate": "2000k",   # For libx264/libx265 if re-encoding
    "audio_codec": "aac",     # e.g., aac, copy
    "audio_bitrate": "128k",   # if re-encoding
    "usb_audio_device": "",   # e.g., "hw:1,0" for ALSA, or empty to try video device
    "disable_usb_audio": False # Set to true to add -an for USB camera
}

def load_config():
    if not os.path.exists(os.path.dirname(CONFIG_FILE)):
        os.makedirs(os.path.dirname(CONFIG_FILE))
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            try:
                # Merge with defaults to ensure all keys exist
                loaded_conf = json.load(f)
                conf = DEFAULT_CONFIG.copy()
                conf.update(loaded_conf)
                return conf
            except json.JSONDecodeError:
                return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

def stop_ffmpeg_process():
    global FFMPEG_PROCESS
    if FFMPEG_PROCESS and FFMPEG_PROCESS.poll() is None:
        print("Stopping existing FFmpeg process...")
        # Send SIGINT (Ctrl+C) for graceful shutdown, then terminate if needed
        FFMPEG_PROCESS.send_signal(signal.SIGINT)
        try:
            FFMPEG_PROCESS.wait(timeout=10) # Wait up to 10 seconds
        except subprocess.TimeoutExpired:
            print("FFmpeg did not terminate gracefully, sending SIGKILL.")
            FFMPEG_PROCESS.kill()
            FFMPEG_PROCESS.wait() # Ensure it's reaped
        FFMPEG_PROCESS = None
        print("FFmpeg process stopped.")
    elif FFMPEG_PROCESS: # Process already exited
        FFMPEG_PROCESS = None # Clear the stale process object

def start_ffmpeg_process(config):
    global FFMPEG_PROCESS
    stop_ffmpeg_process() # Ensure any existing process is stopped

    input_options = []
    additional_input_options = [] # For separate audio inputs, etc.
    input_url = ""

    if config["input_type"] == "dji_rtmp":
        # Nginx RTMP server is 'nginx-rtmp' inside Docker network
        input_url = f"rtmp://nginx-rtmp:1935/live/{config['dji_stream_key']}"
        input_options.extend(["-fflags", "nobuffer", "-flags", "low_delay"])
    elif config["input_type"] == "usb_cam":
        input_url = config["usb_device"]
        input_options.extend([
            "-f", "v4l2",
            "-input_format", config["usb_input_format"],
            "-video_size", config["usb_resolution"],
            "-framerate", config["usb_framerate"]
        ])
        # Check for separate USB audio device if not disabled
        if not config.get("disable_usb_audio", False) and config.get("usb_audio_device"):
            # Assuming ALSA for Linux. User needs to ensure the device exists.
            # Example: "hw:1,0". Could add config for audio input format if needed.
            additional_input_options.extend(["-f", "alsa", "-i", config["usb_audio_device"]])
    else:
        print(f"Unknown input type: {config['input_type']}")
        return

    output_rtsp_url = config["output_rtsp_url"]

    # Codec options
    video_codec_opts = []
    audio_codec_opts = []
    no_audio_output = False

    if config.get("re_encode_video", False):
        video_codec_opts.extend([
            "-c:v", config.get("video_codec", "libx264"),
            "-preset", config.get("video_preset", "ultrafast"),
            "-b:v", config.get("video_bitrate", "2000k"),
            "-tune", "zerolatency"
        ])
    elif config["input_type"] == "usb_cam": # Force re-encode for USB if copy is selected, as M-JPEG (common USB format) is not widely supported by HLS
        print("USB camera input detected with video copy requested. Forcing H264 encoding for HLS compatibility.")
        video_codec_opts.extend([
            "-c:v", "libx264", # Force H264
            "-preset", config.get("video_preset", "ultrafast"),
            "-b:v", config.get("video_bitrate", "2000k"),
            "-tune", "zerolatency"
        ])
    else:
        video_codec_opts.extend(["-c:v", "copy"])

    # Audio codec configuration
    if config["input_type"] == "usb_cam" and config.get("disable_usb_audio", False):
        no_audio_output = True
    elif config["input_type"] == "usb_cam":
        # Always re-encode audio for USB cam if not disabled,
        # as USB audio is often raw (PCM) or needs specific handling.
        # This applies whether using audio from video device or separate audio device.
        audio_codec_opts.extend([
            "-c:a", config.get("audio_codec", "aac"),
            "-b:a", config.get("audio_bitrate", "128k")
        ])
    elif config["input_type"] == "dji_rtmp":
        if config.get("re_encode_video", False): # Re-encode audio if video is re-encoded
             audio_codec_opts.extend([
                "-c:a", config.get("audio_codec", "aac"),
                "-b:a", config.get("audio_bitrate", "128k")
            ])
        else: # Copy audio for DJI RTMP if not re-encoding video
            audio_codec_opts.extend(["-c:a", "copy"])
    # else: # No audio opts for other potential future input types unless specified

    # Construct FFmpeg command
    ffmpeg_cmd_parts = [
        "ffmpeg",
        "-loglevel", config["ffmpeg_loglevel"],
        *input_options,
        *additional_input_options, # Add separate audio input here if configured
    ]

    ffmpeg_cmd_parts.extend(video_codec_opts)

    if no_audio_output:
        ffmpeg_cmd_parts.append("-an") # Add -an (no audio) if requested
    else:
        ffmpeg_cmd_parts.extend(audio_codec_opts) # Add audio codec options

    ffmpeg_cmd_parts.extend([
        "-f", "rtsp",
        "-rtsp_transport", "tcp", # More reliable over networks
        output_rtsp_url
    ])
    ffmpeg_cmd = ffmpeg_cmd_parts

    print(f"Starting FFmpeg with command: {' '.join(ffmpeg_cmd)}")
    try:
        # Start FFmpeg in a way that we can monitor its output if needed
        FFMPEG_PROCESS = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(f"FFmpeg process started with PID: {FFMPEG_PROCESS.pid}")

        # Optional: Thread to monitor FFmpeg output
        def monitor_ffmpeg_output(proc):
            for line in iter(proc.stdout.readline, b''):
                print(f"[FFmpeg]: {line.decode().strip()}")
            proc.stdout.close()
            return_code = proc.wait()
            if return_code:
                print(f"FFmpeg process exited with error code {return_code}")
            else:
                print("FFmpeg process exited cleanly.")

        threading.Thread(target=monitor_ffmpeg_output, args=(FFMPEG_PROCESS,), daemon=True).start()

    except Exception as e:
        print(f"Failed to start FFmpeg: {e}")
        FFMPEG_PROCESS = None


@app.route('/', methods=['GET', 'POST'])
def index():
    config = load_config()
    if request.method == 'POST':
        form_data = request.form.to_dict()
        # Convert checkbox values
        form_data['re_encode_video'] = 're_encode_video' in request.form
        form_data['disable_usb_audio'] = 'disable_usb_audio' in request.form

        config.update(form_data)
        save_config(config)
        print("Configuration updated. Restarting FFmpeg...")
        start_ffmpeg_process(config) # Restart FFmpeg with new config
        return jsonify(success=True, message="Configuration saved and FFmpeg (re)started.", config=config)

    return render_template('index.html', config=config)

@app.route('/status', methods=['GET'])
def status():
    global FFMPEG_PROCESS
    config = load_config()
    is_running = FFMPEG_PROCESS is not None and FFMPEG_PROCESS.poll() is None
    pid = FFMPEG_PROCESS.pid if is_running else None
    return jsonify(
        is_running=is_running,
        pid=pid,
        config=config,
        ffmpeg_command=' '.join(FFMPEG_PROCESS.args) if is_running and hasattr(FFMPEG_PROCESS, 'args') else "Not running"
    )

@app.route('/start', methods=['POST'])
def start_stream():
    config = load_config()
    start_ffmpeg_process(config)
    return jsonify(success=True, message="FFmpeg process initiated.")

@app.route('/stop', methods=['POST'])
def stop_stream():
    stop_ffmpeg_process()
    return jsonify(success=True, message="FFmpeg process stopped.")


if __name__ == '__main__':
    # Ensure config directory exists
    if not os.path.exists(os.path.dirname(CONFIG_FILE)):
        os.makedirs(os.path.dirname(CONFIG_FILE))

    # Load config and start FFmpeg on initial startup
    initial_config = load_config()
    if initial_config.get("output_rtsp_url"): # Only start if output is configured
        start_ffmpeg_process(initial_config)

    app.run(host='0.0.0.0', port=5001)