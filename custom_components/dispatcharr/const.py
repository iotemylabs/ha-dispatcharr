"""Constants for the Dispatcharr integration."""
from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "dispatcharr"

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.MEDIA_PLAYER]

CONF_HOST = "host"
CONF_PORT = "port"
CONF_SSL = "ssl"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_API_KEY = "api_key"
CONF_AUTH_METHOD = "auth_method"

AUTH_METHOD_API_KEY = "api_key"
AUTH_METHOD_CREDENTIALS = "credentials"

DEFAULT_PORT = 9191
UPDATE_INTERVAL = timedelta(seconds=30)

MANUFACTURER = "Dispatcharr"
