"""Tattelecom Intercom custom integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    CONF_TOKEN,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_PHONE,
    CONF_STREAM_TYPES,
    DEFAULT_CALL_DELAY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLEEP,
    DEFAULT_STREAM_TYPES,
    DEFAULT_TIMEOUT,
    DOMAIN,
    OPTION_IS_FROM_FLOW,
    PLATFORMS,
    UPDATE_LISTENER,
    UPDATER,
)
from .helper import get_config_value
from .updater import IntercomUpdater

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up entry configured via user interface.

    :param hass: HomeAssistant: Home Assistant object
    :param entry: ConfigEntry: Config Entry object
    :return bool: Is success
    """

    is_new: bool = get_config_value(entry, OPTION_IS_FROM_FLOW, False)

    if is_new:
        hass.config_entries.async_update_entry(entry, data=entry.data, options={})

    _updater: IntercomUpdater = IntercomUpdater(
        hass,
        get_config_value(entry, CONF_PHONE),
        get_config_value(entry, CONF_TOKEN),
        get_config_value(entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        get_config_value(entry, CONF_TIMEOUT, DEFAULT_TIMEOUT),
        get_config_value(entry, CONF_STREAM_TYPES, DEFAULT_STREAM_TYPES),
    )

    hass.data.setdefault(DOMAIN, {})

    # Сохраняем флаг загрузки для каждой платформы
    loaded_platforms = {platform: False for platform in PLATFORMS}

    hass.data[DOMAIN][entry.entry_id] = {
        UPDATER: _updater,
        "loaded_platforms": loaded_platforms,
        "is_loaded": False
    }

    hass.data[DOMAIN][entry.entry_id][UPDATE_LISTENER] = entry.add_update_listener(
        async_update_options
    )

    async def async_start(with_sleep: bool = False) -> None:
        """Async start."""
        try:
            _LOGGER.debug("Starting refresh for entry %s (is_new=%s, state=%s)",
                         entry.entry_id, is_new, entry.state)
            if is_new and entry.state is ConfigEntryState.SETUP_IN_PROGRESS:
                # Новая запись в состоянии SETUP_IN_PROGRESS: используем async_config_entry_first_refresh
                try:
                    await _updater.async_config_entry_first_refresh()
                except HomeAssistantError as err:
                    _LOGGER.warning("Cannot perform first refresh: %s. Proceeding with async_refresh.", err)
                    await _updater.async_refresh()
            else:
                # Существующая запись или состояние не SETUP_IN_PROGRESS: просто обновляем данные
                await _updater.async_refresh()
            
            _LOGGER.debug("Refresh completed for entry %s, data available: %s",
                         entry.entry_id, bool(_updater.data))
            
            if _updater.data:
                _LOGGER.debug("Data keys for entry %s: %s",
                             entry.entry_id, list(_updater.data.keys()))
            else:
                _LOGGER.warning("No data after refresh for entry %s", entry.entry_id)

            # Логируем количество интеркомов
            _LOGGER.debug("Intercoms count: %d", len(_updater.intercoms))
            for intercom_id, intercom in _updater.intercoms.items():
                _LOGGER.debug("Intercom %s: %s", intercom_id, intercom.name)

            if with_sleep:
                await asyncio.sleep(DEFAULT_SLEEP)

            # Загружаем все платформы по одной
            for platform in PLATFORMS:
                try:
                    _LOGGER.debug("Loading platform %s for entry %s", platform, entry.entry_id)
                    await hass.config_entries.async_forward_entry_setups(entry, [platform])
                    # Отмечаем платформу как загруженную
                    hass.data[DOMAIN][entry.entry_id]["loaded_platforms"][platform] = True
                    _LOGGER.debug("Successfully loaded platform %s for entry %s",
                                 platform, entry.entry_id)
                except (HomeAssistantError, asyncio.TimeoutError, ImportError, ValueError) as err:
                    _LOGGER.error("Failed to load platform %s for entry %s: %s",
                                 platform, entry.entry_id, err)

            # Отмечаем всю интеграцию как загруженную
            hass.data[DOMAIN][entry.entry_id]["is_loaded"] = True
            _LOGGER.debug("Integration fully loaded for entry %s", entry.entry_id)

        except (HomeAssistantError, asyncio.TimeoutError, ImportError, ValueError) as err:
            _LOGGER.error("Error during async_start for entry %s: %s", entry.entry_id, err)

    if is_new:
        _LOGGER.debug("New integration, starting immediately")
        await async_start()
        await asyncio.sleep(DEFAULT_SLEEP)
    else:
        _LOGGER.debug("Existing integration, starting with delay")
        hass.async_create_task(async_start(True))

    async def async_stop(event: Event) -> None:
        """Async stop"""
        _LOGGER.debug("Stopping updater for entry %s", entry.entry_id)
        await _updater.async_stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_stop)

    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options for entry that was configured via user interface.

    :param hass: HomeAssistant: Home Assistant object
    :param entry: ConfigEntry: Config Entry object
    """

    if entry.entry_id not in hass.data[DOMAIN]:
        return

    _LOGGER.info("Updating options for entry %s: new options %s", entry.entry_id, entry.options)
    _LOGGER.debug("Updating options for entry %s, triggering reload", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove entry configured via user interface.

    :param hass: HomeAssistant: Home Assistant object
    :param entry: ConfigEntry: Config Entry object
    :return bool: Is success
    """

    # Проверяем, есть ли данные для этого entry
    if entry.entry_id not in hass.data.get(DOMAIN, {}):
        _LOGGER.debug("Entry %s not found in hass.data, nothing to unload", entry.entry_id)
        return True

    try:
        entry_data = hass.data[DOMAIN][entry.entry_id]

        # Проверяем, была ли интеграция загружена
        if not entry_data.get("is_loaded", False):
            _LOGGER.debug("Entry %s was never fully loaded, cleaning up data only", entry.entry_id)

            # Останавливаем updater
            if UPDATER in entry_data:
                _updater: IntercomUpdater = entry_data[UPDATER]
                await _updater.async_stop()

            # Удаляем слушатель обновлений
            if UPDATE_LISTENER in entry_data:
                _update_listener: CALLBACK_TYPE = entry_data[UPDATE_LISTENER]
                _update_listener()

            # Удаляем данные entry
            hass.data[DOMAIN].pop(entry.entry_id)
            return True

        # Выгружаем все платформы, которые могут быть загружены
        # Используем PLATFORMS, чтобы гарантировать выгрузку всех сущностей
        platforms_to_unload = list(PLATFORMS)
        _LOGGER.debug("Unloading all platforms for entry %s: %s", entry.entry_id, platforms_to_unload)

        # Логируем все сущности, связанные с этой записью конфигурации, перед выгрузкой
        entity_registry = er.async_get(hass)
        all_entities = [
            entity
            for entity in entity_registry.entities.values()
            if entity.config_entry_id == entry.entry_id
        ]
        _LOGGER.debug("Total entities for entry %s: %d", entry.entry_id, len(all_entities))
        for entity in all_entities:
            _LOGGER.debug("Entity %s: platform=%s, unique_id=%s, entity_id=%s, config_entry_id=%s",
                         entity.entity_id, entity.platform, entity.unique_id, entity.entity_id, entity.config_entry_id)

        # Выгружаем платформы
        unload_result = True
        if platforms_to_unload:
            for platform in platforms_to_unload:
                try:
                    _LOGGER.debug("Unloading platform %s for entry %s", platform, entry.entry_id)
                    # Логируем количество сущностей перед выгрузкой
                    entities = [
                        entity
                        for entity in entity_registry.entities.values()
                        if entity.config_entry_id == entry.entry_id and entity.platform == platform
                    ]
                    _LOGGER.debug("Found %d entities for platform %s", len(entities), platform)
                    await hass.config_entries.async_forward_entry_unload(entry, platform)
                    _LOGGER.debug("Successfully unloaded platform %s for entry %s",
                                 platform, entry.entry_id)
                    # Проверяем, что сущности удалились
                    entities_after = [
                        entity
                        for entity in entity_registry.entities.values()
                        if entity.config_entry_id == entry.entry_id and entity.platform == platform
                    ]
                    if entities_after:
                        _LOGGER.debug("Platform %s unload did not remove all entities: %s",
                                        platform, [e.entity_id for e in entities_after])
                        # Удаляем оставшиеся сущности вручную
                        for entity in entities_after:
                            try:
                                entity_registry.async_remove(entity.entity_id)
                                _LOGGER.debug("Manually removed entity %s", entity.entity_id)
                            except (HomeAssistantError, ValueError, KeyError) as err:
                                _LOGGER.debug("Failed to manually remove entity %s: %s",
                                                entity.entity_id, err)
                except (HomeAssistantError, asyncio.TimeoutError, ImportError, ValueError) as err:
                    _LOGGER.error("Error unloading platform %s for entry %s: %s",
                                 platform, entry.entry_id, err)
                    unload_result = False

        # После выгрузки платформ проверяем, не осталось ли сущностей
        remaining_entities = [
            entity
            for entity in entity_registry.entities.values()
            if entity.config_entry_id == entry.entry_id
        ]
        if remaining_entities:
            # Логируем только количество, если сущностей много, иначе список
            if len(remaining_entities) <= 5:
                _LOGGER.info(
                    "After unloading platforms, %d entities still remain for entry %s: %s",
                    len(remaining_entities), entry.entry_id,
                    [e.entity_id for e in remaining_entities]
                )
            else:
                _LOGGER.info(
                    "After unloading platforms, %d entities still remain for entry %s",
                    len(remaining_entities), entry.entry_id
                )
            # Удаляем их вручную
            for entity in remaining_entities:
                # Проверяем, существует ли сущность в реестре
                if entity_registry.async_get(entity.entity_id) is not None:
                    try:
                        entity_registry.async_remove(entity.entity_id)
                        _LOGGER.debug("Manually removed leftover entity %s from registry", entity.entity_id)
                    except (HomeAssistantError, ValueError, KeyError) as err:
                        _LOGGER.debug("Failed to manually remove leftover entity %s from registry: %s",
                                        entity.entity_id, err)
                else:
                    _LOGGER.debug("Entity %s already removed from registry", entity.entity_id)
                # Также удаляем из состояния на всякий случай
                try:
                    hass.states.async_remove(entity.entity_id)
                    _LOGGER.debug("Manually removed leftover entity %s from state", entity.entity_id)
                except (HomeAssistantError, ValueError, KeyError) as err:
                    _LOGGER.debug("Failed to manually remove leftover entity %s from state: %s",
                                    entity.entity_id, err)
            
            # Даём время на обработку удаления
            await hass.async_block_till_done()
            
            # Проверяем, остались ли сущности после удаления
            still_remaining = [
                entity
                for entity in entity_registry.entities.values()
                if entity.config_entry_id == entry.entry_id
            ]
            if still_remaining:
                _LOGGER.error(
                    "After manual removal, %d entities still remain for entry %s: %s",
                    len(still_remaining), entry.entry_id,
                    [e.entity_id for e in still_remaining]
                )
            else:
                _LOGGER.info("All leftover entities successfully removed for entry %s", entry.entry_id)

        # Останавливаем updater
        if UPDATER in entry_data:
            _updater = entry_data[UPDATER]
            await _updater.async_stop()

        # Удаляем слушатель обновлений
        if UPDATE_LISTENER in entry_data:
            _update_listener = entry_data[UPDATE_LISTENER]
            _update_listener()

        # Удаляем данные entry
        hass.data[DOMAIN].pop(entry.entry_id)

        _LOGGER.debug("Successfully unloaded entry %s", entry.entry_id)
        return unload_result

    except (HomeAssistantError, asyncio.TimeoutError, ImportError, ValueError) as err:
        _LOGGER.error("Error unloading entry %s: %s", entry.entry_id, err)
        # В случае ошибки всё равно пытаемся удалить данные
        if entry.entry_id in hass.data.get(DOMAIN, {}):
            hass.data[DOMAIN].pop(entry.entry_id)
        return False