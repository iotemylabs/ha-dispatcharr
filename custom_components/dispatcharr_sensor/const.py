"""Constants for the Dispatcharr integration."""
from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "dispatcharr_sensor"

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.MEDIA_PLAYER]

CONF_HOST = "host"
CONF_PORT = "port"
CONF_SSL = "ssl"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

DEFAULT_PORT = 9191
UPDATE_INTERVAL = timedelta(seconds=30)
