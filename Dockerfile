FROM python:3.12-slim

# Copy the script into the container
COPY scripts /scripts

# Install required Python packages
RUN pip install --no-cache-dir requests

# Set the working directory
WORKDIR /scripts

# Set the entrypoint to the script
ENTRYPOINT ["python", "/scripts/deploy_arcane.py"]
