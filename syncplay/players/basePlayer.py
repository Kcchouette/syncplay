from syncplay import constants


class BasePlayer(object):
    """Adapter contract every concrete player implements.

    Instance methods are invoked from the Twisted reactor thread: they must
    not block (askForStatus in particular must return immediately; long IPC
    waits belong on worker threads). Concrete players declare their
    capabilities with the class attributes below; the defaults here guard
    DummyPlayer (substituted when an optional player cannot be imported)
    and future adapters.

    Deliberately not an ABC: DummyPlayer intentionally stubs only the
    static path helpers, and every concrete class already implements the
    full surface (NotImplementedError remains the contract).
    """

    #: Player can change playback speed (client uses this to slow on desync).
    speedSupported = False
    #: Player can render chat lines on its OSD.
    chatOSDSupported = False
    #: Player can render alert-priority OSD.
    alertOSDSupported = False
    #: Player supplies its own file-open dialog (GUI checks before offering browse UI).
    customOpenDialog = False
    #: String joining multi-part OSD messages.
    osdMessageSeparator = "; "

    def askForStatus(self):
        '''Call client.updatePlayerStatus(paused, position); must not block.'''
        raise NotImplementedError()

    def displayMessage(
        self, message, duration=(constants.OSD_DURATION*1000), OSDType=constants.OSD_NOTIFICATION, mood=constants.MESSAGE_NEUTRAL
    ):
        '''Display given message on player's OSD or similar means.'''
        raise NotImplementedError()

    def drop(self):
        '''Cleanup connection with player before syncplay will close down.'''
        raise NotImplementedError()

    @staticmethod
    def run(client, playerPath, filePath, args):
        '''Start up the player, returns its instance.'''
        raise NotImplementedError()

    def setPaused(self, value):
        '''@type value: boolean'''
        raise NotImplementedError()

    def setFeatures(self, featureList):
        '''@type featureList: list'''
        raise NotImplementedError()

    def setPosition(self, value):
        '''@type value: float'''
        raise NotImplementedError()

    def setSpeed(self, value):
        '''@type value: float'''
        raise NotImplementedError()

    def openFile(self, filePath, resetPosition=False):
        '''@type filePath: string'''
        raise NotImplementedError()

    @staticmethod
    def getDefaultPlayerPathsList():
        '''@return: list of strings'''
        raise NotImplementedError()

    @staticmethod
    def isValidPlayerPath(path):
        '''@type path: string'''
        raise NotImplementedError()

    @staticmethod
    def getIconPath(path):
        '''@type path: string; @return: string'''
        raise NotImplementedError()

    @staticmethod
    def getExpandedPath(path):
        '''@type path: string; @return: string'''
        raise NotImplementedError()

    def openCustomOpenDialog(self):
        '''Open a custom media browse dialog, then switch to that media.

        Instance method (not a staticmethod): the GUI calls it
        instance-style with zero arguments, which the old
        ``@staticmethod def openCustomOpenDialog(self)`` signature would
        have rejected had any player ever enabled customOpenDialog.
        '''
        raise NotImplementedError()

    @staticmethod
    def getPlayerPathErrors(playerPath, filePath):
        '''@type playerPath: string; @type filePath: string; @return: errorMessage string or None'''
        raise NotImplementedError()


class DummyPlayer(BasePlayer):

    @staticmethod
    def getDefaultPlayerPathsList():
        return []

    @staticmethod
    def isValidPlayerPath(path):
        return False

    @staticmethod
    def getIconPath(path):
        return None

    @staticmethod
    def getExpandedPath(path):
        return path

    @staticmethod
    def getPlayerPathErrors(playerPath, filePath):
        return None
