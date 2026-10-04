#!/bin/bash
# Restart the RTI bridge when running under systemd (see deploy/systemd/).
set -e
SERVICE=rti-ad8x-bridge.service

echo "Reloading systemd units..."
sudo systemctl daemon-reload

echo "Restarting $SERVICE..."
sudo systemctl restart "$SERVICE"

echo
systemctl status "$SERVICE" --no-pager
