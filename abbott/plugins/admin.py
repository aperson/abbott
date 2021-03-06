from collections import defaultdict, deque
import time
import re
import random

from twisted.internet import reactor
from twisted.python import log
from twisted.internet import defer

from ..command import CommandPluginSuperclass, require_channel
from ..transport import Event
from . import ircutil
from . import ircop

duration_match = r"(?P<duration>\d+[dhmsw])"
def parse_time(timestr):
    duration = 0
    multipliers = {
            's': 1,
            'm': 60,
            'h': 60*60,
            'd': 60*60*24,
            'w': 60*60*24*7,
            }
    for component in re.findall(duration_match, timestr):
        t, unit = component[:-1], component[-1]
        duration += int(t) * multipliers[unit]

    return duration

class IRCAdmin(CommandPluginSuperclass):
    """Provides a command interface to IRC operator tasks. Uses the plugins in
    the ircop module to perform the operations.

    """
    REQUIRES = ["ircop.OpProvider"]

    def __init__(self, *args):
        self.started = False

        # This dictionary maps tuples of (hostmask, channel, mode) to twisted timer
        # objects. When the timer fires, the mode is unset on the given channel
        # for the given hostmask
        self.later_timers = {}


        super(IRCAdmin, self).__init__(*args)

    def reload(self):
        super(IRCAdmin, self).reload()

        if "laters" not in self.config:
            self.config['laters'] = []

        if self.started:
            self._set_all_timers()
        
    def _set_all_timers(self):
        """Reads from the config and syncs the twisted timers with that"""

        for timer in self.later_timers.itervalues():
            timer.cancel()

        for activatetime, hostmask, channel, mode in self.config['laters']:
            self._set_timer(activatetime - time.time(), hostmask, channel, mode)


    def _set_timer(self, delay, hostmask, channel, mode):
        """In delay seconds, issue a -mode request for hostmask on channel
        
        mode is either 'q' or 'b'
        
        """
        # First, cancel any existing timers and remove any existing saved
        # laters from the config
        if (hostmask, channel, mode) in self.later_timers:
            timer = self.later_timers.pop((hostmask, channel, mode))
            timer.cancel()

        # Filter out any events that match this one from the persistent config
        self.config['laters'] = [item for item in self.config['laters']
                if not (item[1] == hostmask and
                       item[2] == channel and
                       item[3] == mode
                       )]

        # This function will be run later
        @defer.inlineCallbacks
        def do_later():
            log.msg("timed request: -%s for %s in %s" % (mode, hostmask, channel))
            # First, take this item out of the mapping
            del self.later_timers[(hostmask, channel, mode)]

            # And the persistent config
            self.config['laters'] = [item for item in self.config['laters']
                    if not (item[1] == hostmask and
                           item[2] == channel and
                           item[3] == mode
                           )]
            self.config.save()

            # Now send the event
            try:
                yield self.transport.issue_request(
                        "ircop.mode",
                        channel=channel,
                        mode="-"+mode,
                        param=hostmask
                        )
            except ircop.OpFailed, e:
                s = "I was about to un-{0} {1}, but {2}".format(
                        {'q':'quiet','b':'ban'}[mode],
                        hostmask,
                        e,
                        )
                self.transport.send_event(Event("irc.do_msg",
                    user=channel,
                    message=s,
                    ))
                

        # Now submit the do_later() function to twisted to call it later
        timer = reactor.callLater(max(1,delay), do_later)

        log.msg("Setting -{0} on {1} in {2} in {3} seconds".format(
            mode,
            hostmask,
            channel,
            max(1,delay),
            ))

        # and file this timer away:
        self.later_timers[(hostmask, channel, mode)] = timer

        # Save to the persistent config
        self.config['laters'].append(
                (time.time()+delay, hostmask, channel, mode)
                )
        self.config.save()
        
    def on_event_irc_on_mode_change(self, event):
        """If a timer was set to un-ban or un-quiet a user, and we see them be
        un-banned or un-quieted before we get to it, cancel the timer.

        """
        if event.set == False:
            mode = event.mode
            user = event.arg
            channel = event.channel

            # Cancel any pending timers for this
            try:
                timer = self.later_timers.pop((user, channel, mode))
            except KeyError:
                pass
            else:
                timer.cancel()

                # Also filter out the persistent config entry
                self.config['laters'] = [item for item in self.config['laters']
                        if not (item[1] == user and
                               item[2] == channel and
                               item[3] == mode
                               )]
                self.config.save()


    def stop(self):
        super(IRCAdmin, self).stop()

        for timer in self.later_timers.itervalues():
            timer.cancel()
        
    def start(self):
        super(IRCAdmin, self).start()

        self.started = True
        self._set_all_timers()

        self.listen_for_event("irc.on_mode_change")

        # kick command
        self.install_command(
                cmdname="kick",
                cmdmatch="kick|KICK",
                cmdusage="<nickname> [reason]",
                argmatch = "(?P<nick>[^ ]+)( (?P<reason>.*))?$",
                permission="irc.op.kick",
                prefix=".",
                callback=self.kick,
                deniedcallback=self.kickself,
                helptext="Kicks a user from the current channel")

        # Op commands
        self.install_command(
                cmdname="op",
                prefix=".",
                cmdusage="[nick]",
                argmatch="(?P<nick>[^ ]+)?",
                permission="irc.op.op",
                callback=self.give_op,
                helptext="Gives op to the specified user",
                )
        self.install_command(
                cmdname="deop",
                prefix=".",
                cmdusage="[nick]",
                argmatch="(?P<nick>[^ ]+)?",
                permission="irc.op.op",
                callback=self.take_op,
                helptext="Takes op from the specified user",
                )

        # voice commands
        self.install_command(
                cmdname="voice",
                cmdmatch="voice|VOICE|hat",
                cmdusage="[nick]",
                argmatch = "(?P<nick>[^ ]+)?$",
                permission="irc.op.voice",
                prefix=".",
                callback=self.voice,
                helptext="Grants a user voice in the current channel"
                )

        self.install_command(
                cmdname="devoice",
                cmdmatch="devoice|DEVOICE|dehat|unhat",
                cmdusage="[nick]",
                argmatch = "(?P<nick>[^ ]+)?$",
                permission="irc.op.voice",
                prefix=".",
                callback=self.devoice,
                helptext="Revokes a user's voice in the current channel"
                )

        # Quiet commands
        self.install_command(
                cmdname="quiet",
                cmdmatch="quiet|QUIET|mute",
                cmdusage="<nick or hostmask> [for <duration>]",
                argmatch = "(?P<nick>[^ ]+)(?: (?:for )?{0}+)?$".format(duration_match),
                prefix=".",
                permission="irc.op.quiet",
                callback=self.quiet,
                deniedcallback=self.quietself,
                helptext="Quiets a user."
                )

        self.install_command(
                cmdname="unquiet",
                cmdmatch="unquiet|UNQUIET|unmute",
                cmdusage="<nick or hostmask> [in <delay>]",
                argmatch = "(?P<nick>[^ ]+)(?: (?:in )?{0}+)?$".format(duration_match),
                prefix=".",
                permission="irc.op.quiet",
                callback=self.unquiet,
                helptext="Un-quiets a user"
                )

        # Ban commands
        self.install_command(
                cmdname="ban",
                cmdmatch="ban|BAN",
                cmdusage="<nick or hostmask> [for <duration>] [reason]",
                argmatch = "(?P<nick>[^ ]+)(?: (?:for )?{0}+)?(?: (?P<reason>.+))?$".format(duration_match),
                prefix=".",
                permission="irc.op.ban",
                callback=self.ban,
                helptext="Bans a user."
                )

        self.install_command(
                cmdname="unban",
                cmdmatch="unban|UNBAN",
                cmdusage="<nick or hostmask> [in <delay>]",
                argmatch = "(?P<nick>[^ ]+)(?: (?:in )?{0}+)?$".format(duration_match),
                prefix=".",
                permission="irc.op.ban",
                callback=self.unban,
                helptext="Un-bans a user"
                )

    @defer.inlineCallbacks
    def _nick_to_hostmask(self, nick):
        """Takes a nick or a hostmask and returns a parameter suitable for the
        +b or +q modes. If the items given looks like a hostmask (contains a !
        and a @) then it is returned. If the item is an extban (starts with a
        $), then that is returned. Otherwise, it is assumed the parameter is a
        nickname and a whois is performed and the hostmask is returned with the
        first two fields wildcarded.

        This methed is intended to allow bans and quiets to match any nick!user
        combination by banning/quieting all users from that host.

        If no such user is found, an ircutil.NoSuchNick is raised. If the whois
        fails, an ircutil.WhoisTimedout is raised.

        Returnes a deferred that fires with the answer.

        """
        if ("!" in nick and "@" in nick) or (nick.startswith("$")):
            defer.returnValue(nick)
            return

        whois_results = (yield self.transport.issue_request("irc.whois", nick))

        whoisuser = whois_results['RPL_WHOISUSER']

        mask = "{0}!{1}@{2}".format(
                '*',
                '*',
                whoisuser[2],
                )

        defer.returnValue(mask)

    @require_channel
    @defer.inlineCallbacks
    def kick(self, event, match):
        """A user has issued the kick command. Our job here is to acquire OP
        for this channel and issue a kick event

        """
        groupdict = match.groupdict()
        nick = groupdict['nick']
        reason = groupdict.get("reason", None)
        channel = event.channel

        try:
            yield self.transport.issue_request("ircop.kick", channel=channel,
                target=nick, reason=reason)
        except ircop.OpFailed, e:
            event.reply(str(e))

    @require_channel
    def kickself(self, event, match):
        targetnick = match.groupdict()['nick']
        requestor = event.user.split("!")[0]

        if targetnick == requestor:
            self.transport.issue_request("ircop.kick", channel=event.channel,
                target=requestor, reason="okay, you asked for it")
            return True
        elif random.randint(1,4) == 4:
            self.transport.issue_request("ircop.kick", channel=event.channel,
                target=requestor, reason="woops, my bad!")
            return True

    @require_channel
    @defer.inlineCallbacks
    def voice(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel

        try:
            yield self.transport.issue_request("ircop.voice", channel=channel,
                target=nick)
        except ircop.OpFailed, e:
            event.reply(str(e))
        log.msg("Voicing %s in %s" % (nick, channel))

    @require_channel
    @defer.inlineCallbacks
    def devoice(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel

        try:
            yield self.transport.issue_request("ircop.devoice", channel=channel,
                target=nick)
        except ircop.OpFailed, e:
            event.reply(str(e))
        log.msg("De-voicing %s in %s" % (nick, channel))

    @require_channel
    @defer.inlineCallbacks
    def give_op(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel
        
        log.msg("Opping %s in %s" % (nick, channel))
        try:
            yield self.transport.issue_request("ircop.op",channel,nick)
        except ircop.OpFailed, e:
            event.reply(str(e))

    @require_channel
    @defer.inlineCallbacks
    def take_op(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel
        log.msg("Deopping %s in %s" % (nick, channel))
        try:
            yield self.transport.issue_request("ircop.deop",channel,nick)
        except ircop.OpFailed, e:
            event.reply(str(e))

    @require_channel
    def quiet(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        duration = groupdict['duration']
        channel = event.channel

        self._do_moderequest('q', event.reply, nick, duration, channel)

    @require_channel
    def quietself(self, event, match):
        groupdict = match.groupdict()
        nick = event.user.split("!")[0]
        if random.randint(1,3) == 3 or nick == groupdict['nick']:
            duration = 10
            channel = event.channel
            def r(s):
                event.reply("naa, I don't feel like it right now", userprefix=False)
                log.msg(s)
            self._do_moderequest("q",
                    r,
                    nick,
                    duration,
                    channel,
                    )
            if nick != groupdict['nick']:
                reactor.callLater(7,
                        event.reply,
                        "Woops, my bad!",
                        )
            return True

    @require_channel
    @defer.inlineCallbacks
    def ban(self, event, match):
        groupdict = match.groupdict()
        # nick here could be a nick, a hostmask (with possible wildcards), or
        # an extban
        nick = groupdict['nick']
        duration = groupdict['duration']
        channel = event.channel
        reason = groupdict['reason']

        do_kick = False
        if "@" in nick and "!" in nick and not "$" in nick:
            # A mask was given. Kick if the nick section doesn't have any
            # wildcards
            nick = nick.split("!")[0]
            if "*" not in nick:
                do_kick = True
        elif "@" not in nick and "!" not in nick and "$" not in nick:
            # Just a nick was given.
            do_kick = True

        # We have to issue the kick before the mode request. The kick will
        # acquire op, and the mode request will deop in the same request as the
        # ban. If it were the other way around, it would get op, issue the ban
        # and deop, then op to do the kick.
        if do_kick:
            log.msg("issuing kick")
            yield self.transport.issue_request("ircop.kick",
                    channel=channel,
                    target=nick,
                    reason=reason or ("Requested by " + event.user.split("!")[0]),
                    )

        log.msg("issuing ban")
        yield self._do_moderequest('b', event.reply, nick, duration, channel)


    @defer.inlineCallbacks
    def _do_moderequest(self, mode, reply, nick, duration, channel):
        """Does the work to set a mode on a nick (or hostmask) in a channel for
        an optional duration. If duration is None, we will not set it back
        after any length of time.

        reply is used to send error messages. It should take a string.

        """
        try:
            mask = (yield self._nick_to_hostmask(nick))
        except ircutil.NoSuchNick:
            reply("There is no user by that nick on the network. Try {0}!*@* to {1} anyone with that nick, or specify your own hostmask.".format(
                nick,
                {"q":"quiet","b":"ban"}.get(mode, "apply to"),
                ))
            return
        except ircutil.WhoisTimedout:
            reply("That's odd, the whois I did on %s didn't work. Sorry." % nick)
            return

        if duration:
            log.msg("+%s for %s in %s for %s" % (mode, mask, channel, duration))
        else:
            log.msg("+%s for %s in %s" % (mode, mask, channel, ))

        try:
            yield self.transport.issue_request("ircop.mode",
                    channel=channel,
                    mode="+"+mode,
                    param=mask,
                    )
        except ircop.OpFailed, e:
            reply(str(e))
            return

        if duration:
            if isinstance(duration, basestring):
                duration = parse_time(duration)
            self._set_timer(duration, mask, channel, mode)

    @require_channel
    def unquiet(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        duration = groupdict['duration']
        channel = event.channel
        
        self._do_modederequest('q', event.reply, nick, duration, channel)

    @require_channel
    def unban(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        duration = groupdict['duration']
        channel = event.channel
        
        self._do_modederequest('b', event.reply, nick, duration, channel)

    @defer.inlineCallbacks
    def _do_modederequest(self, mode, reply, nick, duration, channel):
        try:
            mask = (yield self._nick_to_hostmask(nick))
        except ircutil.NoSuchNick:
            reply("There is no user by than nick on the network. Check the username or try specifying a full hostmask")
            return
        except ircutil.WhoisTimedout:
            reply("That's odd, the whois I did on %s didn't work. Sorry." % nick)
            return

        if duration:
            if isinstance(duration, basestring):
                duration = parse_time(duration)
            self._set_timer(duration, mask, channel, mode)
            reply("It shall be done")
            return

        try:
            self.transport.issue_request("ircop.mode",
                    channel=channel,
                    mode="-"+mode,
                    param=mask,
                    )
        except ircop.OpFailed, e:
            reply(str(e))
        log.msg("-%s for %s in %s" % (mode, mask, channel))


class IRCTopic(CommandPluginSuperclass):
    """Topic manipulation commands.

    """
    REQUIRES=["ircutil.ChanMode", "ircop.OpProvider"]
    def start(self):
        super(IRCTopic, self).start()

        # Topic commands
        topicgroup = self.install_cmdgroup(
                grpname="topic",
                prefix=None,
                permission="irc.op.topic",
                helptext="Topic manipulation commands",
                )

        topicgroup.install_command(
                cmdname="append",
                cmdmatch="append|push",
                cmdusage="<text>",
                argmatch="(?P<text>.+)$",
                permission=None, # Inherits permissions from the group
                callback=self.topicappend,
                helptext="Appends text to the end of the channel topic",
                )
        topicgroup.install_command(
                cmdname="insert",
                cmdmatch=None,
                cmdusage="<pos> <text>",
                argmatch=r"(?P<pos>-?\d+) (?P<text>.+)$",
                callback=self.topicinsert,
                helptext="Inserts text into the topic at the given position",
                )

        topicgroup.install_command(
                cmdname="replace",
                cmdmatch="set|replace",
                cmdusage="<pos> <text>",
                argmatch=r"(?P<pos>-?\d+) (?P<text>.+)$",
                callback=self.topicreplace,
                helptext="Replaces the given section with the given text",
                )

        topicgroup.install_command(
                cmdname="remove",
                cmdmatch=None,
                cmdusage="<pos>",
                argmatch=r"(?P<pos>-?\d+)$",
                callback=self.topicremove,
                helptext="Removes the pos'th topic selection",
                )
        topicgroup.install_command(
                cmdname="pop",
                callback=self.topicpop,
                helptext="Removes the last topic item",
                )

        topicgroup.install_command(
                cmdname="undo",
                callback=self.topic_undo,
                helptext="Reverts the topic to the last known channel topic",
                )

        # Maps channel names to the last so many topics
        # (The top most item on the stack should be the current topic. But the
        # handlers should handle the case that the stack is empty!)
        self.topic_stack = defaultdict(lambda: deque(maxlen=10))
        self.listen_for_event("irc.on_topic_updated")
        # set of deferreds waiting for the current topic response in a channel
        self.topic_waiters = defaultdict(set)

    ### Topic methods
    def on_event_irc_on_topic_updated(self, event):
        channel = event.channel
        newtopic = event.newtopic
        oldtopic = None
        try:
            oldtopic = self.topic_stack[channel][-1]
        except IndexError:
            pass
        if newtopic != oldtopic:
            self.topic_stack[event.channel].append(newtopic)
            log.msg("Topic updated in %s. Now I know about %s past topics (including this one)" % (event.channel,
                len(self.topic_stack[event.channel])))

        for d in self.topic_waiters.pop(channel, set()):
            d.callback(newtopic)

    def _get_current_topic(self, channel):
        """Returns a deferred object with the current topic.
        The callback will be called with the channel topic once it's known. The
        errback will be called if the topic cannot be determined
        
        """
        topic_stack = self.topic_stack[channel]
        if topic_stack:
            return defer.succeed(topic_stack[-1])

        # We need to ask what the topic is. Go ahead and send off that event.
        log.msg("Sending a request for the current topic since I don't know it")
        topicrequest = Event("irc.do_topic",
                channel=channel)
        self.transport.send_event(topicrequest)

        # Now set up a deferred object that will be called when the topic comes in
        deferreds = self.topic_waiters[channel]
        new_d = defer.Deferred()

        if not deferreds:
            # No current deferreds in the set. Set up a failure callback
            def failure(_):
                log.msg("Topic request timed out. Calling errbacks")
                for d in self.topic_waiters.pop(channel, set()):
                    d.errback()
            c = reactor.callLater(10, failure)
            # Set a success callback to cancel the failure timeout
            def success(result):
                log.msg("Topic result came in")
                c.cancel()
                return result
            new_d.addCallback(success)

        deferreds.add(new_d)
        return new_d

    @require_channel
    def topicappend(self, event, match):
        channel = event.channel
        def callback(currenttopic):
            topic_parts = [x.strip() for x in currenttopic.strip().split("|")]
            topic_parts.append(match.groupdict()['text'])
            self.transport.issue_request("ircop.topic", channel, " | ".join(topic_parts))
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topicinsert(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            gd = match.groupdict()
            pos = int(gd['pos'])
            text = gd['text']

            topic_parts = [x.strip() for x in currenttopic.split("|")]
            topic_parts.insert(pos, text)

            newtopic = " | ".join(topic_parts)
            self.transport.issue_request("ircop.topic", channel, newtopic)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topicreplace(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            gd = match.groupdict()
            pos = int(gd['pos'])
            text = gd['text']

            topic_parts = [x.strip() for x in currenttopic.split("|")]
            try:
                topic_parts[pos] = text
            except IndexError:
                event.reply("There are only %s topic parts. Remember indexes start at 0" % len(topic_parts))
                return


            newtopic = " | ".join(topic_parts)
            self.transport.issue_request("ircop.topic", channel, newtopic)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topicremove(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            gd = match.groupdict()
            pos = int(gd['pos'])

            topic_parts = [x.strip() for x in currenttopic.split("|")]
            try:
                del topic_parts[pos]
            except IndexError:
                event.reply("There are only %s topic parts. Remember indexes start at 0" % len(topic_parts))
                return

            newtopic = " | ".join(topic_parts)
            self.transport.issue_request("ircop.topic", channel, newtopic)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topicpop(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            topic_parts = [x.strip() for x in currenttopic.split("|")]
            try:
                del topic_parts[-1]
            except IndexError:
                event.reply("There are only %s topic parts. Remember indexes start at 0" % len(topic_parts))
                return

            newtopic = " | ".join(topic_parts)
            self.transport.issue_request("ircop.topic", channel, newtopic)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topic_undo(self, event, match):
        channel = event.channel

        topicstack = self.topic_stack[channel]
        if len(topicstack) < 2:
            event.reply("I don't know what the topic used to be. Cannot undo =(")
            return
        # Pop the current item off
        topicstack.pop()
        # Now pop the next item, which will be our new topic
        newtopic = topicstack.pop()

        self.transport.issue_request("ircop.topic", channel, newtopic)


