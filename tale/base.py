"""
Mudlib base objects.

'Tale' mud driver, mudlib and interactive fiction framework
Copyright by Irmen de Jong (irmen@razorvine.net)

object hierarchy::

    MudObject (abstract base class, don't use directly)
      |
      +-- Location
      |
      +-- Item
      |     |
      |     +-- Weapon
      |     +-- Armour
      |     +-- Container
      |     +-- Key
      |
      +-- Living (abstract base class, don't use directly)
      |     |
      |     +-- Player
      |     +-- NPC
      |          |
      |          +-- Shopkeeper
      |
      +-- Exit
            |
            +-- Door


Every object that can hold other objects does so in its "inventory" (a set).
You can't access it directly, object.inventory returns a frozenset copy of it.
Except Location: it separates the items and livings it contains internally.
Use its enter/leave methods instead.
"""

import re
import copy
import random
from textwrap import dedent
from collections import defaultdict
from types import ModuleType
from typing import Iterable, Any, Type, Sequence, Optional, Set, Dict, Union, FrozenSet, Tuple, List
from . import lang
from . import pubsub
from . import mud_context
from . import races
from . import util
from . import verbdefs
from .parseresult import ParseResult
from .errors import ActionRefused, ParseError, LocationIntegrityError, TaleError, UnknownVerbException, NonSoulVerb


__all__ = ["MudObject", "Armour", 'Container', "Door", "Exit", "Item", "Living", "Stats", "Location",
           "Weapon", "Key", "heartbeat", "clone", "Soul"]

pending_actions = pubsub.topic("driver-pending-actions")
pending_tells = pubsub.topic("driver-pending-tells")
async_dialogs = pubsub.topic("driver-async-dialogs")


def heartbeat(klass: Type) -> Type:
    """
    Decorator to use on a class to make it have a heartbeat.
    Use sparingly as it is less efficient than using a deferred, because the driver
    has to call all heartbeats every tick even though they do nothing yet.
    With deferreds, the driver only calls a deferred at the time it is needed.
    """
    klass._register_heartbeat = True
    return klass


class MudObject:
    """
    Root class of all objects in the mud world
    All objects have an identifying short name (will be lowercased),
    an optional short title (shown when listed in a room),
    and an optional longer description (shown when explicitly 'examined').
    The long description is 'dedented' first, which means you can put it between triple-quoted-strings easily.
    Short_description is also optional, and is used in the text when a player 'looks' around.
    If it's not set, a generic 'look' message will be shown (something like "XYZ is here").

    Extra descriptions (extra_desc) are used to make stuff more interesting and interactive
    Extra descriptions are accessed by players when they type ``look at <thing>``
    where <thing> is any keyword you choose.  For example, you might write a room description which
    includes the tantalizing sentence, ``The wall looks strange here.``
    Using extra descriptions, players could then see additional detail by typing
    ``look at wall.``  There can be an unlimited number of Extra Descriptions.
    """
    # @todo add vnum?   (circle provides the number itself, otherwise assign one automatically?)
    # @todo and add the vnum commands from circle as default commands in that case.
    subjective = "it"
    possessive = "its"
    objective = "it"
    gender = "n"

    @staticmethod
    def __new__(cls, *args, **kwargs):
        if cls is MudObject:
            raise TypeError("don't create MudObject directly, use one of the subclasses")
        return super().__new__(cls)

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = value

    @property
    def description(self) -> str:
        return self._description

    @description.setter
    def description(self, value: str) -> None:
        self._description = value

    @property
    def short_description(self) -> str:
        return self._short_description

    @short_description.setter
    def short_description(self, value: str) -> None:
        self._short_description = value

    @property
    def extra_desc(self) -> Dict[str, str]:
        return self._extradesc

    @extra_desc.setter
    def extra_desc(self, value: Dict[str, str]) -> None:
        assert isinstance(value, dict)
        self._extradesc = value

    def __init__(self, name: str, title: str=None, description: str=None, short_description: str=None) -> None:
        self._extradesc = None  # type: Dict[str,str]
        self.name = self._description = self._title = self._short_description = None  # type: str
        self.init_names(name, title, description, short_description)
        self.aliases = set()  # type: Set[str]
        # any custom verbs that need to be recognised (verb->docstring mapping),
        # verb handling is done via handle_verb() callbacks.
        self.verbs = {}   # type: Dict[str, str]
        if getattr(self, "_register_heartbeat", False):
            # one way of setting this attribute is by using the @heartbeat decorator
            self.register_heartbeat()
        self.init()

    def init(self) -> None:
        """
        Secondary initialization/customization. Invoked after all required initialization has been done.
        You can easily override this in a subclass.
        """
        pass

    def init_names(self, name: str, title: str, description: str, short_description: str) -> None:
        """(re)set the name and description attributes"""
        self.name = name.lower()
        if title:
            assert not title.startswith("the ") and not title.startswith("The "), "title must not start with 'the'"
            assert not title.startswith("a ") and not title.startswith("A "), "title must not start with 'a'"
        self._title = title or name
        self._description = dedent(description).strip() if description else ""
        self._short_description = short_description
        self._extradesc = {}   # maps keyword to description

    def add_extradesc(self, keywords: Set[str], description: str) -> None:
        """For the set of keywords, add the extra description text"""
        for keyword in keywords:
            self._extradesc[keyword] = description

    def __repr__(self):
        return "<%s '%s' @ 0x%x>" % (self.__class__.__name__, self.name, id(self))

    def destroy(self, ctx: util.Context) -> None:
        """Common cleanup code that needs to be called when the object is destroyed"""
        assert isinstance(ctx, util.Context)
        self.unregister_heartbeat()
        mud_context.driver.remove_deferreds(self)

    def wiz_clone(self, actor: 'Living') -> 'MudObject':
        """clone the thing (performed by a wizard)"""
        raise ActionRefused("Can't clone " + lang.a(self.__class__.__name__))

    def wiz_destroy(self, actor: 'Living', ctx: util.Context) -> None:
        """destroy the thing (performed by a wizard)"""
        raise ActionRefused("Can't destroy " + lang.a(self.__class__.__name__))

    def show_inventory(self, actor: 'Living', ctx: util.Context) -> None:
        """show the object's inventory to the actor"""
        raise ActionRefused("You can't look inside of that.")

    def register_heartbeat(self) -> None:
        """register this object with the driver to receive heartbeats"""
        mud_context.driver.register_heartbeat(self)

    def unregister_heartbeat(self) -> None:
        """tell the driver to forget about this object for heartbeats"""
        mud_context.driver.unregister_heartbeat(self)

    def heartbeat(self, ctx: util.Context) -> None:
        # not automatically called, only if your object registered with the driver
        pass

    def activate(self, actor: 'Living') -> None:
        # called from the activate command, override if your object needs to act on this.
        raise ActionRefused("You can't activate that.")

    def deactivate(self, actor: 'Living') -> None:
        # called from the deactivate command, override if your object needs to act on this.
        raise ActionRefused("You can't deactivate that.")

    def manipulate(self, verb: str, actor: 'Living') -> None:
        # called from the various manipulate commands, override if your object needs to act on this.
        # verb: move, shove, swivel, shift, manipulate, rotate, press, poke, push, turn
        raise ActionRefused("You can't %s that." % verb)

    def move(self, target: Union['Location', 'Container', 'Living'], actor: 'Living'=None,
             silent: bool=False, is_player: bool=False, verb: str="move") -> None:
        # move the MudObject to a different place (location, container, living).
        raise ActionRefused("You can't %s that." % verb)

    def read(self, actor: 'Living') -> None:
        # called from the read command, override if your object needs to act on this.
        raise ActionRefused("There's nothing to read.")

    def insert(self, obj: Union['Living', 'Item'], actor: Optional['Living']) -> None:
        raise ActionRefused("You can't put things in there.")

    def remove(self, obj: Union['Living', 'Item'], actor: Optional['Living']) -> None:
        raise ActionRefused("You can't take things from there.")

    def handle_verb(self, parsed: ParseResult, actor: 'Living') -> bool:
        """Handle a custom verb. Return True if handled, False if not handled."""
        return False

    def notify_action(self, parsed: ParseResult, actor: 'Living') -> None:
        """Notify the object of an action performed by someone. This can be any verb, command, soul emote, custom verb."""
        pass


class Item(MudObject):
    """
    Root class of all Items in the mud world. Items are physical objects.
    Items can usually be moved, carried, or put inside other items.
    They have a name and optional short and longer descriptions.
    Regular items cannot contain other things, so it makes to sense
    to check containment.
    """
    def init(self) -> None:
        self.contained_in = None  # type: Union[Location, Living, Container]
        self.default_verb = "examine"
        self.value = 0.0   # what the item is worth
        self.rent = 0.0    # price to keep in store / day
        self.weight = 0.0  # some abstract unit

    def __contains__(self, item: 'Item') -> bool:
        raise ActionRefused("You can't look inside of that.")

    @property
    def location(self) -> Optional['Location']:
        if not self.contained_in:
            return None
        if isinstance(self.contained_in, Location):
            return self.contained_in
        return self.contained_in.location

    @location.setter
    def location(self, value: 'Location') -> None:
        if value is None or isinstance(value, Location):
            self.contained_in = value
        else:
            raise TypeError("can only set item's location to a Location, for other container types use item.contained_in")

    @property
    def inventory(self) -> FrozenSet['Item']:
        raise ActionRefused("You can't look inside of that.")

    @property
    def inventory_size(self) -> int:
        raise ActionRefused("You can't look inside of that.")

    def move(self, target: Union['Location', 'Container', 'Living'], actor: 'Living'=None,
             silent: bool=False, is_player: bool=False, verb: str="move") -> None:
        """
        Leave the container the item is currently in, enter the target container (transactional).
        Because items can move on various occasions, there's no message being printed.
        The silent and is_player arguments are not used when moving items -- they're used
        for the movement of livings.
        """
        self.allow_item_move(actor, verb)
        source_container = self.contained_in
        if source_container:
            source_container.remove(self, actor)
        try:
            target.insert(self, actor)
            self.notify_moved(source_container, target, actor)
        except:
            # insert in target failed, put back in original location
            source_container.insert(self, actor)
            raise

    def notify_moved(self, source_container: Union['Location', 'Container', 'Living'],
                     target_container: Union['Location', 'Container', 'Living'], actor: 'Living') -> None:
        """Called when the item has been moved from one place to another"""
        pass

    def allow_item_move(self, actor: 'Living', verb: str="move") -> None:
        """Does the item allow to be moved by someone? (yes; no ActionRefused is raised)"""
        pass

    def open(self, actor: 'Living', item: 'Item'=None) -> None:
        raise ActionRefused("You can't open that.")

    def close(self, actor: 'Living', item: 'Item'=None) -> None:
        raise ActionRefused("You can't close that.")

    def lock(self, actor: 'Living', item: 'Item'=None) -> None:
        raise ActionRefused("You can't lock that.")

    def unlock(self, actor: 'Living', item: 'Item'=None) -> None:
        raise ActionRefused("You can't unlock that.")

    def combine(self, other: 'Item', actor: 'Living') -> None:
        # combine the other thing with us
        raise ActionRefused("You can't combine these.")

    @util.authorized("wizard")
    def wiz_clone(self, actor: 'Living') -> 'Item':
        item = clone(self)
        actor.insert(item, actor)
        actor.tell("Cloned into: " + repr(item))
        actor.tell_others("{Title} conjures up %s, and quickly pockets it." % lang.a(item.title))
        return item

    @util.authorized("wizard")
    def wiz_destroy(self, actor: 'Living', ctx: util.Context) -> None:
        if self in actor:
            actor.remove(self, actor)
        else:
            actor.location.remove(self, actor)
        self.destroy(ctx)

    def show_inventory(self, actor: 'Living', ctx: util.Context) -> None:
        """show the object's contents to the actor"""
        if self.inventory:
            actor.tell("It contains:", end=True)
            for item in self.inventory:
                actor.tell("  " + item.title, format=False)
        else:
            actor.tell("It's empty.")

    @staticmethod
    def search_item(name: str, collection: Iterable['Item']) -> 'Item':
        """
        Searches an item (by name) in a collection of Items.
        Returns the first match. Also considers aliases and titles.
        """
        name = name.lower()
        items = [i for i in collection if i.name == name]
        if not items:
            # try the aliases or titles
            items = [i for i in collection if name in i.aliases or i.title.lower() == name]
        return items[0] if items else None


def clone(obj: Item) -> Any:
    """Create a copy of an existing Item. Only when it has an empty inventory (to avoid problems)"""
    if isinstance(obj, Item):
        try:
            if obj.inventory_size > 0:
                raise ValueError("can't clone something that has other stuff in it")
        except ActionRefused:
            pass
        if obj.location:
            # avoid deepcopying the location
            location, obj.location = obj.location, None
            duplicate = copy.deepcopy(obj)
            obj.location = duplicate.location = location
            return duplicate
        return copy.deepcopy(obj)
    raise TypeError("can only clone Items")


class Weapon(Item):
    """
    An item that can be wielded by a Living (i.e. present in a weapon itemslot),
    and that can be used to attack another Living.
    """
    pass


class Armour(Item):
    """
    An item that can be worn by a Living (i.e. present in an armour itemslot)
    """
    pass


class Location(MudObject):
    """
    A location in the mud world. Livings and Items are in it.
    Has connections ('exits') to other Locations.
    You can test for containment with 'in': item in loc, npc in loc
    """
    def __init__(self, name: str, description: str=None) -> None:
        super().__init__(name, description=description)
        self.name = name      # make sure we preserve the case; base object stores it lowercase
        self.livings = set()  # type: Set[Living] # set of livings in this location
        self.items = set()    # type: Set[Item] # set of all items in the room
        self.exits = {}       # type: Dict[str, Exit] # dictionary of all exits: exit_direction -> Exit object with target & descr

    def __contains__(self, obj: Union['Living', Item]) -> bool:
        return obj in self.livings or obj in self.items

    def __getstate__(self) -> dict:
        state = dict(self.__dict__)
        return state

    def __setstate__(self, state: dict):
        self.__dict__ = state

    def init_inventory(self, objects: Iterable[Union[Item, 'Living']]) -> None:
        """Set the location's initial item and livings 'inventory'"""
        if len(self.items) > 0 or len(self.livings) > 0:
            raise LocationIntegrityError("clobbering existing inventory", None, None, self)
        for obj in objects:
            self.insert(obj, None)

    def destroy(self, ctx: util.Context) -> None:
        super().destroy(ctx)
        for living in self.livings:
            if living.location is self:
                living.location = _limbo
        self.livings.clear()
        self.items.clear()
        self.exits.clear()

    def add_exits(self, exits: Iterable['Exit']) -> None:
        """Adds every exit from the sequence as an exit to this room."""
        for exit in exits:
            exit.bind(self)
            # note: we're not simply adding it to the .exits dict here, because
            # the exit may have aliases defined that it wants to be known as also.

    def get_wiretap(self) -> pubsub.Topic:
        """get a wiretap for this location"""
        return pubsub.topic(("wiretap-location", self.name))

    def tell(self, room_msg: str, exclude_living: 'Living'=None,
             specific_targets: Set[Union['Location', 'Living']]=None, specific_target_msg: str="") -> None:
        """
        Tells something to the livings in the room (excluding the living from exclude_living).
        This is just the message string! If you want to react on events, consider not doing
        that based on this message string. That will make it quite hard because you need to
        parse the string again to figure out what happened... Use handle_verb / notify_action instead.
        """
        targets = specific_targets or set()
        assert isinstance(targets, (frozenset, set, list, tuple))
        assert exclude_living is None or isinstance(exclude_living, Living)
        for living in self.livings:
            if living == exclude_living:
                continue
            if living in targets:
                living.tell(specific_target_msg)
            else:
                living.tell(room_msg)
        if room_msg:
            tap = self.get_wiretap()
            tap.send((self.name, room_msg))

    def message_nearby_locations(self, message: str) -> None:
        """
        Tells a message to adjacent locations, where adjacent is defined by being connected via an exit.
        If the adjacent location has an obvious returning exit to the source location (via one of the
        most obvious routes n/e/s/w/up/down/etc.), it hen also get information on what direction
        the sound originated from.  This is used for loud noises such as yells!
        """
        if self.exits:
            yelled_locations = set()  # type: Set[Location]
            for exit in self.exits.values():
                if exit.target in yelled_locations:
                    continue   # skip double locations (possible because there can be multiple exits to the same location)
                if exit.target is not self:
                    exit.target.tell(message)
                    yelled_locations.add(exit.target)
                    for direction, return_exit in exit.target.exits.items():
                        if return_exit.target is self:
                            if direction in {"north", "east", "south", "west", "northeast", "northwest", "southeast",
                                             "southwest", "left", "right", "front", "back"}:
                                direction = "the " + direction
                            elif direction in {"up", "above", "upstairs"}:
                                direction = "above"
                            elif direction in {"down", "below", "downstairs"}:
                                direction = "below"
                            else:
                                continue  # no direction description possible for this exit
                            exit.target.tell("The sound is coming from %s." % direction)
                            break
                    else:
                        exit.target.tell("You can't hear where the sound is coming from.")

    def nearby(self, no_traps: bool=True) -> Iterable['Location']:
        """
        Returns a sequence of all adjacent locations, normally avoiding 'traps' (locations without a way back).
        (this may be expanded in the future with a way to search further than just 1 step away)
        """
        if no_traps:
            return (e.target for e in self.exits.values() if e.target.exits)
        return (e.target for e in self.exits.values())

    def look(self, exclude_living: 'Living'=None, short: bool=False) -> Sequence[str]:
        """returns a list of paragraph strings describing the surroundings, possibly excluding one living from the description list"""
        paragraphs = ["<location>[" + self.name + "]</>"]
        if short:
            if self.exits and mud_context.config.show_exits_in_look:
                paragraphs.append("<exit>Exits</>: " + ", ".join(sorted(set(self.exits.keys()))))
            if self.items:
                item_names = sorted(item.name for item in self.items)
                paragraphs.append("<item>You see</>: " + lang.join(item_names))
            if self.livings:
                living_names = sorted(living.name for living in self.livings if living != exclude_living)
                if living_names:
                    paragraphs.append("<living>Present</>: " + lang.join(living_names))
            return paragraphs
        # normal (long) output
        if self.description:
            paragraphs.append(self.description)
        if self.exits and mud_context.config.show_exits_in_look:
            exits_seen = set()  # type: Set[Exit]
            exit_paragraph = []  # type: List[str]
            for exit_name in sorted(self.exits):
                exit = self.exits[exit_name]
                if exit not in exits_seen:
                    exits_seen.add(exit)
                    exit_paragraph.append(exit.short_description)
            paragraphs.append(" ".join(exit_paragraph))
        items_and_livings = []  # type: List[str]
        items_with_short_descr = [item for item in self.items if item.short_description]
        items_without_short_descr = [item for item in self.items if not item.short_description]
        uniq_descriptions = set()
        if items_with_short_descr:
            for item in items_with_short_descr:
                uniq_descriptions.add(item.short_description)
        items_and_livings.extend(uniq_descriptions)
        if items_without_short_descr:
            titles = sorted([lang.a(item.title) for item in items_without_short_descr])
            items_and_livings.append("You see " + lang.join(titles) + ".")
        livings_with_short_descr = [living for living in self.livings if living != exclude_living and living.short_description]
        livings_without_short_descr = [living for living in self.livings if living != exclude_living and not living.short_description]
        if livings_without_short_descr:
            titles = sorted(living.title for living in livings_without_short_descr)
            if titles:
                titles_str = lang.join(titles)
                if len(titles) > 1:
                    titles_str += " are here."
                else:
                    titles_str += " is here."
                items_and_livings.append(lang.capital(titles_str))
        uniq_descriptions = set()
        if livings_with_short_descr:
            for living in livings_with_short_descr:
                uniq_descriptions.add(living.short_description)
        items_and_livings.extend(uniq_descriptions)
        if items_and_livings:
            paragraphs.append(" ".join(items_and_livings))
        return paragraphs

    def search_living(self, name: str) -> 'Living':
        """
        Search for a living in this location by its name (and title, if no names match).
        Is alias-aware. If there's more than one match, returns the first.
        """
        name = name.lower()
        result = [living for living in self.livings if living.name == name]
        if not result:
            # try titles and aliases
            result = [living for living in self.livings if name in living.aliases or living.title.lower() == name]
        return result[0] if result else None

    def insert(self, obj: Union['Living', Item], actor: Optional['Living']) -> None:
        """Add item to the contents of the location (either a Living or an Item)"""
        assert obj is not None
        if isinstance(obj, Living):
            self.livings.add(obj)
        elif isinstance(obj, Item):
            self.items.add(obj)
        else:
            raise TypeError("can only add Living or Item")
        obj.location = self

    def remove(self, obj: Union['Living', Item], actor: Optional['Living']) -> None:
        """Remove obj from this location (either a Living or an Item)"""
        assert obj is not None
        if obj in self.livings:
            self.livings.remove(obj)  # type: ignore
        elif obj in self.items:
            self.items.remove(obj)    # type: ignore
        else:
            return   # just ignore an object that wasn't present in the first place
        obj.location = None

    def handle_verb(self, parsed: ParseResult, actor: 'Living') -> bool:
        """Handle a custom verb. Return True if handled, False if not handled."""
        # this code cannot deal with yields directly but you can raise AsyncDialog exception,
        # that indicates to the driver that it should initiate the given async dialog when continuing.
        handled = any(living._handle_verb_base(parsed, actor) for living in self.livings)
        if not handled:
            handled = any(item.handle_verb(parsed, actor) for item in self.items)
            if not handled:
                handled = any(exit.handle_verb(parsed, actor) for exit in set(self.exits.values()))
        return handled

    def notify_action(self, parsed: ParseResult, actor: 'Living') -> None:
        """Notify the room, its livings and items of an action performed by someone."""
        # Notice that this notification event is invoked by the driver after all
        # actions concerning player input have been handled, so we don't have to
        # queue the delegated calls.
        for living in self.livings:
            living._notify_action_base(parsed, actor)
        for item in self.items:
            item.notify_action(parsed, actor)
        for exit in set(self.exits.values()):
            exit.notify_action(parsed, actor)

    def notify_npc_arrived(self, npc: 'Living', previous_location: 'Location') -> None:
        """a NPC has arrived in this location."""
        pass

    def notify_npc_left(self, npc: 'Living', target_location: 'Location') -> None:
        """a NPC has left the location."""
        pass

    def notify_player_arrived(self, player, previous_location: 'Location') -> None:
        """a player has arrived in this location."""
        pass

    def notify_player_left(self, player, target_location: 'Location') -> None:
        """a player has left this location."""
        pass


_limbo = Location("Limbo",
                  """
                  The intermediate or transitional place or state. There's only nothingness.
                  Living beings end up here if they're not in a proper location yet.
                  """)


class Stats:
    def __init__(self) -> None:
        self.gender = 'n'
        self.level = 0
        self.xp = 0
        self.hp = 0
        self.maxhp_dice = None  # type: str
        self.ac = 0
        self.attack_dice = None  # type: str  # damage roll when attacking without a weapon
        self.agi = 0
        self.cha = 0
        self.int = 0
        self.lck = 0
        self.spd = 0
        self.sta = 0
        self.str = 0
        self.wis = 0
        self.stat_prios = None    # type: Dict[int, List[races.Stats]]  # per agi/cha/etc stat, priority level of it (see races.py)
        self.alignment = 0   # -1000 (evil) to +1000 (good), neutral=[-349..349]
        self.bodytype = races.BodyType.HUMANOID
        self.language = None   # type: str
        self.weight = 0.0
        self.size = races.BodySize.HUMAN_SIZED
        self.race = None  # type: str  # optional, can use the stats template from races

    def __repr__(self):
        return "<Stats: %s>" % vars(self)

    @classmethod
    def from_race(cls, race, gender='n') -> 'Stats':
        r = races.races[race]
        s = cls()
        s.gender = gender
        s.race = race
        s.agi = r.stats.agi[0]
        s.cha = r.stats.cha[0]
        s.int = r.stats.int[0]
        s.lck = r.stats.lck[0]
        s.spd = r.stats.spd[0]
        s.sta = r.stats.sta[0]
        s.str = r.stats.str[0]
        s.wis = r.stats.wis[0]
        s.set_stats_from_race()
        # @todo initialize xp, hp, maxhp, ac, attack, alignment, level. Current race defs don't include this data
        return s

    def set_stats_from_race(self) -> None:
        # the stats that are static are always initialized from the races table
        self.stat_prios = defaultdict(list)   # type: Dict[int, List[races.Stats]]
        r = races.races[self.race]
        for stat, (_, prio) in r.stats._asdict().items():
            self.stat_prios[prio].append(stat)      # XXX type error
        self.bodytype = r.body
        self.language = r.language
        self.weight = r.mass
        self.size = r.size


class Living(MudObject):
    """
    A living entity in the mud world (also known as an NPC).
    Livings sometimes have a heart beat 'tick' that makes them interact with the world.
    They are always inside a Location (Limbo when not specified yet).
    They also have an inventory object, and you can test for containment with item in living.
    """
    def __init__(self, name: str, gender: str, race: str="human",
                 title: str=None, description: str=None, short_description: str=None) -> None:
        if race:
            self.stats = Stats.from_race(race, gender=gender)
        else:
            self.stats = Stats()
        self.init_gender(gender)
        self.soul = Soul()
        self.location = _limbo  # set transitional location
        self.privileges = set()  # type: Set[str] # probably only used for Players though
        self.aggressive = False
        self.money = 0.0  # the currency is determined by util.MoneyFormatter set in the driver
        self.default_verb = "examine"
        self.__inventory = set()   # type: Set[Item]
        self.previous_commandline = None   # type: str
        self._previous_parse = None  # type: ParseResult
        super().__init__(name, title, description, short_description)

    def init_gender(self, gender: str) -> None:
        """(re)set gender attributes"""
        self.gender = gender
        self.stats.gender = gender  # notice that for completeness, gender is also present on the stats object
        self.subjective = lang.SUBJECTIVE[self.gender]
        self.possessive = lang.POSSESSIVE[self.gender]
        self.objective = lang.OBJECTIVE[self.gender]

    def init_inventory(self, items: Iterable[Item]) -> None:
        """Set the living's initial inventory"""
        assert len(self.__inventory) == 0
        for item in items:
            self.insert(item, self)

    def __getstate__(self) -> dict:
        state = dict(self.__dict__)
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__ = state

    def __contains__(self, item: Union['Living', Item, Location]) -> bool:
        return item in self.__inventory

    @property
    def inventory_size(self) -> int:
        return len(self.__inventory)

    @property
    def inventory(self) -> FrozenSet[Item]:
        return frozenset(self.__inventory)

    def insert(self, item: Union['Living', Item], actor: Optional['Living']) -> None:
        """Add an item to the inventory."""
        assert item is not None
        if not isinstance(item, Item):
            raise ActionRefused("You can't do that.")
        if actor is self or actor is not None and "wizard" in actor.privileges:
            self.__inventory.add(item)
            item.contained_in = self
            return
        if self.aggressive:
            raise ActionRefused("It's probably not a good idea to give %s to %s." % (item.title, self.title))
        # default behavior is to refuse stuff given to us:
        raise ActionRefused("%s doesn't want %s." % (lang.capital(self.title), item.title))

    def remove(self, item: Union['Living', Item], actor: Optional['Living']) -> None:
        """remove an item from the inventory"""
        assert item is not None
        if not isinstance(item, Item):
            raise ActionRefused("You can't do that.")
        if actor is self or actor is not None and "wizard" in actor.privileges:
            self.__inventory.remove(item)
            item.contained_in = None
        else:
            raise ActionRefused("You can't take %s from %s." % (item.title, self.title))

    def destroy(self, ctx: util.Context) -> None:
        super().destroy(ctx)
        if self.location and self in self.location.livings:
            self.location.livings.remove(self)
        self.location = None
        for item in self.__inventory:
            item.destroy(ctx)
        self.__inventory.clear()
        # @todo: remove attack status, etc.
        self.soul = None   # truly die ;-)

    @util.authorized("wizard")
    def wiz_clone(self, actor: 'Living') -> 'Living':
        duplicate = copy.deepcopy(self)
        actor.tell("Cloned into: " + repr(duplicate))
        actor.tell_others("{Title} summons %s..." % lang.a(duplicate.title))
        actor.location.insert(duplicate, actor)
        actor.location.tell("%s appears." % lang.capital(duplicate.title))
        return duplicate

    @util.authorized("wizard")
    def wiz_destroy(self, actor: 'Living', ctx: util.Context) -> None:
        if self is actor:
            raise ActionRefused("You can't destroy yourself, are you insane?!")
        self.tell("%s creates a black hole that sucks you up. You're utterly destroyed." % lang.capital(actor.title))
        self.destroy(ctx)

    def show_inventory(self, actor: 'Living', ctx: util.Context) -> None:
        """show the living's inventory to the actor"""
        name = lang.capital(self.title)
        if self.inventory:
            actor.tell(name, "is carrying:", end=True)
            for item in self.inventory:
                actor.tell("  " + item.title, format=False)
        else:
            actor.tell(name, "is carrying nothing.")
        if ctx.config.money_type:
            actor.tell("Money in possession: %s." % ctx.driver.moneyfmt.display(self.money))

    def get_wiretap(self) -> pubsub.Topic:
        """get a wiretap for this living"""
        return pubsub.topic(("wiretap-living", self.name))

    def tell(self, *messages: str, **kwargs: Any) -> 'Living':
        """
        Every living thing in the mud can receive one or more action messages.
        For players this is usually printed to their screen, but for all other
        livings the default is to do nothing.
        They could react on it but this is not advisable because you will need
        to parse the string again to figure out what happened...
        kwargs is ignored for Livings.
        The object self is returned so you can chain calls.
        """
        msg = " ".join(str(msg) for msg in messages)
        tap = self.get_wiretap()
        tap.send((self.name, msg))
        return self

    def tell_later(self, *messages: str, **kwargs: Any) -> None:
        """Tell something to this actor, but do it after other messages."""
        pending_tells.send(lambda: self.tell(*messages, **kwargs))

    def tell_others(self, *messages: str) -> None:
        """
        Message(s) sent to the other livings in the location, but not to self.
        There are a few formatting strings for easy shorthands:
        {title}/{Title} = the living's title, and the title with a capital letter.
        If you need even more tweaks with telling stuff, use living.location.tell directly.
        """
        formats = {"title": self.title, "Title": lang.capital(self.title)}
        for msg in messages:
            msg = msg.format(**formats)
            self.location.tell(msg, exclude_living=self)

    def parse(self, commandline: str, external_verbs: Set[str]=set()) -> ParseResult:
        """Parse the commandline into something that can be processed by the soul (ParseResult)"""
        if commandline == "again":
            # special case, repeat previous command
            if self.previous_commandline:
                commandline = self.previous_commandline
                self.tell("<dim>(repeat: %s)</>" % commandline, end=True)
            else:
                raise ActionRefused("Can't repeat your previous action.")
        self.previous_commandline = commandline
        parsed = self.soul.parse(self, commandline, external_verbs)
        self._previous_parse = parsed
        if external_verbs and parsed.verb in external_verbs:
            raise NonSoulVerb(parsed)
        if parsed.verb not in verbdefs.NONLIVING_OK_VERBS:
            # check if any of the targeted objects is a non-living
            if not all(isinstance(who, Living) for who in parsed.who_order):
                raise NonSoulVerb(parsed)
        self.validate_socialize_targets(parsed)
        return parsed

    def validate_socialize_targets(self, parsed: ParseResult) -> None:
        """check if any of the targeted objects is an exit"""
        if any(isinstance(w, Exit) for w in parsed.who_info):
            raise ParseError("That doesn't make much sense.")

    def remember_previous_parse(self) -> None:
        """remember the previously parsed data, soul uses this to reference back to earlier items/livings"""
        self.soul.remember_previous_parse(self._previous_parse)

    def do_socialize(self, cmdline: str, external_verbs: Set[str]=set()) -> None:
        """perform a command line with a socialize/soul verb on the living's behalf"""
        try:
            parsed = self.parse(cmdline, external_verbs=external_verbs)
            self.do_socialize_cmd(parsed)
        except UnknownVerbException as ex:
            if ex.verb == "say":
                # emulate the say command (which is not an emote, but it's convenient to be able to use it as such)
                verb, _, rest = cmdline.partition(" ")
                rest = rest.strip()
                self.tell_others("{Title} says: " + rest)
            else:
                raise

    def do_socialize_cmd(self, parsed: ParseResult) -> None:
        """
        A soul verb such as 'ponder' was entered. Socialize with the environment to handle this.
        Some verbs may trigger a response or action from something or someone else.
        """
        who, actor_message, room_message, target_message = self.soul.process_verb_parsed(self, parsed)
        self.tell(actor_message)
        self.location.tell(room_message, self, who, target_message)
        pending_actions.send(lambda actor=self: actor.location.notify_action(parsed, actor))
        if parsed.verb in verbdefs.AGGRESSIVE_VERBS:
            # usually monsters immediately attack,
            # other npcs may choose to attack or to ignore it
            # We need to check the verb qualifier, it might void the actual action :)
            if parsed.qualifier not in verbdefs.NEGATING_QUALIFIERS:
                for living in who:   # XXX can 'who' really also contain Location ????
                    if living.aggressive:
                        pending_actions.send(lambda victim=self: living.start_attack(victim))

    @util.authorized("wizard")
    def do_forced_cmd(self, actor: 'Living', parsed: ParseResult, ctx: util.Context) -> None:
        """
        Perform a (pre-parsed) command because the actor forced us to do it.

        This code is fairly similar to the __process_player_command from the driver
        but it doesn't deal with as many error situations, and just bails out if it gets confused.
        It does try its best to support the following:
        - custom location verbs (such as 'sell' in a shop)
        - exit handling
        - built-in cmds (such as 'drop'/'take')
        Note that soul emotes are handled by do_socialize_cmd instead.
        """
        try:
            if parsed.qualifier:
                raise ParseError("That action doesn't support qualifiers.")  # for now, quals are only supported on soul-verbs (emotes).
            custom_verbs = set(ctx.driver.current_custom_verbs(self))
            if parsed.verb in custom_verbs:
                if self.location.handle_verb(parsed, self):       # note: can't deal with async dialogs
                    pending_actions.send(lambda actor=self: actor.location.notify_action(parsed, actor))
                    return
                else:
                    raise ParseError("That custom verb is not understood by the environment.")
            if parsed.verb in self.location.exits:
                ctx.driver._go_through_exit(self, parsed.verb)
                return
            command_verbs = set(ctx.driver.current_verbs(self))
            if parsed.verb in command_verbs:
                # Here, one of the commands as annotated with @cmd (or @wizcmd) is executed
                func = ctx.driver.commands.get(self.privileges)[parsed.verb]
                if getattr(func, "is_generator", False):
                    dialog = func(self, parsed, ctx)
                    async_dialogs.send((ctx.conn, dialog))    # enqueue as async, and continue
                    return
                func(self, parsed, ctx)
                if func.enable_notify_action:
                    pending_actions.send(lambda actor=self: actor.location.notify_action(parsed, actor))
                return
            raise ParseError("Command not understood.")
        except Exception as x:
            actor.tell("Error result from forced cmd: " + str(x))

    def move(self, target: Union[Location, 'Container', 'Living'], actor: 'Living'=None,
             silent: bool=False, is_player: bool=False, verb: str="move") -> None:
        """
        Leave the current location, enter the new location (transactional).
        Moving a living is only supported to a Location target.
        Messages are being printed to the locations if the move was successful.
        """
        assert isinstance(target, Location), "can only move to a Location"
        actor = actor or self
        original_location = None
        if self.location:
            original_location = self.location
            self.location.remove(self, actor)
            try:
                target.insert(self, actor)
            except:
                # insert in target failed, put back in original location
                original_location.insert(self, actor)
                raise
            if not silent:
                original_location.tell("%s leaves." % lang.capital(self.title), exclude_living=self)
            # queue event
            if is_player:
                pending_actions.send(lambda who=self, where=target: original_location.notify_player_left(who, where))
            else:
                pending_actions.send(lambda who=self, where=target: original_location.notify_npc_left(who, where))
        else:
            target.insert(self, actor)
        if not silent:
            target.tell("%s arrives." % lang.capital(self.title), exclude_living=self)
        # queue event
        if is_player:
            pending_actions.send(lambda who=self, where=original_location: target.notify_player_arrived(who, where))
        else:
            pending_actions.send(lambda who=self, where=original_location: target.notify_npc_arrived(who, where))

    def search_item(self, name: str, include_inventory: bool=True,
                    include_location: bool=True, include_containers_in_inventory: bool=True) -> Item:
        """The same as locate_item except it only returns the item, or None."""
        item, container = self.locate_item(name, include_inventory, include_location, include_containers_in_inventory)
        return item  # skip the container

    def locate_item(self, name: str, include_inventory: bool=True, include_location: bool=True,
                    include_containers_in_inventory: bool=True) -> Tuple[Item, MudObject]:
        """
        Searches an item within the 'visible' world around the living including his inventory.
        If there's more than one hit, just return the first.
        Returns (None,None) or (item, containing_object)
        """
        if not name:
            raise ValueError("name must be given")
        found = None  # type: Item
        containing_object = None  # type: MudObject
        if include_inventory:
            containing_object = self
            found = Item.search_item(name, self.__inventory)
        if not found and include_location:
            containing_object = self.location
            found = Item.search_item(name, self.location.items)
        if not found and include_containers_in_inventory:
            # check if an item in the inventory might contain it
            for container in self.__inventory:
                containing_object = container
                try:
                    inventory = container.inventory
                except ActionRefused:
                    continue    # no access to inventory, just skip this item silently
                else:
                    found = Item.search_item(name, inventory)
                    if found:
                        break
        return (found, containing_object) if found else (None, None)

    def start_attack(self, victim: 'Living') -> None:
        """Starts attacking the given living until death ensues on either side."""
        # @todo: I'm not yet sure if the combat/attack logic should go here (on Living), or that it should be split across NPC / Player...
        # @TODO actual fight
        name = lang.capital(self.title)
        room_msg = "%s starts attacking %s!" % (name, victim.title)
        victim_msg = "%s starts attacking you!" % name
        attacker_msg = "You start attacking %s!" % victim.title
        victim.tell(victim_msg)
        victim.location.tell(room_msg, exclude_living=victim, specific_targets={self}, specific_target_msg=attacker_msg)

    def allow_give_money(self, actor: 'Living', amount: float) -> None:
        """Do we accept money? Raise ActionRefused if not."""
        if self.stats.race not in (None, "human"):
            raise ActionRefused("You can't do that.")

    def _handle_verb_base(self, parsed: ParseResult, actor: 'Living') -> bool:
        """
        Handle a custom verb. Return True if handled, False if not handled.
        Also checks inventory items. (Don't override this in a subclass,
        override handle_verb instead)
        """
        if self.handle_verb(parsed, actor):
            return True
        return any(item.handle_verb(parsed, actor) for item in self.__inventory)

    def handle_verb(self, parsed: ParseResult, actor: 'Living') -> bool:
        """Handle a custom verb. Return True if handled, False if not handled."""
        return False

    def _notify_action_base(self, parsed: ParseResult, actor: 'Living') -> None:
        """
        Notify the living of an action performed by someone.
        Also calls inventory items. Don't override this one in a subclass,
        override notify_action instead.
        """
        self.notify_action(parsed, actor)
        for item in self.__inventory:
            item.notify_action(parsed, actor)

    def notify_action(self, parsed: ParseResult, actor: 'Living') -> None:
        """Notify the living of an action performed by someone."""
        pass

    def look(self, short: bool=None) -> None:
        """look around in your surroundings. Dummy for base livings."""
        pass

    def select_random_move(self) -> Optional['Exit']:
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


class Container(Item):
    """
    A bag-type container (i.e. an item that acts as a container)
    Allows insert and remove, and examine its contents, as opposed to an Item
    You can test for containment with 'in': item in bag
    """
    def init(self) -> None:
        super().init()
        self.__inventory = set()   # type: Set[Item]

    def init_inventory(self, items: Iterable[Item]) -> None:
        """Set the container's initial inventory"""
        assert len(self.__inventory) == 0
        self.__inventory = set(items)
        for item in items:
            item.contained_in = self

    @property
    def inventory(self) -> FrozenSet[Item]:
        return frozenset(self.__inventory)

    @property
    def inventory_size(self) -> int:
        return len(self.__inventory)

    def __contains__(self, item: Item) -> bool:
        return item in self.__inventory

    def destroy(self, ctx: util.Context) -> None:
        super().destroy(ctx)
        for item in self.__inventory:
            item.destroy(ctx)
        self.__inventory.clear()

    def insert(self, item: Union[Living, Item], actor: Optional[Living]) -> None:
        assert item is not None
        if not isinstance(item, Item):
            raise ActionRefused("You can't do that.")
        self.__inventory.add(item)
        item.contained_in = self

    def remove(self, item: Union[Living, Item], actor: Optional[Living]) -> None:
        assert item is not None
        if not isinstance(item, Item):
            raise ActionRefused("You can't do that.")
        self.__inventory.remove(item)
        item.contained_in = None


class Exit(MudObject):
    """
    An 'exit' that connects one location to another. It is strictly one-way.
    Directions can be a single string or a sequence of directions (all meaning the same exit).
    You can use a Location object as target, or a string designating the location
    (for instance "town.square" means the square location object in game.zones.town).
    If using a string, it will be retrieved and bound at runtime.
    Short_description will be shown when the player looks around the room.
    Long_description is optional and will be shown instead if the player examines the exit.
    The exit's direction is stored as its name attribute (if more than one, the rest are aliases).
    Note that the exit's origin is not stored in the exit object.
    """
    def __init__(self, directions: Union[str, Sequence[str]], target_location: Union[str, Location],
                 short_description: str, long_description: str=None) -> None:
        assert isinstance(target_location, (Location, str)), "target must be a Location or a string"
        if isinstance(directions, str):
            direction = directions
            aliases = set()  # type: Set[str]
        else:
            direction = directions[0]
            aliases = set(directions[1:])
        self.target = None  # type: Location
        if isinstance(target_location, Location):
            self.target = target_location
            title = "Exit to " + self.target.title
        else:
            self._target_str = target_location
            title = "Exit to <unbound:%s>" % target_location
        long_description = long_description or short_description
        super().__init__(direction, title=title, description=long_description, short_description=short_description)
        self.aliases = aliases
        # The driver needs to know about all exits,
        # it will hook them all up once initialization is complete.
        mud_context.driver.register_exit(self)

    def __repr__(self):
        targetname = self.target.name if self.target else self._target_str
        return "<base.Exit to '%s' @ 0x%x>" % (targetname, id(self))

    def bind(self, location: Location) -> None:
        """Binds the exit to a location."""
        assert isinstance(location, Location)
        directions = self.aliases | {self.name}
        for direction in directions:
            if direction in location.exits:
                raise LocationIntegrityError("exit already exists: '%s' in %s" % (direction, location), direction, self, location)
            location.exits[direction] = self

    def _bind_target(self, game_zones_module: ModuleType) -> None:
        """
        Binds the exit to the actual target_location object.
        Usually called by the driver before it starts player interaction.
        The caller needs to pass in the root module of the game zones (to avoid circular import dependencies)
        """
        if not self.target:
            target_module, target_object = self._target_str.rsplit(".", 1)
            module = game_zones_module
            try:
                for name in target_module.split("."):
                    module = getattr(module, name)
                target = getattr(module, target_object)
            except AttributeError:
                raise AttributeError("exit target error, cannot find target: '%s.%s' in exit: '%s'" %
                                     (target_module, target_object, self.short_description))
            assert isinstance(target, Location)
            self.target = target
            self.title = "Exit to " + target.title
            self.name = self.title.lower()
            del self._target_str

    def allow_passage(self, actor: Living) -> None:
        """Is the actor allowed to move through the exit? Raise ActionRefused if not"""
        if not self.target:
            raise LocationIntegrityError("exit not bound", None, self, None)

    def open(self, actor: Living, item: Item=None) -> None:
        raise ActionRefused("You can't open that.")

    def close(self, actor: Living, item: Item=None) -> None:
        raise ActionRefused("You can't close that.")

    def lock(self, actor: Living, item: Item=None) -> None:
        raise ActionRefused("You can't lock that.")

    def unlock(self, actor: Living, item: Item=None) -> None:
        raise ActionRefused("You can't unlock that.")

    def manipulate(self, verb: str, actor: Living) -> None:
        # override from base to print a special error message
        raise ActionRefused("It makes no sense to %s in that direction." % verb)


class Door(Exit):
    """
    A special exit that connects one location to another but which can be closed or even locked.
    """
    def __init__(self, directions: Union[str, Sequence[str]], target_location: Union[str, Location], short_description: str,
                 long_description: str=None, locked: bool=False, opened: bool=True) -> None:
        self.locked = locked
        self.opened = opened
        self.__description_prefix = long_description or short_description
        self.key_code = None   # type: Any  # you can optionally set this to any code that a key must match to unlock the door
        super().__init__(directions, target_location, short_description, long_description)
        if locked and opened:
            raise ValueError("door cannot be both locked and opened")
        self.linked_door = None  # type: Door.DoorPairLink

    class DoorPairLink:
        def __init__(self, other_door: 'Door', other_open_msg: str=None, other_close_msg: str=None) -> None:
            self.door = other_door
            self.open_msg = other_open_msg
            self.close_msg = other_close_msg

    def reverse_door(self, directions: Union[str, Sequence[str]], returning_location: Location,
                     short_description: str, long_description: str=None,
                     reverse_open_msg: str=None, reverse_close_msg: str=None,
                     this_open_msg: str=None, this_close_msg: str=None) -> 'Door':
        """
        Set up a second door in the other location that is paired with this door.
        Opening this door will also open the other door etc.    Returns the new door object.
        """
        other_door = Door(directions, returning_location, short_description, long_description, locked=self.locked, opened=self.opened)
        self.linked_door = Door.DoorPairLink(other_door, this_open_msg, this_close_msg)
        other_door.linked_door = Door.DoorPairLink(self, reverse_open_msg, reverse_close_msg)
        other_door.key_code = self.key_code
        return other_door

    @property
    def description(self) -> str:
        if self.opened:
            status = "It is open "
        else:
            status = "It is closed "
        if self.locked:
            status += "and locked."
        else:
            status += "and unlocked."
        return self.__description_prefix + " " + status

    @description.setter
    def description(self, value: str):
        raise TaleError("you cannot set the description of a Door because it is dynamic")

    def __repr__(self):
        target = self.target.name if self.target else self._target_str
        locked = "locked" if self.locked else "open"
        return "<base.Door '%s'->'%s' (%s) @ 0x%x>" % (self.name, target, locked, id(self))

    def allow_passage(self, actor: Living) -> None:
        """Is the actor allowed to move through this door?"""
        if not self.target:
            raise LocationIntegrityError("door not bound", None, self, None)
        if not self.opened:
            raise ActionRefused("You can't go there; it's closed.")

    def open(self, actor: Living, item: Item=None) -> None:
        """Open the door with optional item. Notifies actor and room of this event."""
        if self.opened:
            raise ActionRefused("It's already open.")
        elif self.locked:
            raise ActionRefused("You try to open it, but it's locked.")
        else:
            self.opened = True
            actor.tell("You opened it.")
            actor.tell_others("{Title} opened the %s." % self.name)
            if self.linked_door:
                self.linked_door.door.opened = True
                if self.linked_door.open_msg:
                    self.target.tell(self.linked_door.open_msg)

    def close(self, actor: Living, item: Item=None) -> None:
        """Close the door with optional item. Notifies actor and room of this event."""
        if not self.opened:
            raise ActionRefused("It's already closed.")
        self.opened = False
        actor.tell("You closed it.")
        actor.tell_others("{Title} closed the %s." % self.name)
        if self.linked_door:
            self.linked_door.door.opened = False
            if self.linked_door.close_msg:
                self.target.tell(self.linked_door.close_msg)

    def lock(self, actor: Living, item: Item=None) -> None:
        """Lock the door with the proper key (optional)."""
        if self.locked:
            raise ActionRefused("It's already locked.")
        if item:
            if self.check_key(item):
                key = item
            else:
                raise ActionRefused("You can't use that to lock it.")
        else:
            key = self.search_key(actor)
            if key:
                actor.tell("<dim>(You use your %s; %s matches the lock.)</>" % (key.title, key.subjective))
            else:
                raise ActionRefused("You don't seem to have the means to lock it.")
        self.locked = True
        actor.tell("Your %s fits, it is now locked." % key.title)
        actor.tell_others("{Title} locked the %s with %s." % (self.name, lang.a(key.title)))
        if self.linked_door:
            self.linked_door.door.locked = True

    def unlock(self, actor: Living, item: Item=None) -> None:
        """Unlock the door with the proper key (optional)."""
        if not self.locked:
            raise ActionRefused("It's not locked.")
        if item:
            if self.check_key(item):
                key = item
            else:
                raise ActionRefused("You can't use that to unlock it.")
        else:
            key = self.search_key(actor)
            if key:
                actor.tell("<dim>(You use your %s; %s matches the lock.)</>" % (key.title, key.subjective))
            else:
                raise ActionRefused("You don't seem to have the means to unlock it.")
        self.locked = False
        actor.tell("Your %s fits, it is now unlocked." % key.title)
        actor.tell_others("{Title} unlocked the %s with %s." % (self.name, lang.a(key.title)))
        if self.linked_door:
            self.linked_door.door.locked = False

    def check_key(self, item: Item) -> bool:
        """Check if the item is a proper key for this door (based on key_code)"""
        key_code = getattr(item, "key_code", None)
        if self.linked_door:
            # if this door has a linked door, it could be that the key_code was set on the other door.
            # in that case, copy the key code from the other door.
            other_code = self.linked_door.door.key_code
            if self.key_code is None:
                self.key_code = other_code
            else:
                assert self.key_code == other_code, "door key codes must match"
        return key_code and key_code == self.key_code

    def search_key(self, actor: Living) -> Optional[Item]:
        """Does the actor have a proper key? Return the item if so, otherwise return None."""
        for item in actor.inventory:
            if self.check_key(item):
                return item
        return None

    def insert(self, item: Union[Living, Item], actor: Optional[Living]) -> None:
        """used when the player tries to put a key into the door, for instance."""
        assert item is not None
        if not isinstance(item, Item):
            raise ActionRefused("You can't do that.")
        if self.check_key(item):
            if self.locked:
                raise ActionRefused("You could try to unlock the door with it instead.")
            else:
                raise ActionRefused("You could try to lock the door with it instead.")
        raise ActionRefused("The %s doesn't fit." % item.title)


class Key(Item):
    """A key which has a unique code. It can be used to open the matching Door."""
    def init(self) -> None:
        super().init()
        self.key_code = None  # type: str

    def key_for(self, door: Door=None, code: str=None) -> None:
        """Makes this key a key for the given door. (basically just copies the door's key_code)"""
        if code:
            assert door is None
            self.key_code = code
        else:
            self.key_code = door.key_code
            if not self.key_code:
                raise LocationIntegrityError("door has no key_code set", None, door, door.target)


class Soul:
    """
    The 'soul' of a Living (most importantly, a Player).
    Handles the high level verb actions and allows for social player interaction.
    Verbs that actually do something in the environment (not purely social messages) are implemented elsewhere.
    """

    _quoted_message_regex = re.compile(r"('(?P<msg1>.*)')|(\"(?P<msg2>.*)\")")  # greedy single-or-doublequoted string match
    _skip_words = {"and", "&", "at", "to", "before", "in", "into", "on", "off", "onto",
                   "the", "with", "from", "after", "before", "under", "above", "next"}

    def __init__(self) -> None:
        self.__previously_parsed = None  # type: ParseResult

    def is_verb(self, verb: str) -> bool:
        return verb in verbdefs.VERBS

    def process_verb(self, player, commandstring: str, external_verbs: Set[str]=set()) \
            -> Tuple[str, Tuple[Set[Union[Location, Living]], str, str, str]]:
        """
        Parse a command string and return a tuple containing the main verb (tickle, ponder, ...)
        and another tuple containing the targets of the action and the various action messages.
        Any action qualifier is added to the verb string if it is present ("fail kick").
        """
        parsed = self.parse(player, commandstring, external_verbs)
        if parsed.verb in external_verbs:
            raise NonSoulVerb(parsed)
        result = self.process_verb_parsed(player, parsed)
        if parsed.qualifier:
            verb = parsed.qualifier + " " + parsed.verb
        else:
            verb = parsed.verb
        return verb, result

    def process_verb_parsed(self, player, parsed: ParseResult) -> Tuple[Set[Union[Location, Living]], str, str, str]:
        """
        This function takes a verb and the arguments given by the user,
        creates various display messages that can be sent to the players and room,
        and returns a tuple: (targets-without-player, playermessage, roommessage, targetmessage)
        """
        if not player:
            raise TaleError("no player in process_verb_parsed")
        verbdata = verbdefs.VERBS.get(parsed.verb)
        if not verbdata:
            raise UnknownVerbException(parsed.verb, None, parsed.qualifier)

        message = parsed.message
        adverb = parsed.adverb

        vtype = verbdata[0]
        if not message and verbdata[1] and len(verbdata[1]) > 1:
            message = verbdata[1][1]  # get the message from the verbs table
        if message:
            if message.startswith("'"):
                # use the message without single quotes around it
                msg = message = self.spacify(message[1:])
            else:
                msg = " '" + message + "'"
                message = " " + message
        else:
            msg = message = ""
        if not adverb:
            if verbdata[1]:
                adverb = verbdata[1][0]    # normal-adverb
            else:
                adverb = ""
        where = ""
        if parsed.bodypart:
            where = " " + verbdefs.BODY_PARTS[parsed.bodypart]
        elif not parsed.bodypart and verbdata[1] and len(verbdata[1]) > 2 and verbdata[1][2]:
            where = " " + verbdata[1][2]  # replace bodyparts string by specific one from verbs table
        how = self.spacify(adverb)

        def result_messages(action: str, action_room: str) -> Tuple[Set[Union[Location, Living]], str, str, str]:
            action = action.strip()
            action_room = action_room.strip()
            if parsed.qualifier:
                qual_action, qual_room, use_room_default = verbdefs.ACTION_QUALIFIERS[parsed.qualifier]
                action_room = qual_room % action_room if use_room_default else qual_room % action
                action = qual_action % action
            # construct message seen by player
            targetnames = [self.who_replacement(player, target, player) for target in parsed.who_order]
            player_msg = action.replace(" \nWHO", " " + lang.join(targetnames))
            player_msg = player_msg.replace(" \nYOUR", " your")
            player_msg = player_msg.replace(" \nMY", " your")
            # construct message seen by room
            targetnames = [self.who_replacement(player, target, None) for target in parsed.who_order]
            room_msg = action_room.replace(" \nWHO", " " + lang.join(targetnames))
            room_msg = room_msg.replace(" \nYOUR", " " + player.possessive)
            room_msg = room_msg.replace(" \nMY", " " + player.objective)
            # construct message seen by targets
            target_msg = action_room.replace(" \nWHO", " you")
            target_msg = target_msg.replace(" \nYOUR", " " + player.possessive)
            target_msg = target_msg.replace(" \nPOSS", " your")
            target_msg = target_msg.replace(" \nIS", " are")
            target_msg = target_msg.replace(" \nSUBJ", " you")
            target_msg = target_msg.replace(" \nMY", " " + player.objective)
            # fix up POSS, IS, SUBJ in the player and room messages
            if len(parsed.who_order) == 1:
                only_living = parsed.who_order[0]
                subjective = getattr(only_living, "subjective", "it")  # if no subjective attr, use "it"
                player_msg = player_msg.replace(" \nIS", " is")
                player_msg = player_msg.replace(" \nSUBJ", " " + subjective)
                player_msg = player_msg.replace(" \nPOSS", " " + Soul.poss_replacement(player, only_living, player))
                room_msg = room_msg.replace(" \nIS", " is")
                room_msg = room_msg.replace(" \nSUBJ", " " + subjective)
                room_msg = room_msg.replace(" \nPOSS", " " + Soul.poss_replacement(player, only_living, None))
            else:
                targetnames_player = lang.join([Soul.poss_replacement(player, living, player) for living in parsed.who_order])
                targetnames_room = lang.join([Soul.poss_replacement(player, living, None) for living in parsed.who_order])
                player_msg = player_msg.replace(" \nIS", " are")
                player_msg = player_msg.replace(" \nSUBJ", " they")
                player_msg = player_msg.replace(" \nPOSS", " " + lang.possessive(targetnames_player))
                room_msg = room_msg.replace(" \nIS", " are")
                room_msg = room_msg.replace(" \nSUBJ", " they")
                room_msg = room_msg.replace(" \nPOSS", " " + lang.possessive(targetnames_room))
            # add fullstops at the end
            player_msg = lang.fullstop("You " + player_msg)
            room_msg = lang.capital(lang.fullstop(player.title + " " + room_msg))
            target_msg = lang.capital(lang.fullstop(player.title + " " + target_msg))
            if player in parsed.who_info:
                who = set(parsed.who_info)
                who.remove(player)  # the player should not be part of the remaining targets.
                whof = set(who)
            else:
                whof = set(parsed.who_info)
            return whof, player_msg, room_msg, target_msg

        # construct the action string
        action = None
        if vtype == verbdefs.DEUX:
            action = verbdata[2]
            action_room = verbdata[3]
            if not self.check_person(action, parsed.who_order):
                raise ParseError("The verb %s needs a person." % parsed.verb)
            action = action.replace(" \nWHERE", where)
            action_room = action_room.replace(" \nWHERE", where)
            action = action.replace(" \nWHAT", message)
            action = action.replace(" \nMSG", msg)
            action_room = action_room.replace(" \nWHAT", message)
            action_room = action_room.replace(" \nMSG", msg)
            action = action.replace(" \nHOW", how)
            action_room = action_room.replace(" \nHOW", how)
            return result_messages(action, action_room)
        elif vtype == verbdefs.QUAD:
            if parsed.who_info:
                action = verbdata[4]
                action_room = verbdata[5]
            else:
                action = verbdata[2]
                action_room = verbdata[3]
            action = action.replace(" \nWHERE", where)
            action_room = action_room.replace(" \nWHERE", where)
            action = action.replace(" \nWHAT", message)
            action = action.replace(" \nMSG", msg)
            action_room = action_room.replace(" \nWHAT", message)
            action_room = action_room.replace(" \nMSG", msg)
            action = action.replace(" \nHOW", how)
            action_room = action_room.replace(" \nHOW", how)
            return result_messages(action, action_room)
        elif vtype == verbdefs.FULL:
            raise TaleError("vtype verbdefs.FULL")  # doesn't matter, verbdefs.FULL is not used yet anyway
        elif vtype == verbdefs.DEFA:
            action = parsed.verb + "$ \nHOW \nAT"
        elif vtype == verbdefs.PREV:
            action = parsed.verb + "$" + self.spacify(verbdata[2]) + " \nWHO \nHOW"
        elif vtype == verbdefs.PHYS:
            action = parsed.verb + "$" + self.spacify(verbdata[2]) + " \nWHO \nHOW \nWHERE"
        elif vtype == verbdefs.SHRT:
            action = parsed.verb + "$" + self.spacify(verbdata[2]) + " \nHOW"
        elif vtype == verbdefs.PERS:
            action = verbdata[3] if parsed.who_order else verbdata[2]
        elif vtype == verbdefs.SIMP:
            action = verbdata[2]
        else:
            raise TaleError("invalid vtype " + vtype)

        if parsed.who_info and len(verbdata) > 3:
            action = action.replace(" \nAT", self.spacify(verbdata[3]) + " \nWHO")
        else:
            action = action.replace(" \nAT", "")

        if not self.check_person(action, parsed.who_order):
            raise ParseError("The verb %s needs a person." % parsed.verb)

        action = action.replace(" \nHOW", how)
        action = action.replace(" \nWHERE", where)
        action = action.replace(" \nWHAT", message)
        action = action.replace(" \nMSG", msg)
        action_room = action
        action = action.replace("$", "")
        action_room = action_room.replace("$", "s")
        return result_messages(action, action_room)

    def parse(self, player, cmd: str, external_verbs: Set[str]=set()) -> ParseResult:
        """Parse a command string, returns a ParseResult object."""
        qualifier = None
        message_verb = False  # does the verb expect a message?
        external_verb = False  # is it a non-soul verb?
        adverb = None   # type: Optional[str]
        message = []  # type: List[str]
        bodypart = None   # type: str
        arg_words = []  # type: List[str]
        unrecognized_words = []   # type: List[str]
        who_info = defaultdict(ParseResult.WhoInfo)   # type: Dict[Any, ParseResult.WhoInfo]
        who_order = []   # type: List[Any]
        who_sequence = 0
        unparsed = cmd

        # a substring enclosed in quotes will be extracted as the message
        m = self._quoted_message_regex.search(cmd)
        if m:
            message = [(m.group("msg1") or m.group("msg2")).strip()]
            cmd = cmd[:m.start()] + cmd[m.end():]

        if not cmd:
            raise ParseError("What?")
        words = cmd.split()
        if words[0] in verbdefs.ACTION_QUALIFIERS:     # suddenly, fail, ...
            qualifier = words.pop(0)
            unparsed = unparsed[len(qualifier):].lstrip()
            if qualifier == "dont":
                qualifier = "don't"  # little spelling suggestion
            # note: don't add qualifier to arg_words
        if words and words[0] in self._skip_words:
            skipword = words.pop(0)
            unparsed = unparsed[len(skipword):].lstrip()

        if not words:
            raise ParseError("What?")
        verb = None
        if words[0] in external_verbs:    # external verbs have priority above soul verbs
            verb = words.pop(0)
            external_verb = True
            # note: don't add verb to arg_words
        elif words[0] in verbdefs.VERBS:
            verb = words.pop(0)
            verbdata = verbdefs.VERBS[verb][2]
            message_verb = "\nMSG" in verbdata or "\nWHAT" in verbdata
            # note: don't add verb to arg_words
        elif player.location.exits:
            # check if the words are the name of a room exit.
            move_action = None
            if words[0] in verbdefs.MOVEMENT_VERBS:
                move_action = words.pop(0)
                if not words:
                    raise ParseError("%s where?" % lang.capital(move_action))
            exit, exit_name, wordcount = self.check_name_with_spaces(words, 0, player.location.exits, {})
            if exit:
                if wordcount != len(words):
                    raise ParseError("What do you want to do with that?")
                unparsed = unparsed[len(exit_name):].lstrip()
                raise NonSoulVerb(ParseResult(verb=exit_name, who_order=[exit], qualifier=qualifier, unparsed=unparsed))
            elif move_action:
                raise ParseError("You can't %s there." % move_action)
            else:
                # can't determine verb at this point, just continue with verb=None
                pass
        else:
            # can't determine verb at this point, just continue with verb=None
            pass

        if verb:
            unparsed = unparsed[len(verb):].lstrip()
        include_flag = True
        collect_message = False
        all_livings = {}  # livings in the room (including player) by name + aliases
        all_items = {}  # all items in the room or player's inventory, by name + aliases
        for living in player.location.livings:
            all_livings[living.name] = living
            for alias in living.aliases:
                all_livings[alias] = living
        for item in player.location.items:
            all_items[item.name] = item
            for alias in item.aliases:
                all_items[alias] = item
        for item in player.inventory:
            all_items[item.name] = item
            for alias in item.aliases:
                all_items[alias] = item
        previous_word = None
        words_enumerator = enumerate(words)
        for index, word in words_enumerator:
            if collect_message:
                message.append(word)
                arg_words.append(word)
                previous_word = word
                continue
            if not message_verb and not collect_message:
                word = word.rstrip(",")
            if word in ("them", "him", "her", "it"):
                if self.__previously_parsed:
                    # try to connect the pronoun to a previously parsed item/living
                    who_list = self.match_previously_parsed(player, word)
                    if who_list:
                        for who, name in who_list:
                            if include_flag:
                                who_info[who].sequence = who_sequence
                                who_info[who].previous_word = previous_word
                                who_sequence += 1
                                who_order.append(who)
                            else:
                                del who_info[who]
                                who_order.remove(who)
                            arg_words.append(name)  # put the replacement-name in the args instead of the pronoun
                    previous_word = None
                    continue
                raise ParseError("It is not clear who you mean.")
            if word in ("me", "myself", "self"):
                if include_flag:
                    who_info[player].sequence = who_sequence
                    who_info[player].previous_word = previous_word
                    who_sequence += 1
                    who_order.append(player)
                elif player in who_info:
                    del who_info[player]
                    who_order.remove(player)
                arg_words.append(word)
                previous_word = None
                continue
            if word in verbdefs.BODY_PARTS:
                if bodypart:
                    raise ParseError("You can't do that both %s and %s." % (verbdefs.BODY_PARTS[bodypart], verbdefs.BODY_PARTS[word]))
                bodypart = word
                arg_words.append(word)
                continue
            if word in ("everyone", "everybody", "all"):
                if include_flag:
                    if not all_livings:
                        raise ParseError("There is nobody here.")
                    # include every *living* thing visible, don't include items, and skip the player itself
                    for living in player.location.livings:
                        if living is not player:
                            who_info[living].sequence = who_sequence
                            who_info[living].previous_word = previous_word
                            who_sequence += 1
                            who_order.append(living)
                else:
                    who_info = {}
                    who_order = []
                    who_sequence = 0
                arg_words.append(word)
                previous_word = None
                continue
            if word == "everything":
                raise ParseError("You can't do something to everything around you, be more specific.")
            if word in ("except", "but"):
                include_flag = not include_flag
                arg_words.append(word)
                continue
            if word in lang.ADVERBS:
                if adverb:
                    raise ParseError("You can't do that both %s and %s." % (adverb, word))
                adverb = word
                arg_words.append(word)
                continue
            if word in all_livings:
                living = all_livings[word]
                if include_flag:
                    who_info[living].sequence = who_sequence
                    who_info[living].previous_word = previous_word
                    who_sequence += 1
                    who_order.append(living)
                elif living in who_info:
                    del who_info[living]
                    who_order.remove(living)
                arg_words.append(word)
                previous_word = None
                continue
            if word in all_items:
                item = all_items[word]
                if include_flag:
                    who_info[item].sequence = who_sequence
                    who_info[item].previous_word = previous_word
                    who_sequence += 1
                    who_order.append(item)
                elif item in who_info:
                    del who_info[item]
                    who_order.remove(item)
                arg_words.append(word)
                previous_word = None
                continue
            if player.location:
                exit, exit_name, wordcount = self.check_name_with_spaces(words, index, player.location.exits, {})
                if exit:
                    who_info[exit].sequence = who_sequence     # XXX exit is not a MudObject???
                    who_info[exit].previous_word = previous_word
                    previous_word = None
                    who_sequence += 1
                    who_order.append(exit)  # XXX who_order contains Livings not str!
                    arg_words.append(exit_name)
                    while wordcount > 1:
                        next(words_enumerator)
                        wordcount -= 1
                    continue
            item, full_name, wordcount = self.check_name_with_spaces(words, index, all_livings, all_items)
            if item:
                while wordcount > 1:
                    next(words_enumerator)
                    wordcount -= 1
                if include_flag:
                    who_info[item].sequence = who_sequence
                    who_info[item].previous_word = previous_word
                    who_sequence += 1
                    who_order.append(item)
                elif item in who_info:
                    del who_info[item]
                    who_order.remove(item)
                arg_words.append(full_name)
                previous_word = None
                continue
            if message_verb and not message:
                collect_message = True
                message.append(word)
                arg_words.append(word)
                continue
            if word not in self._skip_words:
                # unrecognized word, check if it could be a person's name or an item. (prefix)
                if not who_order:
                    for name in all_livings:
                        if name.startswith(word):
                            raise ParseError("Perhaps you meant %s?" % name)
                    for name in all_items:
                        if name.startswith(word):
                            raise ParseError("Perhaps you meant %s?" % name)
                if not external_verb:
                    if not verb:
                        raise UnknownVerbException(word, words, qualifier)
                    # check if it is a prefix of an adverb, if so, suggest a few adverbs
                    adverbs = lang.adverb_by_prefix(word)
                    if len(adverbs) == 1:
                        word = adverbs[0]
                        if adverb:
                            raise ParseError("You can't do that both %s and %s." % (adverb, word))
                        adverb = word
                        arg_words.append(word)
                        previous_word = word
                        continue
                    elif len(adverbs) > 1:
                        raise ParseError("What adverb did you mean: %s?" % lang.join(adverbs, conj="or"))

                if external_verb:
                    arg_words.append(word)
                    unrecognized_words.append(word)
                else:
                    if word in verbdefs.VERBS or word in verbdefs.ACTION_QUALIFIERS or word in verbdefs.BODY_PARTS:
                        # in case of a misplaced verb, qualifier or bodypart give a little more specific error
                        raise ParseError("The word %s makes no sense at that location." % word)
                    else:
                        # no idea what the user typed, generic error
                        errormsg = "It's not clear what you mean by '%s'." % word
                        if word[0].isupper():
                            errormsg += " Just type in lowercase ('%s')." % word.lower()
                        raise ParseError(errormsg)
            previous_word = word

        message_text = " ".join(message)
        if not verb:
            # This is interesting: there's no verb.
            # but maybe the thing the user typed refers to an object or creature.
            # In that case, set the verb to that object's default verb.
            if len(who_order) == 1:
                try:
                    verb = who_order[0].default_verb
                except AttributeError:
                    verb = "examine"   # fallback for everything that hasn't explicitly set a default_verb
            else:
                raise UnknownVerbException(words[0], words, qualifier)
        return ParseResult(verb, who_info=who_info, who_order=who_order,
                           adverb=adverb, message=message_text, bodypart=bodypart, qualifier=qualifier,
                           args=arg_words, unrecognized=unrecognized_words, unparsed=unparsed)

    def remember_previous_parse(self, parsed: ParseResult) -> None:
        self.__previously_parsed = parsed

    def match_previously_parsed(self, player, pronoun: str) -> List[Tuple[Any, str]]:
        """
        Try to connect the pronoun (it, him, her, them) to a previously parsed item/living.
        Returns a list of (who, replacement-name) tuples.
        The reason we return a replacement-name is that the parser can replace the
        pronoun by the proper name that would otherwise have been used in that place.
        """
        if pronoun == "them":
            # plural (any item/living qualifies)
            matches = list(self.__previously_parsed.who_order)
            for who in matches:
                if not player.search_item(who.name) and who not in player.location.livings:
                    player.tell("<dim>(By '%s', it is assumed you meant %s.)</>" % (pronoun, who.title))
                    raise ParseError("%s is no longer around." % lang.capital(who.subjective))
            if matches:
                player.tell("<dim>(By '%s', it is assumed you mean: %s.)" % (pronoun, lang.join(who.title for who in matches)))
                return [(who, who.name) for who in matches]
            else:
                raise ParseError("It is not clear who you're referring to.")
        for who in self.__previously_parsed.who_order:
            # first see if it is an exit
            if pronoun == "it":
                for direction, exit in player.location.exits.items():
                    if exit is who:
                        player.tell("<dim>(By '%s', it is assumed you mean '%s'.)</>" % (pronoun, direction))
                        return [(who, direction)]
            # not an exit, try an item or a living
            if pronoun == who.objective:
                if player.search_item(who.name) or who in player.location.livings:
                    player.tell("<dim>(By '%s', it is assumed you mean %s.)</>" % (pronoun, who.title))
                    return [(who, who.name)]
                player.tell("<dim>(By '%s', it is assumed you meant %s.)</>" % (pronoun, who.title))
                raise ParseError("%s is no longer around." % lang.capital(who.subjective))
        raise ParseError("It is not clear who you're referring to.")

    @staticmethod
    def poss_replacement(actor: Living, target: MudObject, observer: Optional[Living]) -> str:
        """determines what word to use for a POSS"""
        if target is actor:
            if actor is observer:
                return "your own"       # your own foot
            else:
                return actor.possessive + " own"   # his own foot
        else:
            if target is observer:
                return "your"           # your foot
            else:
                return lang.possessive(target.title)

    def spacify(self, string: str) -> str:
        """returns string prefixed with a space, if it has contents. If it is empty, prefix nothing"""
        return " " + string.lstrip(" \t") if string else ""

    def who_replacement(self, actor: Living, target: MudObject, observer: Optional[Living]) -> str:
        """determines what word to use for a WHO"""
        if target is actor:
            if actor is observer:
                return "yourself"       # you kick yourself
            else:
                return actor.objective + "self"    # ... kicks himself
        else:
            if target is observer:
                return "you"            # ... kicks you
            else:
                return target.title      # ... kicks ...

    def check_person(self, action: str, who: Sequence[str]) -> bool:
        if not who and ("\nWHO" in action or "\nPOSS" in action):
            return False
        return True

    def check_name_with_spaces(self, words: Sequence[str], index: int, all_livings: Dict[str, Any],
                               all_items: Dict[str, Any]) -> Tuple[Optional[int], Optional[str], int]:   # XXX return type
        wordcount = 1
        name = words[index]
        try:
            while wordcount < 6:    # an upper bound for the number of words to concatenate to avoid long runtime
                if name in all_livings:
                    return all_livings[name], name, wordcount
                if name in all_items:
                    return all_items[name], name, wordcount
                name = name + " " + words[index + wordcount]
                wordcount += 1
        except IndexError:
            pass
        return None, None, 0
