"""Config flow for Claude Code Assist."""

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_TOKEN, CONF_URL, DEFAULT_URL, DOMAIN, TOKEN_FILE


class ClaudeCodeAgentConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            token = user_input.get(CONF_TOKEN) or await self.hass.async_add_executor_job(self._read_token)
            if not token:
                errors["base"] = "no_token"
            else:
                try:
                    session = async_get_clientsession(self.hass)
                    async with session.get(f"{url}/health", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        resp.raise_for_status()
                except (aiohttp.ClientError, TimeoutError):
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(url)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title="Claude Code", data={CONF_URL: url, CONF_TOKEN: token})

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=DEFAULT_URL): str,
                    vol.Optional(CONF_TOKEN): str,
                }
            ),
            errors=errors,
        )

    def _read_token(self) -> str | None:
        try:
            with open(self.hass.config.path(TOKEN_FILE)) as f:
                return f.read().strip() or None
        except OSError:
            return None
