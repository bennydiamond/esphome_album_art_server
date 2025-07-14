# MQTT to HTTP/ESPHome Cover Art Server

This Python application acts as a bridge for [Shairport Sync](https://github.com/mikebrady/shairport-sync) to display album art on devices like an ESPHome-powered display. It listens for image data published by Shairport Sync to an MQTT topic, processes the image, and serves it via a built-in HTTP server.

It can also send a refresh command to a designated ESPHome device using its native API, telling it to download the new artwork.

## Features

- **MQTT Integration**: Subscribes to an MQTT topic to receive album art dynamically from Shairport Sync.
- **On-the-Fly Image Processing**: Resizes incoming images to a configurable size using the Pillow library.
- **ESPHome Optimized**: Pre-converts images to the correct size and format before serving. This offloads all image processing from the microcontroller, saving valuable CPU cycles and memory on the ESP device.
- **Flash-Friendly Operation**: The entire process runs in-memory. No temporary files are written to the hard drive or SD card, preventing unnecessary wear on flash storage.
- **HTTP Server**: Serves the most recent cover art as both a `.jpg` and `.png` file.
- **Static Fallback Hosting**: Always serves a default cover image from a static URL, which can be used as a fallback on the client device.
- **Optional ESPHome Integration**: Can send a `component.update` command to an ESPHome device via its native API to trigger an immediate screen refresh.
- **Dockerized**: Includes a `Dockerfile` for easy, containerized deployment.

## Requirements

- Python 3.9+
- Docker (recommended for deployment)
- A running instance of [Shairport Sync](https://github.com/mikebrady/shairport-sync).
- An MQTT broker.
- An ESPHome device with the `api` component enabled (optional, for the refresh trigger).

## Setup

### 1. Shairport Sync Configuration

For this script to work, you need to have a shairport-sync executable compiled with mqtt support (--with-mqtt configure flag, not sure for --with-metadata).
Then, you must configure your `shairport-sync.conf` file to publish artwork to your MQTT broker.

Find the `mqtt` section in your `shairport-sync.conf` and ensure the following options are set:

```
mqtt ={
  enabled = "yes";
  hostname = "192.168.1.100"; // IP of your MQTT Broker
  port = 1883;
  publish_artwork = "yes";    // This is essential
  topic = "shairport-sync/artwork"; // This must match 'mqtt_topic_cover' in your config.yaml
  username = "your_mqtt_user";
  password = "your_mqtt_password";
};
```
After editing, restart the Shairport Sync service.

### 2. Application Configuration File

Create a `config.yaml` file in the root of the project directory. You can use the provided `config.yaml-template` as a starting point.

```bash
cp config.yaml-template config.yaml
```

Now, edit config.yaml with your specific details, making sure the mqtt_broker and mqtt_topic_cover match your Shairport Sync settings.

### 3. ESPHome Device Setup (Optional)

If you want the script to trigger a refresh on your ESPHome device, you must add the following to that device's YAML configuration:# In your ESPHome device's configuration file (e.g., hmi-garage.yaml)

```yaml
api:
  # Set a password here if you want, and update config.yaml accordingly
  password: ""
  actions:
    # This defines the action the Python script will call.
    # The name must match 'action_name' in your config.yaml.
    - action: refresh_now_playing_art
      then:
        # This tells the 'online_image' component to re-download its source.
        - component.update: id_online_img_now_playing
```

Make sure the action_name in your config.yaml matches the action name in your ESPHome device's configuration.

## Usage

### Using Docker Compose (Easiest Method)

1. Create a docker-compose.yml file in your project directory with the following content:

```yaml
version: '3.8'
services:
  cover-server:
    build: .
    container_name: cover-server
    restart: unless-stopped
    ports:
      - "82:82" # Maps port 82 on your host to port 82 in the container
    volumes:
      - ./config.yaml:/app/config.yaml
```

(Adjust the `ports` section to match the `http_port` in your `config.yaml` if you change it.)

2. Run the container in the background:

``` bash
docker-compose up -d
```

3. View logs:

```bash
docker-compose logs -f
```

4. Stop the service:

```bash
docker-compose down
```

### Using Docker (Manual Method)

1. Build the Docker image:

```bash
docker build -t cover-server .
```

2. Run the container:

This command runs the container in the background, maps the port, and mounts your local config.yaml file.

```bash
docker run -d -p 82:82 --rm \
-v "$(pwd)/config.yaml:/app/config.yaml" \
--name my-cover-server \
cover-server
```

(Adjust the port `-p 82:82` to match the `http_port` in your config if you change it.)

3. View Logs:

``` bash
docker logs -f my-cover-server
```

### Running Locally with Python:

1. Install dependencies:

``` bash
pip install -r requirements.txt
```

2. Run the script:

```bash
python cover_server.py
```



## File Structure.

```
├── cover_server.py         # The main Python application
├── Dockerfile              # For building the Docker container
├── requirements.txt        # Python dependencies
├── config.yaml-template    # Template for your configuration
├── default_cover.jpg       # Default fallback image
└── default_cover.png       # Default fallback image
```
