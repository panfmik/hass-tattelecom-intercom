"""Switch component."""

from __future__ import annotations

import logging
from typing import Any, Final

from homeassistant.components.switch import (
    SwitchEntity,
    SwitchEntityDescription,
    ENTITY_ID_FORMAT as SWITCH_ENTITY_ID_FORMAT,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_MUTE, SIGNAL_NEW_INTERCOM
from .entity import IntercomEntity
from .exceptions import IntercomError
from .updater import IntercomEntityDescription, IntercomUpdater, async_get_updater

PARALLEL_UPDATES = 0

ICONS: Final = {
    STATE_ON: "mdi:bell-off",
    STATE_OFF: "mdi:bell",
}

_LOGGER = logging.getLogger(__name__)


# Удален класс IntercomSwitchDescription, используем SwitchEntityDescription напрямую


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tattelecom intercom switch entry."""
    updater: IntercomUpdater = async_get_updater(hass, config_entry.entry_id)

    @callback
    def add_switch(entity: IntercomEntityDescription) -> None:
        """Add switch."""
        switch_description = SwitchEntityDescription(
            key=entity.key,
            name=f"Без звука {entity.name}",
            device_class=None,
            entity_category=EntityCategory.CONFIG,
            icon=ICONS[STATE_OFF],
            translation_key="mute",
            translation_placeholders={"intercom_name": entity.name},
        )

        async_add_entities(
            [
                IntercomSwitch(
                    f"{config_entry.entry_id}-switch-{entity.id}",
                    switch_description,
                    updater,
                    entity.id,  # Передаем id отдельно
                )
            ]
        )

    for intercom in updater.intercoms.values():
        add_switch(intercom)

    updater.new_intercom_callbacks.append(
        async_dispatcher_connect(hass, SIGNAL_NEW_INTERCOM, add_switch)
    )


class IntercomSwitch(IntercomEntity, SwitchEntity):
    """Intercom switch."""

    def __init__(
        self,
        unique_id: str,
        description: SwitchEntityDescription,
        updater: IntercomUpdater,
        intercom_id: str | None = None,  # Добавляем параметр для хранения ID
    ) -> None:
        """Initialize switch."""
        # Передаем entity_id_format в родительский класс
        IntercomEntity.__init__(self, unique_id, description, updater, SWITCH_ENTITY_ID_FORMAT)
        
        self._intercom_id = intercom_id
        mute_key = f"{description.key}_{ATTR_MUTE}"
        # Проверяем, что данные updater не None
        if updater.data is None:
            _LOGGER.debug("Switch %s: updater data is None, defaulting to off", unique_id)
            self._attr_is_on = False
        else:
            self._attr_is_on = bool(updater.data.get(mute_key, False))
        self._attr_should_poll = False

    @property
    def device_class(self) -> str | None:
        """Return device class of the entity."""
        return None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._updater.last_update_success

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        try:
            if self._intercom_id:
                await self._updater.client.mute(int(self._intercom_id))
                self._attr_is_on = True
                mute_key = f"{self._intercom_id}_{ATTR_MUTE}"
                self._updater.update_data(mute_key, True)
                self._attr_icon = ICONS[STATE_ON]
        except IntercomError as err:
            _LOGGER.error("Failed to turn on: %s", err)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        try:
            if self._intercom_id:
                await self._updater.client.unmute(int(self._intercom_id))
                self._attr_is_on = False
                mute_key = f"{self._intercom_id}_{ATTR_MUTE}"
                self._updater.update_data(mute_key, False)
                self._attr_icon = ICONS[STATE_OFF]
        except IntercomError as err:
            _LOGGER.error("Failed to turn off: %s", err)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._updater.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._intercom_id and self._updater.data is not None:
            mute_key = f"{self._intercom_id}_{ATTR_MUTE}"
            is_on = bool(self._updater.data.get(mute_key, False))
            if self._attr_is_on != is_on:
                self._attr_is_on = is_on
                self._attr_icon = ICONS[STATE_ON if is_on else STATE_OFF]
                self.async_write_ha_state()
        else:
            _LOGGER.debug("Switch %s: updater data is None, skipping update", self.entity_id)