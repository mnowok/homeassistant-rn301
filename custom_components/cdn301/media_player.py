import logging
import xml.etree.ElementTree as ET
from collections import namedtuple
from defusedxml import cElementTree
import xml
import time
from datetime import datetime

from typing import Optional

import voluptuous as vol
import requests

from homeassistant.components.media_player import (
    MediaPlayerEntity, PLATFORM_SCHEMA)

from homeassistant.components.media_player.const import (
    MEDIA_TYPE_MUSIC, MEDIA_TYPE_PLAYLIST, MEDIA_TYPE_CHANNEL, SUPPORT_NEXT_TRACK, SUPPORT_PAUSE, SUPPORT_PLAY, SUPPORT_PLAY_MEDIA, SUPPORT_PREVIOUS_TRACK,
    SUPPORT_SELECT_SOURCE, SUPPORT_STOP, SUPPORT_TURN_OFF, SUPPORT_TURN_ON, SUPPORT_VOLUME_MUTE, SUPPORT_VOLUME_SET,
    SUPPORT_SHUFFLE_SET)
from homeassistant.const import (
    CONF_HOST, CONF_NAME, STATE_OFF, STATE_IDLE, STATE_PLAYING, STATE_UNKNOWN)

import homeassistant.util.dt as dt_util
import homeassistant.helpers.config_validation as cv

DOMAIN = 'cdn301'

ATTR_ENABLED = 'enabled'
ATTR_PORT = 'port'
DATA_YAMAHA = 'yamaha_known_receivers'
DEFAULT_NAME = 'Yamaha CD-N301'
DEFAULT_TIMEOUT = 5
BASE_URL = 'http://{0}/YamahaRemoteControl/ctrl'

SERVICE_ENABLE_OUTPUT = 'yamaha_enable_output'
SUPPORT_YAMAHA = SUPPORT_TURN_ON | SUPPORT_TURN_OFF | \
                 SUPPORT_SELECT_SOURCE | SUPPORT_PLAY | SUPPORT_PLAY_MEDIA | SUPPORT_PAUSE | SUPPORT_STOP | \
                 SUPPORT_NEXT_TRACK | SUPPORT_PREVIOUS_TRACK | SUPPORT_SHUFFLE_SET

SUPPORTED_PLAYBACK = SUPPORT_TURN_ON | SUPPORT_TURN_OFF | \
                     SUPPORT_SELECT_SOURCE | SUPPORT_SHUFFLE_SET

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Required(CONF_HOST): cv.string
})
SOURCE_MAPPING = {
    'Optical': 'OPTICAL',
    'CD': 'CD',
    'Spotify': 'Spotify',
    'Net Radio': 'NET RADIO',
    'Server': 'SERVER'
}

_LOGGER = logging.getLogger(__name__)


def setup_platform(hass, config, add_devices, discovery_info=None):
    devices = []
    device = YamahaCdn301MP(config.get(CONF_NAME), config.get(CONF_HOST))
    devices.append(device)
    add_devices(devices)


class YamahaCdn301MP(MediaPlayerEntity):
    MenuStatus = namedtuple("MenuStatus", "ready layer name current_line max_line current_list")
    ListGet = "<{src_name}><List_Info>GetParam</List_Info></{src_name}>"
    SelectNetRadioLine = "<Player><List_Control><Direct_Sel>Line_{lineno}</Direct_Sel></List_Control></Player>"

    def __init__(self, name, host):
        self._data = None
        self._name = name
        self._host = host
        self._base_url = BASE_URL.format(self._host)
        self._pwstate = STATE_UNKNOWN
        self._volume = 0
        self._muted = False
        self._is_on = None
        self._current_state = -1
        self._current_operation = ''
        self._set_state = None
        self._source = None
        self._device_source = None
        self._source_list = list(SOURCE_MAPPING.keys())
        self._reverse_mapping = {val: key for key, val in SOURCE_MAPPING.items()}
        self._operation_list = ['On', 'Vol']

        self._media_meta = {}
        self._media_playing = False
        self._media_play_position = None
        self._media_play_position_updated = None
        self._media_play_shuffle = None
        self._media_play_repeat = None
        self._media_play_artist = None
        self._media_play_album = None
        self._media_play_song = None
        self._media_playback_state = None
        _LOGGER.debug("Init called")

    def update(self) -> None:
        data = self._do_api_get("<System><Basic_Status>GetParam</Basic_Status></System>")
        tree = ET.fromstring(data)
        for node in tree[0][0]:
            if node.tag == "Power_Control":
                self._pwstate = STATE_IDLE if (node[0].text) == "On" else STATE_OFF
            elif node.tag == "Volume":
                for voln in node:
                    if voln.tag == "Lvl":
                        self._volume = int(voln.find("Val").text) / 50
                    elif voln.tag == "Mute":
                        self._muted = voln.text == "On"
            elif node.tag == "Input":

                txt = node.find("Input_Sel").text
                self._source = self._reverse_mapping[txt]
                self._device_source = txt.replace(" ", "_")
        if self._pwstate != STATE_OFF:
            self._update_media_playing()

    @property
    def state(self):
        return self._pwstate

    @property
    def supported_features(self):
        if self._source in ("Optical", "Line 1", "Line 2", "Line 3", "Tuner"):
            return SUPPORTED_PLAYBACK
        return SUPPORT_YAMAHA

    @property
    def volume_level(self):
        return self._volume

    @property
    def source(self):
        return self._source

    @property
    def source_list(self):
        return self._source_list

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_volume_muted(self) -> bool:
        return self._muted

    @property
    def media_position(self):
        """Duration of current playing media"""
        return self._media_play_position

    @property
    def media_position_updated_at(self):
        """Duration of current playing media"""
        return self._media_play_position_updated

    @property
    def media_title(self):
        """Title of currently playing track"""
        if "song" in self._media_meta and "frequency" in self._media_meta:
            return self._media_meta["song"] if datetime.now().second < 20 else self._media_meta["frequency"]
        elif "song" in self._media_meta:
            return self._media_meta.get("song")
        elif "frequency" in self._media_meta:
            return self._media_meta.get("frequency")

    @property
    def media_album(self):
        """Album of currently playing track"""
        return self._media_meta.get('album')

    @property
    def media_artist(self) -> Optional[str]:
        """Artist of currently playing track"""
        return self._media_meta.get('artist')

    @property
    def media_content_type(self):
        if self._source == "Net Radio" or self._source == "Tuner":
            return MEDIA_TYPE_CHANNEL
        return MEDIA_TYPE_PLAYLIST

    @property
    def shuffle(self):
        return self._media_play_shuffle

    def set_shuffle(self, shuffle):
        self._media_play_control("Shuffle")

    def turn_on(self):
        """Turn on the amplifier"""
        self._set_power_state(True)

    def turn_off(self):
        """Turn off the amplifier"""
        self._set_power_state(False)

    def set_volume_level(self, volume):
        self._do_api_put(
            '<Main_Zone><Volume><Lvl><Val>{0}</Val><Exp>0</Exp><Unit></Unit></Lvl></Volume></Main_Zone>'.format(
                int(volume * 50)))

    def select_source(self, source):
        self._do_api_put(
            '<System><Input><Input_Sel>{0}</Input_Sel></Input></System>'.format(SOURCE_MAPPING[source]))

    def mute_volume(self, mute):
        self._do_api_put('<System><Volume><Mute>{0}</Mute></Volume></System>'.format('On' if mute else 'Off'))
        self._muted = mute

    def _media_play_control(self, command):
        self._do_api_put(
			'<{0}><Play_Control><Playback>{1}</Playback></Play_Control></{0}>'.format("Player", command))
		

    def media_play(self):
        """Play media"""
        self._media_play_control("Play")

    def media_pause(self):
        """Play media"""
        self._media_play_control("Pause")

    def media_stop(self):
        """Play media"""
        self._media_play_control("Stop")

    def media_next_track(self):
        self._media_play_control("Next")

    def media_previous_track(self):
        self._media_play_control("Prev")
		
    def play_media(self, media_type, media_id, **kwargs):
        """Play media from an ID.
        This exposes a pass through for various input sources in the
        Yamaha to direct play certain kinds of media. media_type is
        treated as the input type that we are setting, and media id is
        specific to it.
        For the NET RADIO mediatype the format for ``media_id`` is a
        "path" in your vtuner hierarchy. For instance:
        ``Bookmarks>Internet>Radio Paradise``. The separators are
        ``>`` and the parts of this are navigated by name behind the
        scenes. There is a looping construct built into the yamaha
        library to do this with a fallback timeout if the vtuner
        service is unresponsive.
        NOTE: this might take a while, because the only API interface
        for setting the net radio station emulates button pressing and
        navigating through the net radio menu hierarchy. And each sub
        menu must be fetched by the receiver from the vtuner service.
        """
        
        if media_type == "NET RADIO":
            self.net_radio(media_id)		

    def _set_power_state(self, on):
        self._do_api_put(
            '<System><Power_Control><Power>{0}</Power></Power_Control></System>'.format("On" if on else "Standby"))

    def _do_api_request(self, data) -> str:
        data = '<?xml version="1.0" encoding="utf-8"?>' + data
        req = requests.post(self._base_url, data=data, timeout=DEFAULT_TIMEOUT)
        if req.status_code != 200:
            _LOGGER.exception("Error doing API request, %d, %s", req.status_code, data)
        else:
            _LOGGER.debug("API request ok %d", req.status_code)
        return req.text
		
    def _do_api_request_xml(self, data):
        data = '<?xml version="1.0" encoding="utf-8"?>' + data
        req = requests.post(self._base_url, data=data, timeout=DEFAULT_TIMEOUT)
        if req.status_code != 200:
            _LOGGER.exception("Error doing API request, %d, %s", req.status_code, data)
        else:
            _LOGGER.debug("API request ok %d", req.status_code)
        return req		

    def _do_api_get(self, data) -> str:
        request = '<YAMAHA_AV cmd="GET">' + data + '</YAMAHA_AV>'
        _LOGGER.debug("Request:")
        _LOGGER.debug(request)
        response = self._do_api_request(request)
        _LOGGER.debug("Response:")
        _LOGGER.debug(response)
        return response

    def _do_api_put(self, data) -> str:
        _LOGGER.debug("_do_api_put: %s", data)
        data = '<YAMAHA_AV cmd="PUT">' + data + '</YAMAHA_AV>'
        return self._do_api_request(data)

    def _nullify_media_fields(self) -> None:
        """Set media fields to null as we don't require them on certain channels"""
        self._media_meta = {}
        self._media_playing = False
        self._pwstate = STATE_IDLE if self._pwstate != STATE_OFF else STATE_OFF

    def _set_playback_info(self, text: str) -> None:
        """Set the playback info from xml"""
        if text == "Play" or text == "Assert":
            self._pwstate = STATE_PLAYING if self._pwstate != STATE_OFF else STATE_OFF
            self._media_playing = True
        elif text == "Pause":
            self._pwstate = STATE_IDLE if self._pwstate != STATE_OFF else STATE_OFF
            self._media_playing = True
        else:
            self._media_playing = False



    def _update_media_playing(self):
        media_meta_mapping = {
            'Artist': 'artist',
            'Station': 'song',
            'Radio_Text_A': 'song',
            'Album': 'album',
            'Song': 'song',
            'Track': 'song',
        }
        device_mapping = {
            "Spotify": "Player",
            "NET_RADIO": "Player",
            "SERVER": "Player",
            "TUNER": "Player",
            "CD": "Player"
        }

        try:
            if self._device_source in device_mapping:
                data = self._do_api_get(
                    "<{0}><Play_Info>GetParam</Play_Info></{0}>".format(device_mapping[self._device_source]))
                self._media_meta = {}
                tree = ET.fromstring(data)
                for node in tree[0][0]:
                    try:
                        if node.tag == "Play_Mode":
                            self._media_play_repeat = node.text == "On"
                            self._media_play_shuffle = node.text == "On"
                        elif node.tag == "Play_Time":
                            self._media_play_position = int(node.text)
                            self._media_play_position_updated = dt_util.utcnow()
                        elif node.tag == "Meta_Info":
                            for meta in node:
                                if meta.tag in media_meta_mapping and meta.text:
                                    self._media_meta[media_meta_mapping[meta.tag]] = meta.text.replace('&amp;', '&')
                        elif node.tag == "Playback_Info":
                            self._set_playback_info(node.text)
                        elif node.tag == "Signal_Info":
                            node.find("Tuned")
                            self._set_playback_info(node.find("Tuned").text)
                        elif node.tag == "Tuning":
                            band = node.find("Band").text
                            current = node.find("Freq").find("Current")
                            val = float(current.find("Val").text) / 100
                            unit = current.find("Unit").text
                            self._media_meta["frequency"] = f"{band} {val} {unit}"
                    except Exception as e:
                        _LOGGER.warning(e)

                _LOGGER.debug("_media_meta")
                _LOGGER.debug(self._media_meta)
            else:
                self._nullify_media_fields()
        except:
            _LOGGER.exception(data)

    def _do_api_get_xml(self, data):
        request = '<YAMAHA_AV cmd="GET">' + data + '</YAMAHA_AV>'
        _LOGGER.debug("Request XML:")
        _LOGGER.debug(request)
        try:
            res = self._do_api_request_xml(request)
            response = cElementTree.XML(res.content)
            if response.get("RC") != "0":
                logger.error("Request %s failed with %s",
                             request_text, res.content)
                raise ResponseException(res.content)
            return response
        except xml.etree.ElementTree.ParseError:
            logger.exception("Invalid XML returned for request %s: %s",
                             request_text, res.content)
            raise
        return response

    def _direct_sel(self, lineno):
        global SelectNetRadioLine

        request_text = YamahaCdn301MP.SelectNetRadioLine.format(lineno=lineno)
        return self._do_api_put(request_text)

    def menu_status(self):
        #cur_input = self.input
        #src_name = self._src_name(cur_input)
        #if not src_name:
        #    raise MenuUnavailable(cur_input)

        request_text = YamahaCdn301MP.ListGet.format(src_name="Player")
        _LOGGER.debug(request_text)
        res = self._do_api_get_xml(request_text)

        ready = (next(res.iter("Menu_Status")).text == "Ready")
        layer = int(next(res.iter("Menu_Layer")).text)
        name = next(res.iter("Menu_Name")).text
        current_line = int(next(res.iter("Current_Line")).text)
        max_line = int(next(res.iter("Max_Line")).text)
        current_list = next(res.iter('Current_List'))

        cl = {
            elt.tag: elt.find('Txt').text
            for elt in current_list.getchildren()
            if elt.find('Attribute').text != 'Unselectable'
        }

        status = YamahaCdn301MP.MenuStatus(ready, layer, name, current_line, max_line, cl)
        return status


    def net_radio(self, path):
        """Play net radio at the specified path.
        This lets you play a NET_RADIO address in a single command
        with by encoding it with > as separators. For instance:
            Bookmarks>Internet>Radio Paradise
        It does this by push commands, then looping and making sure
        the menu is in a ready state before we try to push the next
        one. A sufficient number of iterations are allowed for to
        ensure we give it time to get there.
        TODO: better error handling if we some how time out
        """
        _LOGGER.debug("net_radio")
        
        layers = path.split(">")
        #self.input = "NET RADIO"
        self.select_source("Net Radio")
        
        self._do_api_put("<Player><List_Control><Cursor>Return to Home</Cursor></List_Control></Player>")

        for attempt in range(20):
            menu = self.menu_status()
            if menu.ready:
                for line, value in menu.current_list.items():
                    if value == layers[menu.layer - 1]:
                        lineno = line[5:]
                        self._direct_sel(lineno)
                        if menu.layer == len(layers):
                            return
                        break
            else:
                # print("Sleeping because we are not ready yet")
                time.sleep(1)
