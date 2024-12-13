# Set your new location here
NEW_DOCKER_PATH="/mnt/HC_Volume_101776654" 


# Stop Docker service
sudo systemctl stop docker
sudo systemctl stop docker.socket

# Create new directory
sudo mkdir -p $NEW_DOCKER_PATH

# Copy data to new location
sudo rsync -aP /var/lib/docker/ $NEW_DOCKER_PATH/

# Backup old directory
sudo mv /var/lib/docker /var/lib/docker.old

# Create/modify daemon.json
sudo mkdir -p /etc/docker
sudo bash -c "cat > /etc/docker/daemon.json << EOL
{
    \"data-root\": \"$NEW_DOCKER_PATH\"
}
EOL"

# Start Docker service
sudo systemctl start docker
sudo systemctl start docker.socket

# Optional: Remove old directory after confirming everything works
sudo rm -rf /var/lib/docker.old

docker info | grep "Docker Root Dir"