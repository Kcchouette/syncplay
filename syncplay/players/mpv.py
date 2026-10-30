# coding:utf8
import os
import random
import re
import sys
import time
import subprocess
import threading

from syncplay import constants
from syncplay.messages import getMessage
from syncplay.players.basePlayer import BasePlayer
from syncplay.players.mpv_status import (
    EVENT_FILE_LOADED,
    EVENT_START_FILE,
    OBSERVED_PROPERTIES,
    MpvStatusCache,
    Signal,
    build_mpv_args,
)
from syncplay.utils import getRuntimeDir, isURL, findResourcePath
from syncplay.utils import isWindows, isASCII
from syncplay.utils import playerPathExists
from syncplay.vendor.python_mpv_jsonipc.python_mpv_jsonipc import MPV

class MpvPlayer(BasePlayer):
    RE_VERSION = re.compile(r'.*mpv v?(\d+)\.(\d+)\.\d+.*')
    osdMessageSeparator = "; "  # TODO: Make conditional
    POSITION_QUERY = 'time-pos'
    OSD_QUERY = 'show_text'
    RE_ANSWER = re.compile(constants.MPLAYER_ANSWER_REGEX)
    lastResetTime = None
    alertOSDSupported = True
    chatOSDSupported = True
    speedSupported = True
    customOpenDialog = False

    @staticmethod
    def run(client, playerPath, filePath, args):
        try:
            ver = MpvPlayer.RE_VERSION.search(subprocess.check_output([playerPath, '--version']).decode('utf-8'))
        except:
            ver = None
        constants.MPV_NEW_VERSION = ver is None or int(ver.group(1)) > 0 or int(ver.group(2)) >= 23
        if not constants.MPV_NEW_VERSION:
            from twisted.internet import reactor
            the_reactor = reactor
            the_reactor.callFromThread(client.ui.showErrorMessage,
                                        "This version of mpv is not compatible with Syncplay. "
                                        "Please use mpv >=0.23.0.", True)
            the_reactor.callFromThread(client.stop)
            return

        constants.MPV_OSC_VISIBILITY_CHANGE_VERSION = False if ver is None else int(ver.group(1)) > 0 or int(ver.group(2)) >= 28
        if not constants.MPV_OSC_VISIBILITY_CHANGE_VERSION:
            client.ui.showDebugMessage(
                "This version of mpv is not known to be compatible with changing the OSC visibility. "
                "Please use mpv >=0.28.0.")
        return MpvPlayer(client, MpvPlayer.getExpandedPath(playerPath), filePath, args)

    @staticmethod
    def getStartupArgs(userArgs):
        return build_mpv_args(
            constants.MPV_ARGS, "script", findResourcePath("syncplayintf.lua"), userArgs
        )

    @staticmethod
    def getDefaultPlayerPathsList():
        l = []
        for path in constants.MPV_PATHS:
            p = MpvPlayer.getExpandedPath(path)
            if p:
                l.append(p)
        return l

    @staticmethod
    def isValidPlayerPath(path):
        if "mpv" in path and "mpvnet" not in path and MpvPlayer.getExpandedPath(path):
            return True
        return False

    @staticmethod
    def getExpandedPath(playerPath):
        if not playerPathExists(playerPath):
            if playerPathExists(playerPath + "mpv.exe"):
                playerPath += "mpv.exe"
                return playerPath
            elif playerPathExists(playerPath + "\\mpv.exe"):
                playerPath += "\\mpv.exe"
                return playerPath
        if os.access(playerPath, os.X_OK):
            return playerPath
        for path in os.environ['PATH'].split(':'):
            path = os.path.join(os.path.realpath(path), playerPath)
            if os.access(path, os.X_OK):
                return path

    @staticmethod
    def getIconPath(path):
        return constants.MPV_ICONPATH

    @staticmethod
    def getPlayerPathErrors(playerPath, filePath):
        return None

    def _setProperty(self, property_, value):
        self._listener.sendLine(["set_property", property_, value])

    def mpvErrorCheck(self, line):
        if "Error parsing option" in line or "Error parsing commandline option" in line:
            self.quitReason = getMessage("mpv-version-error")

        elif "Could not open pipe at '/dev/stdin'" in line:
            self.reactor.callFromThread(self._client.ui.showErrorMessage, getMessage("mpv-version-error"), True)
            self.drop()

        if constants and any(errormsg in line for errormsg in constants.MPV_ERROR_MESSAGES_TO_REPEAT):
            self._client.ui.showErrorMessage(line)

    def displayMessage(self, message, duration=(constants.OSD_DURATION * 1000), OSDType=constants.OSD_NOTIFICATION,
                       mood=constants.MESSAGE_NEUTRAL):
        if not self._client._config["chatOutputEnabled"]:
            messageString = self._sanitizeText(message.replace("\\n", "<NEWLINE>")).replace("<NEWLINE>", "\\n")
            self._listener.mpvpipe.show_text(messageString, duration, constants.MPLAYER_OSD_LEVEL)
            return
        messageString = message.replace("\r", "").replace("\n", "").replace("%", "%%")
        self._listener.sendLine(["script-message-to", "syncplayintf", "{}-osd-{}".format(OSDType, mood), messageString])

    def displayChatMessage(self, username, message):
        if not self._client._config["chatOutputEnabled"]:
            messageString = "<{}> {}".format(username, message)
            messageString = self._sanitizeText(messageString.replace("\\n", "<NEWLINE>")).replace("<NEWLINE>", "\\n")
            duration = int(constants.OSD_DURATION * 1000)
            self._listener.mpvpipe.show_text(messageString, duration, constants.MPLAYER_OSD_LEVEL)
            return
        username = username.replace("\r", "").replace("\n", "").replace("%", "%%")
        message = message.replace("\r", "").replace("\n", "").replace("%", "%%")
        messageString = "<{}> {}".format(username, message)
        self._listener.sendLine(["script-message-to", "syncplayintf", "chat", messageString])

    def setSpeed(self, value):
        self._setProperty('speed', "{:.2f}".format(value))

    def setPaused(self, value):
        if self._cache.snapshot().paused == value:
            self._client.ui.showDebugMessage("Not sending setPaused to mpv as state is already {}".format(value))
            return
        pauseValue = "yes" if value else "no"
        self._setProperty("pause", pauseValue)
        # Command path: store unconditionally; the observer-driven gate does
        # not apply here (the property observer is authoritative for its own
        # samples — this value only bridges until the next change arrives).
        self._cache.force_pause(value)
        if value == False:
            self._cache.stamp_position()

    def eofDetected(self):
        self._client.eofReportedByPlayer()

    # ------------------------------------------------------------------
    # JSON-IPC observers: event-driven status (replaces log scraping of
    # print-text ANS_* / term-playing-msg markers).
    # ------------------------------------------------------------------

    def _wireObservers(self, pipe):
        """Bind property/event observers on a freshly created mpv pipe.

        Called from the listener constructor: issuing observe_property
        commands here is safe (the pipe is fully connected) and must never
        happen from inside an EventHandler callback.
        """
        for prop in OBSERVED_PROPERTIES:
            pipe.bind_property_observer(prop, self._onMpvProperty)
        for event in (EVENT_START_FILE, EVENT_FILE_LOADED):
            pipe.bind_event(event, self._onMpvEvent)
        pipe.bind_event("client-message", self._onClientMessage)

    def _onClientMessage(self, data):
        """Runs on the vendor lib's EventHandler thread.

        Lua → Python transport for chat lines and the script-ready options
        handshake (replaces the old print-text → log scraping).
        """
        args = data.get("args") or []
        if not args:
            return
        if args[0] == "syncplay-chat" and len(args) >= 2:
            self._listener.sendChat(args[1])
        elif args[0] == "syncplay-intf-ready":
            # Beats the setFeatures race when the script loads after launch
            # (IINA's load-script). Hopped onto the reactor: _sendMpvOptions
            # issues synchronous mpvpipe.command() calls, which must never
            # run on the EventHandler thread (single-threaded; a hung
            # command would head-of-line-block chat/EOF/property callbacks
            # for up to the vendor's 120 s timeout).
            self.reactor.callFromThread(self._sendMpvOptions)

    def _onMpvProperty(self, name, data):
        """Runs on the vendor lib's EventHandler thread.

        The cache is updated here; the reactor is only touched for EOF —
        never per time-pos tick, which would flood it.
 """
        if name in ("time-pos", "pause"):
            signals = self._cache.on_property(
                name, data,
                recently_reset=self._recentlyReset(),
                global_position=self._client.getGlobalPosition(),
                global_paused=self._client.getGlobalPaused(),
            )
        else:
            signals = self._cache.on_property(name, data)
        for signal in signals:
            if signal is Signal.EOF:
                self.reactor.callFromThread(self.eofDetected)

    def _onMpvEvent(self, data):
        """Runs on the vendor lib's EventHandler thread."""
        for signal in self._cache.on_event(data.get("event")):
            if signal is Signal.FILE_LOAD_START:
                # Flipped synchronously on this thread (like the old
                # <SyncplayUpdateFile> marker) so the send queue cannot flush
                # a mid-transition command before the reactor wakes up.
                self._client.ui.showDebugMessage("Not ready to send due to start-file")
                self._listener.setReadyToSend(False)
            elif signal is Signal.FILE_LOAD_DONE:
                threading.Thread(
                    target=self._resolveFileLoadFields,
                    args=(self._cache.current_load_generation(),),
                    name="MPV FileLoad", daemon=True,
                ).start()

    def _resolveFileLoadFields(self, generation):
        """Worker thread: fill fields the property observers may have raced.

        Bounded per-field waits keep a hung mpv from stalling the adapter for
        the vendor lib's 120 s command timeout; neither the reactor nor the
        EventHandler thread is ever blocked here.

        ``pause``/``time-pos`` are re-read on purpose: observers only fire on
        *changes*, so the values mpv held right before/at load (e.g. the
        initial ``pause=yes``) would otherwise be lost to the pre-load gate
        and never refresh — which both desynced pause and starved the
        position stamp.

        ``generation`` guards against a newer load starting mid-resolve
        (playlist advance / double-click): every write and the finalize hop
        are skipped if the cache generation moved on, so this file's values
        can never pollute the next file's state.
        """

        def still_current():
            return self._cache.current_load_generation() == generation

        snap = self._cache.snapshot()
        cached = {"filename": snap.filename, "duration": snap.duration, "path": snap.path}
        for prop in ("filename", "duration", "path"):
            if cached[prop] is None:
                value = self._queryProperty(prop, timeout=2.0)
                if value is not None and still_current():
                    self._cache.on_property(prop, value)
        if not still_current():
            return
        pause_value = self._queryProperty("pause", timeout=2.0)
        if pause_value is not None and still_current():
            # Direct read of mpv reality — bypasses the file-loaded gate.
            self._cache.force_pause(bool(pause_value))
        position_value = self._queryProperty("time-pos", timeout=2.0)
        if position_value is not None and still_current():
            self._cache.force_position(float(position_value), stamp=True)
        if still_current():
            self.reactor.callFromThread(self._finalizeFileLoad)

    def _queryProperty(self, name, timeout=2.0):
        pipe = self._listener.mpvpipe
        result = {}
        done = threading.Event()

        def query():
            try:
                result["value"] = pipe.command("get_property", name)
            except Exception as e:
                result["error"] = e
            finally:
                done.set()

        threading.Thread(target=query, name="MPV get_property", daemon=True).start()
        done.wait(timeout)
        return result.get("value")

    def _finalizeFileLoad(self):
        """Reactor thread: run the file-update hook (IINA overrides it),
        then re-open the send gate — same order as the old
        </SyncplayUpdateFile> marker path (the gate flips even when IINA
        skips the placeholder file)."""
        self._onFileUpdate()
        self._listener.setReadyToSend(True)
        self._client.ui.showDebugMessage("Ready to send due to file load")

    def lineReceived(self, line):
        if line:
            self._client.ui.showDebugMessage("player << {}".format(line))
            line = line.replace("[cplayer] ", "")  # -v workaround
            line = line.replace("[term-msg] ", "")  # -v workaround
            line = line.replace("   cplayer: ", "")  # --msg-module workaround
            line = line.replace("  term-msg: ", "")
        match = self.RE_ANSWER.match(line)
        if match:
            name, value = [m for m in match.groups() if m]
            if name.lower() == "exiting":
                # Kept on purpose: the mpv `quit` event carries no reason
                # string, so abnormal exits are only visible in this log line.
                if value != 'Quit':
                    if self.quitReason is None:
                        self.quitReason = getMessage("media-player-error").format(value)
                    self.reactor.callFromThread(self._client.ui.showErrorMessage, self.quitReason, True)
                self.drop()
            # Other ANS_* lines have no senders anymore (status now comes
            # from property observers) — ignore them.
            return
        self._handleUnknownLine(line)

    def askForStatus(self):
        if not self._listener.isReadyForSend:
            self._client.ui.showDebugMessage("mpv not ready for update")
            return

        snap = self._cache.snapshot()
        position, unresponsive = self._cache.calculated_position(
            self._client.getGlobalPosition(), self._recentlyReset())
        if unresponsive and snap.last_position_time is not None:
            diff = time.time() - snap.last_position_time
            self.reactor.callFromThread(
                self._client.ui.showErrorMessage,
                getMessage("mpv-unresponsive-error").format(int(diff)), True)
            self.drop()
        if snap.file_loaded and snap.paused is not None:
            paused = snap.paused
        else:
            paused = self._client.getGlobalPaused()
        self._client.updatePlayerStatus(paused, position)

    def drop(self):
        try:
            self._listener.sendLine(['quit'])
        except AttributeError as e:
            self._client.ui.showDebugMessage("Could not send quit message: {}".format(str(e)))
        self.reactor.callFromThread(self._client.stop, False)

    def _sanitizeText(self, text):
        text = text.replace("\r", "")
        text = text.replace("\n", "")
        text = text.replace("\\\"", "<SYNCPLAY_QUOTE>")
        text = text.replace("\"", "<SYNCPLAY_QUOTE>")
        text = text.replace("%", "%%")
        text = text.replace("\\", "\\\\")
        text = text.replace("{", "\\\\{")
        text = text.replace("}", "\\\\}")
        text = text.replace("<SYNCPLAY_QUOTE>", "\\\"")
        return text

    def _quoteArg(self, arg):
        arg = arg.replace('\\', '\\\\')
        arg = arg.replace("'", "\\'")
        arg = arg.replace('"', '\\"')
        arg = arg.replace("\r", "")
        arg = arg.replace("\n", "")
        return '"{}"'.format(arg)

    def _preparePlayer(self):
        if self.delayedFilePath:
            self.openFile(self.delayedFilePath)
        self.setPaused(True)
        self.reactor.callLater(0, self._client.initPlayer, self)

    def _loadFile(self, filePath):
        self._cache.clear_file()
        self._listener.sendLine(['loadfile', filePath], notReadyAfterThis=True)

    def setFeatures(self, featureList):
        self._sendMpvOptions()

    def setPosition(self, value):
        if value < constants.DO_NOT_RESET_POSITION_THRESHOLD and self._recentlyReset():
            self._client.ui.showDebugMessage(
                "Did not seek as recently reset and {} below 'do not reset position' threshold".format(value))
            return
        self._cache.force_position(value)
        self._client.ui.showDebugMessage(
            "Setting position to {}...".format(self._cache.snapshot().position))
        self._setProperty(self.POSITION_QUERY, "{}".format(value))
        time.sleep(0.03)
        self._cache.stamp_position()

    def openFile(self, filePath, resetPosition=False):
        self._client.ui.showDebugMessage("openFile, resetPosition=={}".format(resetPosition))
        if resetPosition:
            self.lastResetTime = time.time()
            if isURL(filePath):
                self._client.ui.showDebugMessage("Setting additional lastResetTime due to stream")
                self.lastResetTime += constants.STREAM_ADDITIONAL_IGNORE_TIME
        self._loadFile(filePath)
        if self._cache.snapshot().paused != self._client.getGlobalPaused():
            self._client.ui.showDebugMessage("Want to set paused to {}".format(self._client.getGlobalPaused()))
        else:
            self._client.ui.showDebugMessage("Don't want to set paused to {}".format(self._client.getGlobalPaused()))
        if resetPosition == False:
            self._client.ui.showDebugMessage("OpenFile setting position to global position: {}".format(self._client.getGlobalPosition()))
            self.setPosition(self._client.getGlobalPosition())
        else:
            self._cache.record_position(
                0.0, recently_reset=self._recentlyReset(),
                global_position=self._client.getGlobalPosition())
        # TO TRY: self._listener.setReadyToSend(False)

    def _sendMpvOptions(self):
        options = []
        for option in constants.MPV_SYNCPLAYINTF_OPTIONS_TO_SEND:
            options.append("{}={}".format(option, self._client._config[option]))
        for option in constants.MPV_SYNCPLAYINTF_CONSTANTS_TO_SEND:
            options.append(option)
        for option in constants.MPV_SYNCPLAYINTF_LANGUAGE_TO_SEND:
            options.append("{}={}".format(option, getMessage(option)))
        options.append("OscVisibilityChangeCompatible={}".format(constants.MPV_OSC_VISIBILITY_CHANGE_VERSION))
        options_string = ", ".join(options)
        self._listener.sendLine(["script-message-to", "syncplayintf", "set_syncplayintf_options",  options_string])
        self._setOSDPosition()
        socketPath = self._listener.mpv_arguments.get("input-ipc-server")
        if socketPath is not None:
            self._setProperty("input-ipc-server", socketPath)

    def _handleUnknownLine(self, line):
        self.mpvErrorCheck(line)
        if "Playing:" in line:
            # mpv's own early gate (parity with the old marker branch): the
            # file is switching — hold outbound commands and drop file state.
            self._client.ui.showDebugMessage("Not ready to send due to Playing:")
            self._listener.setReadyToSend(False)
            self._cache.clear_file()

        elif "Failed" in line or "failed" in line or "No video or audio streams selected" in line or "error" in line:
            self._client.ui.showDebugMessage("Not ready to send due to error")
            self._listener.setReadyToSend(True)

    def _setOSDPosition(self):
        if (
            self._client._config['chatMoveOSD'] and (
                self._client._config['chatOutputEnabled'] or (
                    self._client._config['chatInputEnabled'] and
                    self._client._config['chatInputPosition'] == constants.INPUT_POSITION_TOP
                )
            )
        ):
            self._setProperty("osd-align-y", "bottom")
            self._setProperty("osd-margin-y", int(self._client._config['chatOSDMargin']))

    def _recentlyReset(self):
        if not self.lastResetTime:
            return False
        elif time.time() < self.lastResetTime + constants.MPV_NEWFILE_IGNORE_TIME:
            return True
        else:
            return False

    def _onFileUpdate(self):
        self._client.ui.showDebugMessage("File update")
        snap = self._cache.snapshot()
        self._cache.mark_file_loaded()
        # Streams/slow demuxers may have no duration yet — normalise to the
        # 0.0 the old term-playing-msg `${=length:0}` expansion always gave.
        duration = snap.duration if snap.duration is not None else 0.0
        self.reactor.callFromThread(self._client.updateFile, snap.filename, duration, snap.path)
        if not (self._recentlyReset()):
            self._client.ui.showDebugMessage("onFileUpdate setting position to global position: {}".format(self._client.getGlobalPosition()))
            self.reactor.callFromThread(self.setPosition, self._client.getGlobalPosition())
        if snap.paused != self._client.getGlobalPaused():
            self._client.ui.showDebugMessage("onFileUpdate setting paused to global paused: {}".format(self._client.getGlobalPaused()))
            self.reactor.callFromThread(self.setPaused, self._client.getGlobalPaused())

    def __init__(self, client, playerPath, filePath, args):
        from twisted.internet import reactor
        self.reactor = reactor
        self._client = client
        self._set_defaults()

        self._playerIPCHandler = MPV
        self._create_listener(playerPath, filePath, args)

    def _set_defaults(self):
        # Sole owner of status state (gates, stamps, file-loaded): the cache
        # is written from the EventHandler thread and read from the reactor.
        self._cache = MpvStatusCache()
        self.quitReason = None
        self.delayedFilePath = None

    def _create_listener(self, playerPath, filePath, args):
        try:
            self._listener = self.__Listener(self, self._playerIPCHandler, playerPath, filePath, args)
        except ValueError:
            self._client.ui.showMessage(getMessage("mplayer-file-required-notification"))
            self._client.ui.showMessage(getMessage("mplayer-file-required-notification/example"))
            self.drop()
            return
        except AttributeError as e:
            self._client.ui.showErrorMessage("Could not load mpv: " + str(e))
            return
        self._listener.daemon = True
        self._listener.start()

        self._preparePlayer()

    def mpv_log_handler(self, level, prefix, text):
        self.lineReceived(text)

    class __Listener(threading.Thread):
        def __init__(self, playerController, playerIPCHandler, playerPath, filePath, args):
            self.playerIPCHandler = playerIPCHandler
            self.playerPath = playerPath
            self.mpv_arguments = playerController.getStartupArgs(args)
            self.mpv_running = True
            self.sendQueue = []
            self.readyToSend = True
            self.lastSendTime = None
            self.lastNotReadyTime = None
            self.__playerController = playerController
            if not self.__playerController._client._config["chatOutputEnabled"]:
                self.__playerController.alertOSDSupported = False
                self.__playerController.chatOSDSupported = False
            if self.__playerController.getPlayerPathErrors(playerPath, filePath):
                raise ValueError()
            if filePath and '://' not in filePath:
                if not os.path.isfile(filePath) and 'PWD' in os.environ:
                    filePath = os.environ['PWD'] + os.path.sep + filePath
                filePath = os.path.realpath(filePath)

            if filePath:
                self.__playerController.delayedFilePath = filePath

            # At least mpv may output escape sequences which result in syncplay
            # trying to parse something like
            # "\x1b[?1l\x1b>ANS_filename=blah.mkv". Work around this by
            # unsetting TERM.
            env = os.environ.copy()
            if 'TERM' in env:
                del env['TERM']
            try:
                self.mpvpipe = self.playerIPCHandler(
                    loglevel="info",
                    ipc_socket=self._get_ipc_socket(),
                    mpv_location=self.playerPath,
                    log_handler=self.__playerController.mpv_log_handler,
                    quit_callback=self.stop_client,
                    env=env,
                    **self.mpv_arguments
                )
            except Exception as e:
                self.quitReason = getMessage("media-player-error").format(str(e)) + " " + getMessage("mpv-failed-advice")
                self.__playerController.reactor.callFromThread(self.__playerController._client.ui.showErrorMessage, self.quitReason, True)
                self.__playerController.drop()
            self.__process = self.mpvpipe
            # Pipe is fully connected here — safe place to issue the blocking
            # observe_property commands (never do this from an EventHandler
            # callback).
            self.__playerController._wireObservers(self.mpvpipe)
            #self.mpvpipe.show_text("HELLO WORLD!", 1000)
            threading.Thread.__init__(self, name="MPV Listener")

        def _get_ipc_socket(self):
            if isWindows():
                # On Windows, mpv expects a named pipe identifier (not a path)
                return "syncplay-mpv-{0}".format(random.randint(0, 2**48))
            return getRuntimeDir().joinpath("mpv-socket").as_posix()

        def __getCwd(self, filePath, env):
            if not filePath:
                return None
            if os.path.isfile(filePath):
                cwd = os.path.dirname(filePath)
            elif 'HOME' in env:
                cwd = env['HOME']
            elif 'APPDATA' in env:
                cwd = env['APPDATA']
            else:
                cwd = None
            return cwd

        def run(self):
            pass

        def sendChat(self, message):
            if message:
                if message[:1] == "/" and message != "/":
                    command = message[1:]
                    if command and command[:1] == "/":
                        message = message[1:]
                    else:
                        self.__playerController.reactor.callFromThread(
                            self.__playerController._client.ui.executeCommand, command)
                        return
                self.__playerController.reactor.callFromThread(self.__playerController._client.sendChat, message)

        def isReadyForSend(self):
            self.checkForReadinessOverride()
            return self.readyToSend

        def setReadyToSend(self, newReadyState):
            oldState = self.readyToSend
            self.readyToSend = newReadyState
            self.lastNotReadyTime = time.time() if newReadyState == False else None
            if self.readyToSend == True:
                self.__playerController._client.ui.showDebugMessage("<mpv> Ready to send: True")
            else:
                self.__playerController._client.ui.showDebugMessage("<mpv> Ready to send: False")
            if self.readyToSend == True and oldState == False:
                self.processSendQueue()

        def checkForReadinessOverride(self):
            if self.lastNotReadyTime and time.time() - self.lastNotReadyTime > constants.MPV_MAX_NEWFILE_COOLDOWN_TIME:
                self.setReadyToSend(True)

        def sendLine(self, line, notReadyAfterThis=None):
            self.checkForReadinessOverride()
            try:
                if self.sendQueue:
                    if constants.MPV_SUPERSEDE_IF_DUPLICATE_COMMANDS:
                        for command in constants.MPV_SUPERSEDE_IF_DUPLICATE_COMMANDS:
                            line_command = " ".join(line)
                            answer = line_command.startswith(command)
                            #self.__playerController._client.ui.showDebugMessage("Does line_command {} start with {}? {}".format(line_command, command, answer))
                            if line_command.startswith(command):
                                for itemID, deletionCandidate in enumerate(self.sendQueue):
                                    if " ".join(deletionCandidate).startswith(command):
                                        self.__playerController._client.ui.showDebugMessage(
                                            "<mpv> Remove duplicate (supersede): {}".format(self.sendQueue[itemID]))
                                        try:
                                            self.sendQueue.remove(self.sendQueue[itemID])
                                        except UnicodeWarning:
                                            self.__playerController._client.ui.showDebugMessage(
                                                "<mpv> Unicode mismatch occurred when trying to remove duplicate")
                                            # TODO: Prevent this from being triggered
                                            pass
                                        break
                            break
                    if constants.MPV_REMOVE_BOTH_IF_DUPLICATE_COMMANDS:
                        for command in constants.MPV_REMOVE_BOTH_IF_DUPLICATE_COMMANDS:
                            if line == command:
                                for itemID, deletionCandidate in enumerate(self.sendQueue):
                                    if deletionCandidate == command:
                                        self.__playerController._client.ui.showDebugMessage(
                                            "<mpv> Remove duplicate (delete both): {}".format(self.sendQueue[itemID]))
                                        self.__playerController._client.ui.showDebugMessage(self.sendQueue[itemID])
                                        return
            except Exception as e:
                self.__playerController._client.ui.showDebugMessage("<mpv> Problem removing duplicates, etc: {}".format(e))
            self.sendQueue.append(line)
            self.processSendQueue()
            if notReadyAfterThis:
                self.setReadyToSend(False)

        def processSendQueue(self):
            while self.sendQueue and self.readyToSend:
                if self.lastSendTime and time.time() - self.lastSendTime < constants.MPV_SENDMESSAGE_COOLDOWN_TIME:
                    self.__playerController._client.ui.showDebugMessage(
                        "<mpv> Throttling message send, so sleeping for {}".format(
                            constants.MPV_SENDMESSAGE_COOLDOWN_TIME))
                    time.sleep(constants.MPV_SENDMESSAGE_COOLDOWN_TIME)
                try:
                    lineToSend = self.sendQueue.pop()
                    if lineToSend:
                        self.lastSendTime = time.time()
                        self.actuallySendLine(lineToSend)
                except IndexError:
                    pass

        def stop_client(self):
            self.readyToSend = False
            try:
                self.mpvpipe.terminate()
            except: #When mpv is already closed
                pass
            self.__playerController.reactor.callFromThread(self.__playerController._client.stop, False)

        def actuallySendLine(self, line):
            try:
                self.__playerController._client.ui.showDebugMessage("player >> {}".format(line))
                try:
                    self.mpvpipe.command(*line)
                except Exception as e:
                    self.__playerController._client.ui.showDebugMessage("CANNOT SEND {} DUE TO {}".format(line, e))
                    self.stop_client()
            except IOError:
                pass
