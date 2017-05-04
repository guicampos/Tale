"""
Non-Player-Character classes

'Tale' mud driver, mudlib and interactive fiction framework
Copyright by Irmen de Jong (irmen@razorvine.net)
"""

import random
from typing import Optional
from . import base
from . import lang
from .errors import ActionRefused


class NPC(base.Living):
    """
    Non-Player-Character: computer controlled entity.
    These are neutral or friendly or aggressive (defaults to non-aggressive)
    """
    def __init__(self, name: str, gender: str, race: str="human", title: str=None,
                 description: str=None, short_description: str=None) -> None:
        super().__init__(name, gender, race, title, description, short_description)
        self.aggressive = False

    def insert(self, item: base.Item, actor: base.Living) -> None:
        """NPC have a bit nicer refuse message when giving items to them."""
        if not self.aggressive or actor is self or actor is not None and "wizard" in actor.privileges:
            super().insert(item, self)
        else:
            if self.aggressive:
                raise ActionRefused("It's probably not a good idea to give %s to %s." % (item.title, self.title))
            raise ActionRefused("%s doesn't want %s." % (lang.capital(self.title), item.title))

    def allow_give_money(self, actor: base.Living, amount: float) -> None:
        """Do we accept money? Raise ActionRefused if not."""
        if self.stats.race not in (None, "human"):
            raise ActionRefused("You can't do that.")

    def select_random_move(self) -> Optional[base.Exit]:
        """
        Select a random accessible exit to move to.
        Avoids exits to a room that have no exits (traps).
        If no suitable exit is found in a few random attempts, return None.
        """
        directions_with_exits = [d for d, e in self.location.exits.items() if e.target.exits]
        if directions_with_exits:
            for tries in range(4):
                direction = random.choice(directions_with_exits)
                xt = self.location.exits[direction]
                try:
                    xt.allow_passage(self)
                except ActionRefused:
                    continue
                else:
                    return xt
        return None

    def start_attack(self, victim: base.Living) -> None:
        """
        Starts attacking the given living until death ensues on either side
        """
        # @TODO actual fight
        name = lang.capital(self.title)
        room_msg = "%s starts attacking %s!" % (name, victim.title)
        victim_msg = "%s starts attacking you!" % name
        attacker_msg = "You start attacking %s!" % victim.title
        victim.tell(victim_msg)
        victim.location.tell(room_msg, exclude_living=victim, specific_targets=[self], specific_target_msg=attacker_msg)
