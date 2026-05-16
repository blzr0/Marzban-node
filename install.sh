#!/bin/bash

# Запускаем официальный установщик
sudo bash -c "$(curl -sL https://github.com/Gozargah/Marzban-scripts/raw/master/marzban-node.sh)" @ install

# Подменяем образ на наш форк
sed -i 's|gozargah/marzban-node:latest|ghcr.io/blzr0/marzban-node:latest|g' /opt/marzban-node/docker-compose.yml

# Перезапускаем с новым образом
docker-compose -f /opt/marzban-node/docker-compose.yml pull
docker-compose -f /opt/marzban-node/docker-compose.yml up -d

echo "Done! Node is running with patched image."
