import io
import os
import paho.mqtt.client as mqtt
from PIL import Image
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import logging
import time
import yaml
import asyncio
from aioesphomeapi import APIClient
import traceback

# --- Global variables ---
current_cover_jpeg = None
current_cover_png = None
# --- STATIC HOSTING CHANGE 1: Add globals to store the default image data ---
default_cover_jpeg_bytes = None
default_cover_png_bytes = None
logger = logging.getLogger(__name__)

# --- Configuration Loading ---
CONFIG_FILE = "config.yaml"
config = {}

def load_config():
    """Loads configuration from YAML."""
    global config
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = yaml.safe_load(f)
        logger.info("Configuration loaded.")
    except Exception as e:
        logger.critical(f"Failed to load configuration: {e}")
        exit(1)

def setup_logging():
    """Sets up logging."""
    log_level = config.get("log_level", "INFO").upper()
    logging.basicConfig(level=log_level,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def load_default_cover():
    """Loads and processes the default cover images."""
    global current_cover_jpeg, current_cover_png, default_cover_jpeg_bytes, default_cover_png_bytes
    try:
        # --- STATIC HOSTING CHANGE 2: Load default images into their own dedicated variables ---
        with Image.open(config["default_cover_jpeg"]) as img:
            processed_img = process_image(img)
            default_cover_jpeg_bytes = img_to_jpeg_bytes(processed_img)
            current_cover_jpeg = default_cover_jpeg_bytes # Also set as current on startup
            logger.info("Loaded default JPEG cover.")
        with Image.open(config["default_cover_png"]) as img:
            processed_img = process_image(img)
            default_cover_png_bytes = img_to_lossy_png_bytes(processed_img)
            current_cover_png = default_cover_png_bytes # Also set as current on startup
            logger.info("Loaded default PNG cover.")
    except FileNotFoundError:
        logger.warning("Default cover image not found.")
    except Exception as e:
        logger.error(f"Error loading default cover: {e}")

def process_image(img):
    """Resizes and converts image to RGB."""
    img_rgb = img.convert('RGB')
    return img_rgb.resize(tuple(config["image_size"]), Image.LANCZOS)

def img_to_jpeg_bytes(img):
    """Converts PIL image to JPEG bytes."""
    with io.BytesIO() as byte_arr:
        img.save(byte_arr, format='JPEG', quality=config["jpeg_quality"])
        return byte_arr.getvalue()

def img_to_lossy_png_bytes(img):
    """Converts PIL image to PNG bytes."""
    with io.BytesIO() as byte_arr:
        img.save(byte_arr, format='PNG', optimize=True)
        return byte_arr.getvalue()

# --- Asynchronous ESPHome Function ---
async def trigger_esphome_action(device_ip, api_password, action_name):
    """Connects and calls a user-defined action on an ESPHome device."""
    logger.info(f"Executing ESPHome action '{action_name}' for {device_ip}")
    cli = APIClient(device_ip, 6053, api_password)
    connected = False
    try:
        await cli.connect(login=True)
        connected = True

        _, services = await cli.list_entities_services()
        service_to_call = next((s for s in services if s.name == action_name), None)

        if service_to_call:
            # This is the corrected, non-awaited call
            cli.execute_service(service_to_call, data={})
            logger.info(f"Successfully scheduled action '{action_name}'.")
            await asyncio.sleep(1) 
        else:
            logger.error(f"Action '{action_name}' not found on device.")
            
    except Exception as e:
        logger.error(f"Error during ESPHome action: {e}")
        logger.error(traceback.format_exc())
    finally:
        if connected:
            await cli.disconnect()

# --- Synchronous MQTT and HTTP parts ---
def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info("Connected to MQTT broker.")
        client.subscribe(config["mqtt_topic_cover"])
    else:
        logger.error(f"MQTT connection failed with code {rc}")

def on_message(client, userdata, msg):
    """Safely schedules the async task on the running event loop."""
    loop = userdata["loop"]
    try:
        if msg.topic == config["mqtt_topic_cover"]:
            with Image.open(io.BytesIO(msg.payload)) as img:
                processed_img = process_image(img)
                global current_cover_jpeg, current_cover_png
                current_cover_jpeg = img_to_jpeg_bytes(processed_img)
                current_cover_png = img_to_lossy_png_bytes(processed_img)
                logger.info("Updated cover image from MQTT.")
                
                if config.get("esphome") and config["esphome"].get("device_ip"):
                    esphome_config = config["esphome"]
                    action_name = esphome_config.get("action_name")
                    if action_name:
                        asyncio.run_coroutine_threadsafe(
                            trigger_esphome_action(
                                esphome_config["device_ip"],
                                esphome_config.get("api_password"),
                                action_name
                            ),
                            loop
                        )
    except Exception as e:
        logger.error(f"Error processing MQTT message: {e}")

class CoverImageHandler(BaseHTTPRequestHandler):
    """HTTP request handler for serving all cover images."""
    def do_GET(self):
        # --- STATIC HOSTING CHANGE 3: Add logic to serve default images from their paths ---
        if self.path == config["served_jpeg_filename"]:
            self.serve_image(current_cover_jpeg, "image/jpeg")
        elif self.path == config["served_png_filename"]:
            self.serve_image(current_cover_png, "image/png")
        elif self.path == config["served_default_jpeg_filename"]:
            self.serve_image(default_cover_jpeg_bytes, "image/jpeg")
        elif self.path == config["served_default_png_filename"]:
            self.serve_image(default_cover_png_bytes, "image/png")
        else:
            self.send_error(404)

    def serve_image(self, data, content_type):
        if not data: return self.send_error(404)
        self.send_response(200)
        self.send_header('Content-type', content_type)
        self.send_header('Content-length', len(data))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, format, *args): return

def run_asyncio_loop(loop):
    """A simple target for the asyncio thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()

def main():
    load_config()
    setup_logging()
    load_default_cover()

    # Setup the dedicated asyncio event loop and thread
    loop = asyncio.new_event_loop()
    asyncio_thread = threading.Thread(target=run_asyncio_loop, args=(loop,), daemon=True)
    asyncio_thread.start()
    logger.info("Asyncio event loop thread started.")
    
    # Pass the running loop to the MQTT client via userdata
    mqtt_userdata = {"loop": loop}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata=mqtt_userdata)
    client.username_pw_set(config.get("mqtt_username"), config.get("mqtt_password"))
    client.on_connect = on_connect
    client.on_message = on_message
    
    try:
        client.connect(config['mqtt_broker'], config['mqtt_port'], 60)
        client.loop_start() # Use non-blocking loop
        
        httpd = HTTPServer(('', config["http_port"]), CoverImageHandler)
        logger.info(f"HTTP server running on port {config['http_port']}")
        httpd.serve_forever()

    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        # Clean shutdown
        if 'httpd' in locals() and httpd: httpd.server_close()
        if 'client' in locals() and client.is_connected(): client.loop_stop()
        loop.call_soon_threadsafe(loop.stop)
        asyncio_thread.join()

if __name__ == "__main__":
    main()
