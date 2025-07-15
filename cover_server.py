"""
A script that listens for media updates from a HomePod (pyatv) or an MQTT
topic, processes the album art, and serves it via a local HTTP server,
with an optional ESPHome trigger.

This script combines the pyatv listener logic with an HTTP server and
image processing pipeline. The data source is determined by the config.yaml file.
"""
import asyncio
import io
import logging
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import paho.mqtt.client as mqtt
import pyatv
import yaml
from aioesphomeapi import APIClient
from PIL import Image
from pyatv import exceptions
from pyatv.interface import Playing

# --- Global variables ---
config = {}
current_cover_jpeg = None
current_cover_png = None
default_cover_jpeg_bytes = None
default_cover_png_bytes = None
logger = logging.getLogger(__name__)


# --- Configuration and Setup ---
def load_config():
    """Loads configuration from YAML."""
    global config
    try:
        with open("config.yaml", "r") as f:
            config = yaml.safe_load(f)
        logger.info("Configuration loaded.")
    except Exception as e:
        logger.critical(f"Failed to load configuration: {e}")
        exit(1)


def setup_logging():
    """Sets up logging."""
    log_level = config.get("log_level", "INFO").upper()
    logging.basicConfig(
        level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )


# --- Image Processing ---
def process_image(img):
    """Resizes and converts image to RGB."""
    img_rgb = img.convert("RGB")
    return img_rgb.resize(tuple(config["image_size"]), Image.LANCZOS)


def img_to_jpeg_bytes(img):
    """Converts PIL image to JPEG bytes."""
    with io.BytesIO() as byte_arr:
        img.save(byte_arr, format="JPEG", quality=config["jpeg_quality"])
        return byte_arr.getvalue()


def img_to_lossy_png_bytes(img):
    """Converts PIL image to PNG bytes."""
    with io.BytesIO() as byte_arr:
        img.save(byte_arr, format="PNG", optimize=True)
        return byte_arr.getvalue()


def load_default_cover():
    """Loads and processes the default cover images."""
    global current_cover_jpeg, current_cover_png, default_cover_jpeg_bytes, default_cover_png_bytes
    try:
        with Image.open(config["default_cover_jpeg"]) as img:
            processed_img = process_image(img)
            default_cover_jpeg_bytes = img_to_jpeg_bytes(processed_img)
            current_cover_jpeg = default_cover_jpeg_bytes
            logger.info("Loaded default JPEG cover.")
        with Image.open(config["default_cover_png"]) as img:
            processed_img = process_image(img)
            default_cover_png_bytes = img_to_lossy_png_bytes(processed_img)
            current_cover_png = default_cover_png_bytes
            logger.info("Loaded default PNG cover.")
    except FileNotFoundError as e:
        logger.warning(f"Default cover image not found: {e}")
    except Exception as e:
        logger.error(f"Error loading default cover: {e}")


# --- Asynchronous Logic (ESPHome and pyatv) ---
async def trigger_esphome_action():
    """Connects and calls a user-defined action on an ESPHome device."""
    esphome_config = config.get("esphome", {})
    device_ip = esphome_config.get("device_ip")
    action_name = esphome_config.get("action_name")

    if not device_ip or not action_name:
        return

    logger.info(f"Executing ESPHome action '{action_name}' for {device_ip}")
    cli = APIClient(device_ip, 6053, esphome_config.get("api_password"))
    try:
        await cli.connect(login=True)
        _, services = await cli.list_entities_services()
        service_to_call = next((s for s in services if s.name == action_name), None)

        if service_to_call:
            await cli.execute_service(service_to_call, data={})
            logger.info(f"Successfully executed action '{action_name}'.")
        else:
            logger.error(f"Action '{action_name}' not found on device.")
    except Exception as e:
        logger.error(f"Error during ESPHome action: {e}")
    finally:
        await cli.disconnect()


class HomePodListener(pyatv.interface.PushListener):
    """A listener class to handle push updates from a device."""

    def __init__(self, atv_instance):
        self.atv = atv_instance
        self.last_printed_title = None
        self._connection_lost_event = asyncio.Event()

    def connection_lost(self, exception: Exception) -> None:
        _LOGGER.warning("Connection lost to device: %s", exception)
        self._connection_lost_event.set()

    async def _fetch_and_process_artwork(self, playstatus: Playing):
        global current_cover_jpeg, current_cover_png
        for attempt in range(5):
            try:
                artwork = await self.atv.metadata.artwork()
                if artwork:
                    with Image.open(io.BytesIO(artwork.bytes)) as img:
                        processed_img = process_image(img)
                        current_cover_jpeg = img_to_jpeg_bytes(processed_img)
                        current_cover_png = img_to_lossy_png_bytes(processed_img)
                    logger.info(f"Artwork found for {playstatus.title}. Updating current cover image.")
                    await trigger_esphome_action()
                    return
                else:
                    logger.info(f"No artwork available for {playstatus.title}.")
                    return
            except exceptions.BlockedStateError:
                delay = 0.05 * (2**attempt)
                _LOGGER.warning(
                    f"Metadata blocked on attempt {attempt + 1}. Retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
            except Exception as e:
                _LOGGER.error(f"Failed to fetch or process artwork: {e}")
                return
        _LOGGER.error(f"Failed to fetch artwork for {playstatus.title} after retries.")

    def playstatus_update(self, updater, playstatus: Playing) -> None:
        global current_cover_jpeg, current_cover_png
        title = playstatus.title or "No Title"
        state = playstatus.device_state.name

        if title == self.last_printed_title and state == "Playing":
            return
        self.last_printed_title = title if state == "Playing" else None

        if state == "Playing":
            asyncio.create_task(self._fetch_and_process_artwork(playstatus))
        else:
            if current_cover_jpeg != default_cover_jpeg_bytes:
                logger.info("Reverting to default cover image.")
                current_cover_jpeg = default_cover_jpeg_bytes
                current_cover_png = default_cover_png_bytes

    def playstatus_error(self, updater, exception: Exception) -> None:
        logger.error(f"An error occurred during push update: {exception}")

    async def wait_for_disconnect(self):
        await self._connection_lost_event.wait()


async def pyatv_loop():
    """Main pyatv connection and listening loop."""
    loop = asyncio.get_event_loop()
    atv = None
    is_shutting_down = False
    while not is_shutting_down:
        try:
            device_name = config["homepod"]["name"]
            logger.info(f"Searching for '{device_name}' on the network...")
            found = await pyatv.scan(loop, timeout=10)
            conf = next((dev for dev in found if dev.name == device_name), None)

            if not conf:
                logger.warning(f"Could not find '{device_name}'. Retrying in 20s...")
                await asyncio.sleep(20)
                continue

            logger.info(f"Found '{device_name}'. Connecting...")
            atv = await pyatv.connect(conf, loop)
            listener = HomePodListener(atv)
            atv.push_updater.listener = listener
            atv.push_updater.start()
            logger.info("✅ Connection successful. Listening for push updates...")

            initial_status = await atv.metadata.playing()
            listener.playstatus_update(None, initial_status)

            await listener.wait_for_disconnect()
        except asyncio.CancelledError:
            logger.info("pyatv_loop received cancellation request.")
            is_shutting_down = True
        except Exception as e:
            logger.error(f"An error occurred in the pyatv loop: {e}")
        finally:
            if atv:
                atv.close()
            if not is_shutting_down:
                logger.info("Connection closed. Reconnecting in 10s...")
                await asyncio.sleep(10)


# --- Synchronous MQTT Logic ---
def on_connect_mqtt(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info("Connected to MQTT broker.")
        client.subscribe(config["mqtt"]["topic_cover"])
        # Also subscribe to availability topic if defined
        if config["mqtt"].get("topic_availability"):
            client.subscribe(config["mqtt"]["topic_availability"])
            logger.info(f"Subscribed to availability topic: {config['mqtt']['topic_availability']}")
    else:
        logger.error(f"MQTT connection failed with code {rc}")


def on_message_mqtt(client, userdata, msg):
    """Handles new artwork from an MQTT message."""
    global current_cover_jpeg, current_cover_png
    loop = userdata["loop"]
    payload = msg.payload.decode('utf-8')
    mqtt_config = config["mqtt"]

    try:
        # Handle availability topic
        if mqtt_config.get("topic_availability") and msg.topic == mqtt_config["topic_availability"]:
            if payload == mqtt_config.get("payload_not_available"):
                if current_cover_jpeg != default_cover_jpeg_bytes:
                    logger.info("Device not available. Reverting to default cover.")
                    current_cover_jpeg = default_cover_jpeg_bytes
                    current_cover_png = default_cover_png_bytes
            elif payload == mqtt_config.get("payload_available"):
                 logger.info("Device is available.")
            return

        # Handle cover art topic
        if msg.topic == mqtt_config["topic_cover"]:
            with Image.open(io.BytesIO(msg.payload)) as img:
                processed_img = process_image(img)
                current_cover_jpeg = img_to_jpeg_bytes(processed_img)
                current_cover_png = img_to_lossy_png_bytes(processed_img)
            logger.info("Updated cover image from MQTT.")
            asyncio.run_coroutine_threadsafe(trigger_esphome_action(), loop)

    except Exception as e:
        logger.error(f"Error processing MQTT message: {e}")


def start_mqtt_client(loop):
    """Initializes and starts the MQTT client."""
    mqtt_config = config["mqtt"]
    mqtt_userdata = {"loop": loop}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata=mqtt_userdata)
    client.username_pw_set(mqtt_config.get("username"), mqtt_config.get("password"))
    client.on_connect = on_connect_mqtt
    client.on_message = on_message_mqtt

    try:
        client.connect(mqtt_config["broker"], mqtt_config["port"], 60)
        client.loop_start()
        logger.info("MQTT client started.")
        return client
    except Exception as e:
        logger.critical(f"Failed to start MQTT client: {e}")
        exit(1)


# --- Synchronous HTTP Server ---
class CoverImageHandler(BaseHTTPRequestHandler):
    """HTTP request handler for serving all cover images."""

    def do_GET(self):
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
        if not data:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-type", content_type)
        self.send_header("Content-length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        return


# --- Main Execution ---
async def cancel_all_tasks(loop):
    """Gracefully cancel all running tasks."""
    logger.info("Cancelling outstanding asyncio tasks.")
    tasks = [t for t in asyncio.all_tasks(loop=loop) if t is not asyncio.current_task(loop=loop)]
    for task in tasks:
        task.cancel()
    
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("All asyncio tasks cancelled.")


def run_asyncio_loop(loop):
    """Target for the asyncio thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main():
    """Main entry point."""
    load_config()
    setup_logging()
    load_default_cover()

    use_homepod = "homepod" in config
    use_mqtt = "mqtt" in config

    if use_homepod and use_mqtt:
        logger.critical(
            "Config error: Both 'homepod' and 'mqtt' sources defined. Use only one."
        )
        exit(1)

    if not use_homepod and not use_mqtt:
        logger.critical(
            "Config error: No data source. Add a 'homepod' or 'mqtt' block."
        )
        exit(1)

    loop = asyncio.new_event_loop()
    client = None

    # This thread will just run the event loop forever
    asyncio_thread = threading.Thread(
        target=run_asyncio_loop, args=(loop,), daemon=True
    )
    asyncio_thread.start()
    logger.info("Asyncio event loop thread started.")

    if use_homepod:
        logger.info("Starting in HomePod mode.")
        # Schedule the main async logic to run on the loop
        asyncio.run_coroutine_threadsafe(pyatv_loop(), loop)
    else:  # use_mqtt
        logger.info("Starting in MQTT mode.")
        client = start_mqtt_client(loop)

    httpd = None
    try:
        httpd = HTTPServer(("", config["http_port"]), CoverImageHandler)
        logger.info(f"HTTP server running on port {config['http_port']}")
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        if httpd:
            httpd.server_close()
        if use_mqtt and client and client.is_connected():
            client.loop_stop()
        
        # Gracefully stop the asyncio loop
        if loop.is_running():
            # Step 1: Cancel all running tasks
            future = asyncio.run_coroutine_threadsafe(cancel_all_tasks(loop), loop)
            future.result() # Wait for cancellation to complete

            # Step 2: Stop the loop
            loop.call_soon_threadsafe(loop.stop)

        asyncio_thread.join()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
