"""
'Circle' -  an attempt to run the CircleMUD world data.

Written for Tale IF framework.
Copyright by Irmen de Jong (irmen@razorvine.net)
"""

import datetime
import sys
from typing import Optional

from tale.driver import Driver
from tale.player import Player
from tale.story import *


class Story(StoryBase):
    # create story configuration and customize:
    config = StoryConfig()
    config.name = "Circle"
    config.author = "Irmen de Jong"
    config.author_address = "irmen@razorvine.net"
    config.version = "1.5"
    config.requires_tale = "3.3"
    config.supported_modes = {GameMode.MUD}
    config.player_name = None
    config.player_gender = None
    config.player_race = None
    config.player_money = 0.0
    config.money_type = MoneyType.FANTASY
    config.server_tick_method = TickMethod.TIMER
    config.server_tick_time = 1.0
    config.gametime_to_realtime = 5
    config.display_gametime = True
    config.epoch = datetime.datetime(2015, 5, 14, 14, 0, 0)       # start date/time of the game clock
    config.startlocation_player = "midgaard_city.temple"
    config.startlocation_wizard = "god_simplex.boardroom"
    config.savegames_enabled = False
    config.show_exits_in_look = False
    config.mud_host = "localhost"
    config.mud_port = 8200
    # story-specific fields follow:
    driver = None     # will be set by init()

    def init(self, driver: Driver) -> None:
        """Called by the game driver when it is done with its initial initialization"""
        print("Story initialization started by driver.")
        self.driver = driver
        from zones import init_zones
        init_zones()

    def init_player(self, player: Player) -> None:
        """
        Called by the game driver when it has created the player object.
        You can set the hint texts on the player object, or change the state object, etc.
        """
        pass

    def welcome(self, player: Player) -> Optional[str]:
        """
        Welcome text when player enters a new game
        If you return a string, it is used as an input prompt before continuing (a pause).
        """
        player.tell("<bright>Hello, <player>%s</><bright>!  Welcome to '%s'.</>" % (player.title, self.config.name), end=True)
        player.tell("--", end=True)
        return None


if __name__ == "__main__":
    # story is invoked as a script, start it in the Tale Driver.
    Driver().start(game=sys.path[0], mode=GameMode.MUD)
