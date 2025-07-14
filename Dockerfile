# Use a specific, stable version of the official Python runtime
FROM python:3.9-slim-buster

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file first to leverage Docker's build cache.
# The following RUN command will only be re-executed if this file changes.
COPY requirements.txt .

# Install any needed packages from the requirements file
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files, including the critical config.yaml
COPY . .

# Expose the HTTP port defined in your config.yaml (e.g., 82)
# This is for documentation; you'll map the port in the 'docker run' command.
EXPOSE 82

# Run the application when the container launches
CMD ["python", "cover_server.py"]
