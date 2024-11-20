"""The linkytic integration serial reader."""

from __future__ import annotations

from collections.abc import Callable
import logging
import threading
import time

import serial
import serial.serialutil

from homeassistant.core import callback

from .const import (
    BYTESIZE,
    CONSTRUCTORS_CODES,
    DEVICE_TYPES,
    DID_CONSTRUCTOR,
    DID_CONSTRUCTOR_CODE,
    DID_REGNUMBER,
    DID_TYPE,
    DID_TYPE_CODE,
    DID_YEAR,
    FRAME_END,
    LINE_END,
    LINKY_IO_ERRORS,
    MODE_HISTORIC_BAUD_RATE,
    MODE_HISTORIC_FIELD_SEPARATOR,
    MODE_STANDARD_BAUD_RATE,
    MODE_STANDARD_FIELD_SEPARATOR,
    PARITY,
    SHORT_FRAME_DETECTION_TAGS,
    SHORT_FRAME_FORCED_UPDATE_TAGS,
    STOPBITS,
)

_LOGGER = logging.getLogger(__name__)


class LinkyTICReader(threading.Thread):
    """Implements the reading of a serial Linky TIC."""

    def __init__(
        self,
        title: str,
        port: str,
        std_mode: bool,
        producer_mode: bool,
        three_phase: bool,
        real_time: bool,
    ) -> None:
        """Initialize the reader thread."""
        super().__init__(name=f"LinkyTIC_{title}")
        self._port = port
        self._std_mode = std_mode
        self._producer_mode = producer_mode
        self._three_phase = three_phase
        self._real_time = real_time
        self._stopevent = threading.Event()
        self._callbacks: dict[str, list[Callable[[bool], None]]] = {}
        self._reader = None
        self.serial_number = None
        self.device_identification = {}
        # Start with an empty dict
        self._last_values: dict[str, str] = {}

    async def _open_serial(self):
        """Open the serial connection."""
        try:
            self._reader = await hass.async_add_executor_job(
                lambda: serial.serial_for_url(
                    url=self._port,
                    baudrate=MODE_STANDARD_BAUD_RATE if self._std_mode else MODE_HISTORIC_BAUD_RATE,
                    bytesize=BYTESIZE,
                    parity=PARITY,
                    stopbits=STOPBITS,
                    timeout=1,
                )
            )
        except LINKY_IO_ERRORS as e:
            _LOGGER.error("Failed to open serial port %s: %s", self._port, e)
            raise

    def get_values(self, tag) -> tuple[str | None, str | None]:
        """Get tag value and timestamp from the thread memory cache."""
        if not self.is_connected:
            return None, None
        try:
            payload = self._values[tag]
            return payload["value"], payload["timestamp"]
        except KeyError:
            return None, None

    @property
    def has_read_full_frame(self) -> bool:
        """Use to known if at least one complete frame has been read on the serial connection."""
        return self._frames_read >= 1

    @property
    def is_connected(self) -> bool:
        """Use to know if the reader is actually connected to a serial connection."""
        if self._reader is None:
            return False
        return self._reader.is_open

    @property
    def serial_number(self) -> str | None:
        """Returns meter serial number (ADSC or ADCO tag)."""
        return self._serial_number

    @property
    def port(self) -> str:
        """Returns serial port."""
        return self._port

    def run(self):
        """Continuously read the the serial connection and extract TIC values."""
        while not self._stopevent.is_set():
            # Reader should have been opened.
            assert self._reader is not None
            if not self._reader.is_open:
                # NOTE: implement a maximum retry, and go in failure mode if the connection can't be renewed?
                try:
                    self._reader.open()
                except LINKY_IO_ERRORS:
                    time.sleep(5)  # Cooldown to prevent spamming logs.
                    _LOGGER.warning("Could not open port")
                finally:
                    continue
            try:
                line = self._reader.readline()
            except LINKY_IO_ERRORS as exc:
                _LOGGER.error(
                    "Error while reading serial device %s: %s. Will retry in 5s",
                    self._port,
                    exc,
                )
                self._reset_state()
                self._reader.close()
                continue
                
            # Parse the line if non empty (prevent errors from read timeout that returns empty byte string)
            if not line:
                continue
            tag = self._parse_line(line)
            if tag is not None:
                # Mark this tag as seen for end of frame cache cleanup
                self._tags_seen.append(tag)
                # Handle short burst for tri-phase historic mode
                if (
                    not self._std_mode
                    and self._three_phase
                    and not self._within_short_frame
                    and tag in SHORT_FRAME_DETECTION_TAGS
                ):
                    _LOGGER.warning(
                        "Short trame burst detected (%s): switching to forced update mode",
                        tag,
                    )
                    self._within_short_frame = True
                # If we have a notification callback for this tag, call it
                try:
                    notif_callback = self._notif_callbacks[tag]
                    _LOGGER.debug("We have a notification callback for %s: executing", tag)
                    forced_update = self._realtime
                    # Special case for forced_update: historic tree-phase short frame
                    if self._within_short_frame and tag in SHORT_FRAME_FORCED_UPDATE_TAGS:
                        forced_update = True
                    # Special case for forced_update: historic single-phase ADPS
                    if tag == "ADPS":
                        forced_update = True
                    notif_callback(forced_update)
                except KeyError:
                    pass
            # Handle frame end
            if FRAME_END in line:
                if self._within_short_frame:
                    # burst / short frame (exceptional)
                    self._within_short_frame = False
                else:
                    # regular long frame
                    self._frames_read += 1
                    self._cleanup_cache()
                if tag is not None:
                    _LOGGER.debug("End of frame, last tag read: %s", tag)
        # Stop flag as been activated
        _LOGGER.info("Thread stop: closing the serial connection")
        if self._reader:
            self._reader.close()

    def register_push_notif(self, tag: str, notif_callback: Callable[[bool], None]):
        """Call to register a callback notification when a certain tag is parsed."""
        _LOGGER.debug("Registering a callback for %s tag", tag)
        self._notif_callbacks[tag] = notif_callback

    @callback
    def signalstop(self, event):
        """Activate the stop flag in order to stop the thread from within."""
        if self.is_alive():
            _LOGGER.info("Stopping %s serial thread reader (received %s)", self._title, event)
            self._stopsignal = True

    def update_options(self, real_time: bool):
        """Setter to update serial reader options."""
        _LOGGER.debug("%s: new real time option value: %s", self._title, real_time)
        self._realtime = real_time

    def _cleanup_cache(self):
        """Call to cleanup the data cache to allow some sensors to get back to undefined/unavailable if they are not present in the last frame."""
        for cached_tag in list(self._values.keys()):  # pylint: disable=consider-using-dict-items,consider-iterating-dictionary
            if cached_tag not in self._tags_seen:
                _LOGGER.debug(
                    "tag %s was present in cache but has not been seen in previous frame: removing from cache",
                    cached_tag,
                )
                # Clean serial controller data cache for this tag
                del self._values[cached_tag]
                # Inform entity of a new value available (None) if in push mode
                try:
                    notif_callback = self._notif_callbacks[cached_tag]
                    notif_callback(self._realtime)
                except KeyError:
                    pass
        self._tags_seen = []

    def _parse_line(self, line) -> str | None:
        """Parse a line when a full line has been read from serial. It parses it as Linky TIC infos, validate its checksum and save internally the line infos."""
        # there is a great chance that the first line is a partial line: skip it
        if self._first_line:
            _LOGGER.debug("skipping first line: %s", repr(line))
            self._first_line = False
            return None
        # if not, it should be complete: parse it !
        _LOGGER.debug("line to parse: %s", repr(line))
        # cleanup the line
        line = line.rstrip(LINE_END).rstrip(FRAME_END)
        if not line:
            return None
        # extract the fields by parsing the line given the mode
        timestamp = None
        if self._std_mode:
            fields = line.split(MODE_STANDARD_FIELD_SEPARATOR)
            if len(fields) == 4:
                tag = fields[0]
                timestamp = fields[1]
                field_value = fields[2]
                checksum = fields[3]
            elif len(fields) == 3:
                tag = fields[0]
                field_value = fields[1]
                checksum = fields[2]
            else:
                _LOGGER.error(
                    "Failed to parse the following line (%d fields detected) in standard mode: %s",
                    len(fields),
                    repr(line),
                )
                return None
        else:
            fields = line.split(MODE_HISTORIC_FIELD_SEPARATOR)
            if len(fields) == 3:
                tag = fields[0]
                field_value = fields[1]
                checksum = fields[2]
            elif len(fields) == 4:
                # checksum has the same value as field separator, leading to 4 fields with the last 2 empty
                tag = fields[0]
                field_value = fields[1]
                checksum = MODE_HISTORIC_FIELD_SEPARATOR
            else:
                _LOGGER.error(
                    "Failed to parse the following line (%d fields detected) in historic mode: %s",
                    len(fields),
                    repr(line),
                )
                return None
        # validate the checksum
        if not checksum:
            _LOGGER.error("Empty checksum on line '%s'", repr(line))
            return None
        try:
            self._validate_checksum(tag, timestamp, field_value, checksum)
        except InvalidChecksum as invalid_checksum:
            _LOGGER.error(
                "Failed to validate the checksum of line '%s': %s",
                repr(line),
                invalid_checksum,
            )
            return None
        _LOGGER.debug("line checksum is valid")
        # transform and store the values
        payload: dict[str, str | None] = {"value": field_value.decode("ascii")}
        payload["timestamp"] = timestamp.decode("ascii") if timestamp else None
        tag = tag.decode("ascii")
        self._values[tag] = payload
        _LOGGER.debug("read the following values: %s -> %s", tag, repr(payload))
        # Parse ADS for device identification if necessary
        if (self._std_mode and tag == "ADSC") or (not self._std_mode and tag == "ADCO"):
            self.parse_ads(payload["value"])
        return tag

    def _validate_checksum(self, tag: bytes, timestamp: bytes | None, value: bytes, checksum: bytes):
        # rebuild the frame
        if self._std_mode:
            sep = MODE_STANDARD_FIELD_SEPARATOR
            if timestamp is None:
                frame = tag + sep + value + sep
            else:
                frame = tag + sep + timestamp + sep + value + sep
        else:
            frame = tag + MODE_HISTORIC_FIELD_SEPARATOR + value
        # compute the sum of the frame
        sum1 = 0
        for byte in frame:
            sum1 += byte
        # compute checksum for s1
        truncated = sum1 & 0x3F
        computed_checksum = truncated + 0x20
        # validate
        try:
            if computed_checksum != ord(checksum):
                raise InvalidChecksum(tag, timestamp, value, sum1, truncated, computed_checksum, checksum)
        except TypeError as exc:
            # see https://github.com/hekmon/linkytic/issues/9
            _LOGGER.exception("Encountered an unexpected checksum (%s): %s", exc, checksum)
            raise InvalidChecksum(
                tag,
                timestamp,
                value,
                sum1,
                truncated,
                computed_checksum,
                bytes("0", encoding="ascii"),  # fake expected checksum to avoid type error on ord()
            ) from exc

    def parse_ads(self, ads):
        """Extract information contained in the ADS as EURIDIS."""
        _LOGGER.debug(
            "%s: parsing ADS: %s",
            self._title,
            ads,
        )
        if len(ads) != 12:
            _LOGGER.error(
                "%s: ADS should be 12 char long, actually %d cannot parse: %s",
                self._title,
                len(ads),
                ads,
            )
            return

        # Because S/N is a device identifier, only parse it once.
        if self.serial_number:
            return

        # Save serial number
        self._serial_number = ads

        # let's parse ADS as EURIDIS
        device_identification = {DID_YEAR: ads[2:4], DID_REGNUMBER: ads[6:]}
        # # Parse constructor code
        device_identification[DID_CONSTRUCTOR_CODE] = ads[0:2]
        try:
            device_identification[DID_CONSTRUCTOR] = CONSTRUCTORS_CODES[device_identification[DID_CONSTRUCTOR_CODE]]
        except KeyError:
            _LOGGER.warning(
                "%s: constructor code is unknown: %s",
                self._title,
                device_identification[DID_CONSTRUCTOR_CODE],
            )
            device_identification[DID_CONSTRUCTOR] = None
        # # Parse device type code
        device_identification[DID_TYPE_CODE] = ads[4:6]
        try:
            device_identification[DID_TYPE] = f"{DEVICE_TYPES[device_identification[DID_TYPE_CODE]]}"
        except KeyError:
            _LOGGER.warning("%s: ADS device type is unknown: %s", self._title, device_identification[DID_TYPE_CODE])
            device_identification[DID_TYPE] = None
        # # Update device infos
        self.device_identification = device_identification
        # Parsing done
        _LOGGER.debug("%s: parsed ADS: %s", self._title, repr(self.device_identification))


class InvalidChecksum(Exception):
    """Exception for Linky TIC checksum validation error."""

    def __init__(
        self,
        tag: bytes,
        timestamp: bytes | None,
        value: bytes,
        s1: int,
        s1_truncated: int,
        computed: int,
        expected: bytes,
    ) -> None:
        """Initialize the checksum exception."""
        try:
            self.tag = tag.decode("ascii")
        except UnicodeDecodeError:
            self.tag = "<invalid ascii sequence>"
        try:
            self.timestamp = timestamp.decode("ascii") if timestamp else None
        except UnicodeDecodeError:
            self.timestamp = "<invalid ascii sequence>"
        try:
            self.value = value.decode("ascii")
        except UnicodeDecodeError:
            self.value = "<invalid ascii sequence>"
        self.sum1 = s1
        self.s1_truncated = s1_truncated
        self.computed = computed
        self.expected = expected
        super().__init__(self.msg())

    def msg(self):
        """Printable exception method."""
        return "{} -> {} ({}) | s1 {} {} | truncated {} {} {} | computed {} {} {} | expected {} {} {}".format(
            self.tag,
            self.value,
            self.timestamp,
            self.sum1,
            bin(self.sum1),
            self.s1_truncated,
            bin(self.s1_truncated),
            chr(self.s1_truncated),
            self.computed,
            bin(self.computed),
            chr(self.computed),
            int.from_bytes(self.expected, byteorder="big"),
            bin(int.from_bytes(self.expected, byteorder="big")),
            chr(ord(self.expected)),
        )


async def linky_tic_tester(hass, device: str, std_mode: bool) -> None:
    """Before starting the thread, this method can help validate configuration by opening the serial communication and read a line."""
    try:
        # Exécuter l'opération bloquante dans un thread séparé
        serial_reader = await hass.async_add_executor_job(
            lambda: serial.serial_for_url(
                url=device,
                baudrate=MODE_STANDARD_BAUD_RATE if std_mode else MODE_HISTORIC_BAUD_RATE,
                bytesize=BYTESIZE,
                parity=PARITY,
                stopbits=STOPBITS,
                timeout=1,
            )
        )
    except serial.serialutil.SerialException as exc:
        raise CannotConnect(f"Unable to connect to the serial device {device}: {exc}") from exc

    # Try to read a line
    try:
        await hass.async_add_executor_job(serial_reader.readline)
    except serial.serialutil.SerialException as exc:
        await hass.async_add_executor_job(serial_reader.close)
        raise CannotRead(f"Failed to read a line: {exc}") from exc

    # All good
    await hass.async_add_executor_job(serial_reader.close)


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""

    def __init__(self, message) -> None:
        """Initialize the CannotConnect error with an explanation message."""
        super().__init__(message)


class CannotRead(Exception):
    """Error to indicate that the serial connection was open successfully but an error occurred while reading a line."""

    def __init__(self, message) -> None:
        """Initialize the CannotRead error with an explanation message."""
        super().__init__(message)
