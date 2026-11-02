"""Claude Code Assist: use headless Claude Code as a conversation agent."""

import asyncio

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_TOKEN, CONF_URL, DOMAIN

PLATFORMS = [Platform.CONVERSATION]
SERVICE_RUN_TASK = "run_task"
EVENT_FINISHED = "claude_code_assist_task_finished"

RUN_TASK_SCHEMA = vol.Schema(
    {
        vol.Required("prompt"): cv.string,
        vol.Optional("run_id"): cv.string,
        vol.Optional("model"): cv.string,
        vol.Optional("wait", default=False): cv.boolean,
        vol.Optional("timeout", default=1800): vol.All(vol.Coerce(int), vol.Range(min=10, max=14400)),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def run_task(call: ServiceCall) -> ServiceResponse:
        """Start a task in Claude Code; optionally wait for its result."""
        current = hass.config_entries.async_loaded_entries(DOMAIN)
        if not current:
            raise HomeAssistantError("Claude Code Assist is not set up")
        data = current[0].data
        body = {"prompt": call.data["prompt"], "run_id": call.data.get("run_id", ""), "model": call.data.get("model", "")}
        wait = call.data["wait"] and call.return_response
        finished: asyncio.Future = hass.loop.create_future()
        run_id_box: dict = {}

        @callback
        def _on_finished(event: Event) -> None:
            if event.data.get("run_id") == run_id_box.get("id") and not finished.done():
                finished.set_result(dict(event.data))

        unsub = hass.bus.async_listen(EVENT_FINISHED, _on_finished) if wait else None
        try:
            session = async_get_clientsession(hass)
            try:
                async with session.post(
                    f"{data[CONF_URL]}/task", json=body,
                    headers={"Authorization": f"Bearer {data[CONF_TOKEN]}"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    reply = await resp.json(content_type=None)
                    if resp.status >= 400:
                        raise HomeAssistantError(f"Claude Code Assist: {reply.get('error', resp.status)}")
            except (aiohttp.ClientError, TimeoutError) as err:
                raise HomeAssistantError(f"Cannot reach the Claude Code Assist app: {err}") from err
            run_id_box["id"] = reply["run_id"]
            if not wait:
                return {"run_id": reply["run_id"]} if call.return_response else None
            try:
                return await asyncio.wait_for(finished, call.data["timeout"])
            except TimeoutError:
                return {"run_id": reply["run_id"], "success": False, "timed_out": True, "result": ""}
        finally:
            if unsub:
                unsub()

    if not hass.services.has_service(DOMAIN, SERVICE_RUN_TASK):
        hass.services.async_register(
            DOMAIN, SERVICE_RUN_TASK, run_task, schema=RUN_TASK_SCHEMA, supports_response=SupportsResponse.OPTIONAL
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok and len(hass.config_entries.async_loaded_entries(DOMAIN)) <= 1:
        hass.services.async_remove(DOMAIN, SERVICE_RUN_TASK)
    return ok
